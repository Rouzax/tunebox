"""The admin panel's HTTP side: its pages, its JSON API under /admin/api/,
and the rules every admin request passes. The server hands every /admin
request here, so the panel's pages are never reachable through the player's
static files."""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tunebox import crypto
from tunebox.accounts import (
    ABSOLUTE_S,
    AccountError,
    Accounts,
    Outcome,
    PasswordTooShort,
    Session,
    SessionEnded,
    session_id,
)
from tunebox.config import (
    CODECS,
    DEFAULTS,
    SOURCE_CHOICES,
    SOURCE_SETTINGS,
    AdminConfig,
    ConfigError,
    validate_state,
)
from tunebox.logs import event
from tunebox.paths import inside
from tunebox.pin import PIN_MAX, PIN_MIN, Pin, PinError, PinStale, read_cookie
from tunebox.rulecheck import refused
from tunebox.sessions import KNOWN_S, Gate
from tunebox.settings import (
    Settings,
    SettingsUnavailable,
    SettingsWriteError,
    StaleVersion,
    state_from_dict,
    state_to_json,
)

log = logging.getLogger(__name__)

JSON = "application/json; charset=utf-8"
MAX_BODY = 65_536
# A save sends the whole station list: about 40 kB for 50 stations. Granted
# only to a request whose headers already pass the session checks, before
# its body is read.
MAX_STATE_BODY = 524_288
# Longest history target a save may carry.
MAX_TARGET = 200
LARGE_ROUTES = {"config": MAX_STATE_BODY}
SESSION_COOKIE = "tunebox_admin"
KNOWN_COOKIE = "tunebox_known"
SETUP_TOKEN_S = 900
ADMIN_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' data:",
        "media-src https:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)
ADMIN_HEADERS = {
    "Content-Security-Policy": ADMIN_CSP,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    # Every header line as received, so a repeated one can be told apart.
    headers: tuple[tuple[str, str], ...]
    body: bytes
    client: str
    query: str = ""

    def header(self, name: str) -> str | None:
        """The header's value; None when it is missing or repeated. A
        repeated Origin, token or cookie line is a broken proxy or a forgery,
        and either way not one value to trust."""
        wanted = name.lower()
        found = [v for k, v in self.headers if k.lower() == wanted]
        return found[0] if len(found) == 1 else None

    def cookie(self, name: str) -> str | None:
        return read_cookie(self.header("Cookie"), name)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = JSON
    headers: tuple[tuple[str, str], ...] = ()


_UINT = re.compile(r"[0-9]{1,18}")


def _uint(text: str) -> int | None:
    """A history id: ASCII digits that fit SQLite's integer."""
    return int(text) if _UINT.fullmatch(text) else None


def _same(given: str, expected: str) -> bool:
    """A constant-time comparison that also takes text compare_digest
    refuses (non-ASCII), as a mismatch rather than an error."""
    return hmac.compare_digest(
        given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
    )


def _json(status: int, payload: object, *headers: tuple[str, str]) -> Response:
    return Response(status, json.dumps(payload).encode(), JSON, headers)


def _error(status: int, name: str) -> Response:
    return _json(status, {"error": name})


class Admin:
    def __init__(
        self,
        accounts: Accounts,
        config: AdminConfig,
        web_dir: Path,
        settings: Settings,
        pin: Pin,
        apply: Callable[[], int],
        samples: Callable[[], list[str]],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """pin is the listening PIN the settings page sets; apply reloads the
        running channels after a change; samples gives recent titles that
        new title rules are tried on."""
        self._accounts = accounts
        self._pin = pin
        self._settings = settings
        self._apply = apply
        self._samples = samples
        self._origin = config.public_origin
        # Over plain http a browser drops Secure cookies and refuses the
        # __Secure- prefix; the server then serves the panel only to the
        # home network (see nearby in netguard).
        self.plain_http = config.plain_http
        prefix = "" if self.plain_http else "__Secure-"
        self.session_cookie = prefix + SESSION_COOKIE
        self.known_cookie = prefix + KNOWN_COOKIE
        if self.plain_http:
            event(log, logging.WARNING, "admin_plain_http", origin=config.public_origin)
        self._web = web_dir.resolve()
        self._clock = clock
        self._monotonic = monotonic
        self.gate = Gate(accounts.server_key, self.plain_http, clock, monotonic, sleep)
        self._setup_token: str | None = None
        self._setup_until = 0.0
        self._setup_secret: bytes | None = None
        if config.setup_page and not accounts.exists():
            self._setup_token = crypto.new_token()
            self._setup_until = monotonic() + SETUP_TOKEN_S
            # The one secret tunebox logs: it is how the owner finds it.
            event(
                log,
                logging.WARNING,
                "admin_setup_token",
                token=self._setup_token,
                path=f"/admin/#setup={self._setup_token}",
                expires_in_s=SETUP_TOKEN_S,
            )

    def handle(self, request: Request) -> Response:
        try:
            response = self._dispatch(request)
        except Exception:
            # Logged here, inside the admin context, so the formatter
            # withholds the exception text.
            log.error("request_crashed", exc_info=True, extra={"fields": {"path": request.path}})
            response = _error(500, "internal")
        return Response(
            response.status,
            response.body,
            response.content_type,
            (*response.headers, *ADMIN_HEADERS.items()),
        )

    def _dispatch(self, request: Request) -> Response:
        if request.path == "/admin":
            return Response(301, headers=(("Location", "/admin/"),))
        if request.path.startswith("/admin/api/"):
            try:
                return self._api(request)
            except SettingsWriteError, SettingsUnavailable:
                return _error(503, "read_only")
        if request.method not in ("GET", "HEAD"):
            return _error(405, "method_not_allowed")
        return self._static(request.path.removeprefix("/admin/"))

    def _api(self, request: Request) -> Response:
        route = self._route(request.method, request.path.removeprefix("/admin/api/"))
        if route is None:
            return _error(404, "not_found")
        handler, needs_session = route
        refused = self._refusal(request, needs_session)
        if refused is not None:
            return refused
        return handler(request)

    def body_limit(self, request: Request) -> int:
        """The largest body this POST may send, judged on its headers alone
        (the body is not read yet): a large one only on a route that takes
        one, for a live session with its token and the right origin."""
        if request.method != "POST" or not request.path.startswith("/admin/api/"):
            return MAX_BODY
        large = LARGE_ROUTES.get(request.path.removeprefix("/admin/api/"))
        if large is None:
            return MAX_BODY
        try:
            refused = self._refusal(request, needs_session=True, log_refusals=False)
        except SettingsUnavailable:
            return MAX_BODY
        return MAX_BODY if refused is not None else large

    def _refusal(
        self, request: Request, needs_session: bool, log_refusals: bool = True
    ) -> Response | None:
        """The origin, content type, session and token checks a request must
        pass before its handler runs: the refusal, or None to go on. A
        caller that answers the refusal itself later passes log_refusals
        False so the rejection is logged once."""
        if request.method == "POST":
            if not self._origin or request.header("Origin") != self._origin:
                if log_refusals:
                    event(
                        log,
                        logging.WARNING,
                        "admin_csrf_rejected",
                        client=request.client,
                        why="origin",
                    )
                return _error(403, "bad_origin")
            content_type = (request.header("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                return _error(415, "json_only")
        if needs_session:
            session = self._session(request)
            if session is None:
                return _error(401, "login_required")
            given = request.header("X-Tunebox-CSRF") or ""
            if request.method == "POST" and not _same(given, session.csrf):
                if log_refusals:
                    event(
                        log,
                        logging.WARNING,
                        "admin_csrf_rejected",
                        client=request.client,
                        why="token",
                    )
                return _error(403, "bad_csrf")
        return None

    def _route(self, method: str, name: str) -> tuple[Callable[[Request], Response], bool] | None:
        """The handler and whether it needs a session."""
        fixed: dict[tuple[str, str], tuple[Callable[[Request], Response], bool]] = {
            ("GET", "state"): (self._state, False),
            ("POST", "login"): (self._login, False),
            ("POST", "logout"): (self._logout, False),
            ("POST", "setup/start"): (self._setup_start, False),
            ("POST", "setup/finish"): (self._setup_finish, False),
            ("GET", "config"): (self._config, True),
            ("POST", "config"): (self._save, True),
            ("GET", "history"): (self._history, True),
            ("GET", "account"): (self._account, True),
            ("POST", "account/reauth"): (self._reauth, True),
            ("POST", "account/password"): (self._password, True),
            ("POST", "account/totp/start"): (self._totp_start, True),
            ("POST", "account/totp/finish"): (self._totp_finish, True),
            ("POST", "account/recovery"): (self._recovery, True),
            ("GET", "pin"): (self._pin_status, True),
            ("POST", "pin"): (self._pin_change, True),
        }
        if (method, name) in fixed:
            return fixed[(method, name)]
        parts = name.split("/")
        if parts[:2] == ["account", "sessions"] and len(parts) == 4 and parts[3] == "end":
            return (self._end_session, True) if method == "POST" else None
        if parts[0] != "history" or not parts[1:2] or _uint(parts[1]) is None:
            return None
        if len(parts) == 2 and method == "GET":
            return (self._history_entry, True)
        if parts[2:] == ["restore"] and method == "POST":
            return (self._restore, True)
        return None

    def _cookie(self, name: str, value: str, max_age: int) -> tuple[str, str]:
        secure = "" if self.plain_http else " Secure;"
        attributes = f"Path=/admin; Max-Age={max_age}; HttpOnly;{secure} SameSite=Strict"
        return ("Set-Cookie", f"{name}={value}; {attributes}")

    def _session(self, request: Request) -> Session | None:
        token = request.cookie(self.session_cookie)
        return self._accounts.session(token) if token else None

    def _state(self, request: Request) -> Response:
        session = self._session(request)
        detail = None
        if session is not None:
            detail = {
                "csrf": session.csrf,
                "client": request.client,
                "totp_suspected": self._accounts.totp_suspected(),
            }
        return _json(
            200,
            {
                "account": self._accounts.exists(),
                "setup": self._setup_open(),
                "session": detail,
                "limits": {
                    "password_min": crypto.MIN_PASSWORD,
                    "pin_min": PIN_MIN,
                    "pin_max": PIN_MAX,
                },
            },
        )

    def _login(self, request: Request) -> Response:
        body = _body(request, ("password", "code"))
        if body is None:
            return _error(400, "bad_request")
        # The address lock first: a locked address must not spend the
        # shared pace, or one host could keep everyone else out.
        release = self.gate.reserve(request.client)
        if release is None:
            return self._limited(request, "address")
        if not self.gate.pace(self._known(request)):
            release()
            return self._limited(request, "pace")
        started = self._monotonic()
        with self.gate.hashing() as took:
            if not took:
                release()
                return self._limited(request, "busy")
            user_agent = request.header("User-Agent") or ""
            outcome = self._accounts.login(
                body["password"], body["code"], request.client, user_agent
            )
        answer = self._login_answer(request, outcome, release)
        self.gate.floor(started)
        return answer

    def _login_answer(
        self, request: Request, outcome: Outcome, release: Callable[[], None]
    ) -> Response:
        if outcome.reason == "unavailable":
            # A right login that could not be saved is not a failure of
            # this address.
            release()
            return _error(503, "read_only")
        if not outcome.ok:
            event(
                log,
                logging.WARNING,
                "admin_login_failed",
                client=request.client,
                reason=outcome.reason,
            )
            if outcome.reason != "password" and self._accounts.totp_suspected():
                event(log, logging.WARNING, "admin_totp_suspected", client=request.client)
            return _error(401, "login_failed")
        release()
        event(log, logging.INFO, "admin_login", client=request.client)
        return _json(
            200,
            {"csrf": outcome.csrf},
            self._cookie(self.session_cookie, outcome.token or "", ABSOLUTE_S),
            self._known_grant(self._accounts.generation()),
        )

    def _known_grant(self, generation: int) -> tuple[str, str]:
        """The known-browser cookie for an account generation."""
        return self._cookie(self.known_cookie, self.gate.known_grant(generation), KNOWN_S)

    def _logout(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            # Already logged out (or expired): nothing to protect.
            return Response(204, headers=(self._cookie(self.session_cookie, "", 0),))
        given = request.header("X-Tunebox-CSRF") or ""
        if not _same(given, session.csrf):
            event(log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="token")
            return _error(403, "bad_csrf")
        self._accounts.logout(request.cookie(self.session_cookie) or "")
        self.gate.forget_totp(session.token_hash)
        return Response(204, headers=(self._cookie(self.session_cookie, "", 0),))

    def _known(self, request: Request) -> bool:
        """A browser that logged in before gets its own lane in the pace."""
        return self.gate.known(request.cookie(self.known_cookie), self._accounts.generation())

    def _limited(self, request: Request, why: str) -> Response:
        event(log, logging.WARNING, "admin_rate_limited", client=request.client, why=why)
        return _error(429, "slow_down")

    def _setup_open(self) -> bool:
        return (
            self._setup_token is not None
            and self._monotonic() < self._setup_until
            and not self._accounts.exists()
        )

    def _setup_allowed(self, request: Request, token: str) -> bool:
        """A wrong token counts as a failed login for the address."""
        release = self.gate.reserve(request.client)
        if release is None:
            return False
        if self._setup_open() and _same(token, self._setup_token or ""):
            release()
            return True
        event(log, logging.WARNING, "admin_setup_token_rejected", client=request.client)
        return False

    def _setup_start(self, request: Request) -> Response:
        body = _body(request, ("token",))
        if body is None:
            return _error(400, "bad_request")
        if not self._setup_allowed(request, body["token"]):
            return _error(401, "setup_refused")
        # One secret per token, so a reload shows the secret already scanned.
        if self._setup_secret is None:
            self._setup_secret = crypto.new_totp_secret()
        return _json(
            200,
            {
                "secret": crypto.secret_text(self._setup_secret),
                "uri": crypto.otpauth_uri(self._setup_secret),
            },
        )

    def _setup_finish(self, request: Request) -> Response:
        body = _body(request, ("token", "password", "code"))
        if body is None:
            return _error(400, "bad_request")
        secret = self._setup_secret
        if not self._setup_allowed(request, body["token"]) or secret is None:
            return _error(401, "setup_refused")
        try:
            body["password"].encode()
        except UnicodeEncodeError:  # a lone surrogate from a JSON escape
            return _error(400, "bad_request")
        step = crypto.matching_step(secret, body["code"].strip(), self._clock(), -1)
        if step is None:
            return _error(400, "code_wrong")
        codes = crypto.new_recovery_codes()
        with self.gate.hashing() as took:
            if not took:
                return self._limited(request, "busy")
            try:
                self._accounts.create(body["password"], secret, codes, step)
            except PasswordTooShort:
                return _error(400, "password_too_short")
            except AccountError:
                return _error(401, "setup_refused")
        self._setup_token = None
        self._setup_secret = None
        event(log, logging.WARNING, "admin_account_created", client=request.client)
        return _json(200, {"recovery_codes": codes})

    def _config(self, _request: Request) -> Response:
        state, version = self._settings.load()
        schema = {
            "sources": SOURCE_SETTINGS,
            "choices": SOURCE_CHOICES,
            "codecs": list(CODECS),
            "defaults": DEFAULTS,
        }
        return _json(
            200, {"version": version, "state": json.loads(state_to_json(state)), "schema": schema}
        )

    def _save(self, request: Request) -> Response:
        payload = _object(request)
        based_on = _version(payload)
        if payload is None or based_on is None:
            return _error(400, "bad_request")
        target = payload.get("target")
        if isinstance(target, str) and len(target) > MAX_TARGET:
            return _error(400, "bad_request")
        return self._commit(
            payload.get("state"), based_on, "edit", target if isinstance(target, str) else None
        )

    def _commit(self, raw: object, based_on: int, kind: str, target: str | None) -> Response:
        try:
            state = state_from_dict(raw)
            validate_state(state)
            stored, version = self._settings.load()
            if version != based_on:
                return _json(409, {"error": "stale", "version": version})
            reason = refused(state, stored, self._samples())
            if reason:
                return _json(400, {"error": "invalid", "message": reason})
            version, changed = self._settings.commit_changed(state, based_on, kind, target)
        except ConfigError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        except StaleVersion as error:
            return _json(409, {"error": "stale", "version": error.current})
        if not changed:
            return _json(200, {"version": version, "changed": False, "applied": True})
        applied = self._apply() >= version
        if not applied:
            event(log, logging.WARNING, "admin_change_not_live", version=version)
        event(log, logging.INFO, "admin_change", kind=kind, target=target, version=version)
        return _json(200, {"version": version, "changed": True, "applied": applied})

    def _history(self, request: Request) -> Response:
        raw = urllib.parse.parse_qs(request.query).get("before", [""])[0]
        before = _uint(raw) if raw else None
        if raw and before is None:
            return _error(400, "bad_request")
        entries, more = self._settings.history(before)
        return _json(200, {"entries": [dataclasses.asdict(e) for e in entries], "more": more})

    def _history_entry(self, request: Request) -> Response:
        found = self._settings.history_entry(_history_id(request.path))
        if found is None:
            return _error(404, "not_found")
        entry, before, after = found
        return _json(
            200,
            {
                "entry": dataclasses.asdict(entry),
                "before": json.loads(state_to_json(before)),
                "after": json.loads(state_to_json(after)),
            },
        )

    def _restore(self, request: Request) -> Response:
        based_on = _version(_object(request))
        if based_on is None:
            return _error(400, "bad_request")
        entry_id = _history_id(request.path)
        found = self._settings.history_entry(entry_id)
        if found is None:
            return _error(404, "not_found")
        _, before, _ = found
        raw = json.loads(state_to_json(before))
        return self._commit(raw, based_on, "restore", f"history {entry_id}")

    def _live(self, request: Request) -> Session | None:
        """The caller's session, or None when it ended after _api checked it."""
        return self._session(request)

    def _fresh(self, request: Request) -> Session | Response:
        """The caller's session when it typed a code recently; else the
        answer to give (401 when it has ended, 403 when it is not fresh)."""
        session = self._live(request)
        if session is None:
            return _error(401, "login_required")
        if not self._accounts.fresh(session):
            return _error(403, "reauth_required")
        return session

    def _account(self, request: Request) -> Response:
        session = self._live(request)
        if session is None:
            return _error(401, "login_required")
        mine = session_id(session.token_hash)
        listed = [
            {**dataclasses.asdict(s), "current": s.id == mine} for s in self._accounts.sessions()
        ]
        return _json(200, {"sessions": listed, "fresh": self._accounts.fresh(session)})

    def _reauth(self, request: Request) -> Response:
        body = _body(request, ("code",))
        if body is None:
            return _error(400, "bad_request")
        # Read before reserving: a session that is gone spends no slot.
        session = self._live(request)
        if session is None:
            return _error(401, "login_required")
        release = self.gate.reserve(request.client)
        if release is None:
            # Not why=address, which the CrowdSec ban scenario counts: this
            # is someone with a session typing codes wrong.
            return self._limited(request, "reauth")
        reason = self._accounts.reauth(session, body["code"])
        if reason == "unavailable":
            release()
            return _error(503, "read_only")
        if reason is not None:
            # Not admin_login_failed: the CrowdSec scenarios ban on that, and
            # this is someone with a session, most likely the owner.
            event(log, logging.WARNING, "admin_reauth_failed", client=request.client, reason=reason)
            return _error(400, "code_wrong")
        release()
        return Response(204)

    def _password(self, request: Request) -> Response:
        body = _body(request, ("password",))
        if body is None:
            return _error(400, "bad_request")
        session = self._fresh(request)
        if isinstance(session, Response):
            return session
        password = body["password"]
        try:
            password.encode()
        except UnicodeEncodeError:  # a lone surrogate from a JSON escape
            return _error(400, "bad_request")
        with self.gate.hashing() as took:
            if not took:
                return self._limited(request, "busy")
            try:
                generation = self._accounts.change_password(session, password)
            except PasswordTooShort:
                return _error(400, "password_too_short")
            except SessionEnded:
                return _error(401, "login_required")
            except AccountError as error:
                return _json(400, {"error": "invalid", "message": str(error)})
        event(log, logging.INFO, "admin_change", kind="password", client=request.client)
        return Response(204, headers=(self._known_grant(generation),))

    def _totp_start(self, request: Request) -> Response:
        session = self._fresh(request)
        if isinstance(session, Response):
            return session
        secret = crypto.new_totp_secret()
        self.gate.hold_totp(session.token_hash, secret)
        return _json(200, {"secret": crypto.secret_text(secret), "uri": crypto.otpauth_uri(secret)})

    def _totp_finish(self, request: Request) -> Response:
        body = _body(request, ("code",))
        if body is None:
            return _error(400, "bad_request")
        session = self._fresh(request)
        if isinstance(session, Response):
            return session
        secret = self.gate.take_totp(session.token_hash)
        if secret is None:
            return _error(400, "no_new_secret")
        step = crypto.matching_step(secret, body["code"].strip(), self._clock(), -1)
        if step is None:
            return _error(400, "code_wrong")
        try:
            generation = self._accounts.replace_totp(session, secret, step)
        except SessionEnded:
            return _error(401, "login_required")
        # Every session but this one has ended; their waiting secrets too.
        self.gate.drop_totp(keep=())
        event(log, logging.INFO, "admin_change", kind="totp", client=request.client)
        return Response(204, headers=(self._known_grant(generation),))

    def _recovery(self, request: Request) -> Response:
        session = self._fresh(request)
        if isinstance(session, Response):
            return session
        try:
            codes, generation = self._accounts.replace_recovery_codes(session)
        except SessionEnded:
            return _error(401, "login_required")
        event(log, logging.INFO, "admin_change", kind="recovery_codes", client=request.client)
        return _json(200, {"recovery_codes": codes}, self._known_grant(generation))

    def _end_session(self, request: Request) -> Response:
        wanted = request.path.removeprefix("/admin/api/account/sessions/").split("/")[0]
        session = self._live(request)
        if session is None:
            return _error(401, "login_required")
        if wanted == session_id(session.token_hash):
            return _error(400, "this_session")
        ended = self._accounts.end_session(wanted)
        if ended is None:
            return _error(404, "not_found")
        # Other live sessions keep their waiting secrets: they may be
        # halfway through a change.
        self.gate.forget_totp(ended)
        event(log, logging.INFO, "admin_change", kind="session_end", client=request.client)
        return Response(204)

    def _pin_status(self, _request: Request) -> Response:
        return _json(200, dataclasses.asdict(self._pin.status()))

    def _pin_change(self, request: Request) -> Response:
        payload = _object(request) or {}
        mode = payload.get("mode")
        new_pin = payload.get("pin")
        based_on = payload.get("based_on")
        if (
            not isinstance(mode, str)
            or not isinstance(based_on, str)
            or not (new_pin is None or isinstance(new_pin, str))
        ):
            return _error(400, "bad_request")
        try:
            status = self._pin.change(mode, new_pin, based_on)
        except PinStale as error:
            return _json(409, {"error": "stale", "revision": error.current})
        except PinError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        # Not in the history: a restore applies a whole configuration, and a
        # PIN does not belong in one. This line is the record.
        event(
            log,
            logging.INFO,
            "admin_change",
            kind="pin",
            mode=mode,
            new_pin=new_pin is not None,
            client=request.client,
        )
        return _json(200, dataclasses.asdict(status))

    def _static(self, relative: str) -> Response:
        target = inside(self._web, urllib.parse.unquote(relative) or "index.html")
        content_type = _TYPES.get(target.suffix) if target else None
        if target is None or content_type is None:
            return _error(404, "not_found")
        return Response(200, target.read_bytes(), content_type)


def _body(request: Request, keys: tuple[str, ...]) -> dict[str, str] | None:
    """The JSON object with the string fields named, or None."""
    try:
        payload: object = json.loads(request.body or b"null")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    fields = cast("dict[str, Any]", payload)
    if not all(isinstance(fields.get(key), str) for key in keys):
        return None
    return {key: cast(str, fields[key]) for key in keys}


def _object(request: Request) -> dict[str, object] | None:
    try:
        payload: object = json.loads(request.body or b"null")
    except ValueError:
        return None
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def _version(payload: dict[str, object] | None) -> int | None:
    """based_on: an int, and not a bool (which is an int in Python)."""
    value = payload.get("based_on") if payload else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _history_id(path: str) -> int:
    return int(path.removeprefix("/admin/api/history/").split("/")[0])
