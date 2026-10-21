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
from typing import Any, Literal, cast, overload

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
    state_from_import,
    state_to_toml,
    validate_state,
)
from tunebox.http_headers import single
from tunebox.logs import event
from tunebox.paths import inside
from tunebox.pin import PIN_MAX, PIN_MIN, Pin, PinError, PinStale, read_cookie
from tunebox.rulecheck import lint_all, refused
from tunebox.sessions import KNOWN_S, Gate
from tunebox.settings import (
    Settings,
    SettingsUnavailable,
    SettingsWriteError,
    StaleVersion,
    state_from_dict,
    state_to_json,
)
from tunebox.tomlout import dumps

log = logging.getLogger(__name__)

JSON = "application/json; charset=utf-8"
MAX_BODY = 65_536
# A save sends the whole station list: about 40 kB for 50 stations. Granted
# only to a request whose headers already pass the session checks, before
# its body is read.
MAX_STATE_BODY = 524_288
# Longest history target a save may carry.
MAX_TARGET = 200
LARGE_ROUTES = {
    "config": MAX_STATE_BODY,
    "import/preview": MAX_STATE_BODY,
    "import": MAX_STATE_BODY,
}
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
        return single(self.headers, name)

    def cookie(self, name: str) -> str | None:
        return read_cookie(self.header("Cookie"), name)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = JSON
    headers: tuple[tuple[str, str], ...] = ()


_UINT = re.compile(r"[0-9]{1,18}")
# Bounded so int() never meets its 4300-digit limit; no real body is longer.
_LENGTH = re.compile(r"[0-9]{1,18}")


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
            # Caught here so the 500 carries ADMIN_HEADERS like every other
            # admin answer; logged inside the admin context, so the formatter
            # withholds the exception text.
            log.error("request_crashed", exc_info=True, extra={"fields": {"path": request.path}})
            response = _error(500, "internal")
        return _with_admin_headers(response)

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
        name = request.path.removeprefix("/admin/api/")
        open_handler = self._open_route(request.method, name)
        if open_handler is not None:
            refused = self._refusal(request, needs_session=False)
            return refused if isinstance(refused, Response) else open_handler(request)
        session_handler = self._session_route(request.method, name)
        if session_handler is None:
            return _error(404, "not_found")
        checked = self._refusal(request, needs_session=True)
        if isinstance(checked, Response):
            return checked
        return session_handler(request, checked)

    def body_limit(self, request: Request) -> tuple[int, Response | None]:
        """The largest body this POST may send, judged on its headers alone
        (the body is not read yet), and the answer to give instead of
        reading a body over MAX_BODY. Only a route that takes a large body
        grants one, to a live session with its token and the right origin;
        any other request over MAX_BODY gets the refusal its handler would
        have given, so the page can tell an ended session from a size."""
        large = LARGE_ROUTES.get(request.path.removeprefix("/admin/api/"))
        declared = content_length(request.header("Content-Length"))
        if (
            request.method != "POST"
            or not request.path.startswith("/admin/api/")
            or large is None
            or declared is None
            or declared <= MAX_BODY
        ):
            # Within MAX_BODY the handler's own checks judge it after the read.
            return MAX_BODY, None
        try:
            refused = self._refusal(request, needs_session=True)
        except SettingsUnavailable:
            refused = _error(503, "read_only")
        if isinstance(refused, Session):
            return large, None
        return MAX_BODY, _with_admin_headers(refused)

    @overload
    def _refusal(self, request: Request, needs_session: Literal[True]) -> Session | Response: ...

    @overload
    def _refusal(self, request: Request, needs_session: Literal[False]) -> Response | None: ...

    def _refusal(self, request: Request, needs_session: bool) -> Session | Response | None:
        """The origin, content type, session and token checks a request must
        pass before its handler runs: the refusal, or the session a route
        that needs one passed with (None for a route without a session that
        passes). A request body_limit refuses never reaches _api, so a
        rejection is logged once."""
        if request.method == "POST":
            if not self._origin or request.header("Origin") != self._origin:
                event(
                    log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="origin"
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
                event(
                    log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="token"
                )
                return _error(403, "bad_csrf")
            return session
        return None

    def _open_route(self, method: str, name: str) -> Callable[[Request], Response] | None:
        """The handler of a route that works without a session."""
        fixed: dict[tuple[str, str], Callable[[Request], Response]] = {
            ("GET", "state"): self._state,
            ("POST", "login"): self._login,
            ("POST", "logout"): self._logout,
            ("POST", "setup/start"): self._setup_start,
            ("POST", "setup/finish"): self._setup_finish,
        }
        return fixed.get((method, name))

    def _session_route(
        self, method: str, name: str
    ) -> Callable[[Request, Session], Response] | None:
        """The handler of a route that needs a session; it is handed the
        session the checks found."""
        fixed: dict[tuple[str, str], Callable[[Request, Session], Response]] = {
            ("GET", "config"): self._config,
            ("POST", "config"): self._save,
            ("GET", "history"): self._history,
            ("GET", "export"): self._export,
            ("POST", "import/preview"): self._import_preview,
            ("POST", "import"): self._import,
            ("GET", "account"): self._account,
            ("POST", "account/reauth"): self._reauth,
            ("POST", "account/password"): self._password,
            ("POST", "account/totp/start"): self._totp_start,
            ("POST", "account/totp/finish"): self._totp_finish,
            ("POST", "account/recovery"): self._recovery,
            ("GET", "pin"): self._pin_status,
            ("POST", "pin"): self._pin_change,
        }
        if (method, name) in fixed:
            return fixed[(method, name)]
        parts = name.split("/")
        if parts[:2] == ["account", "sessions"] and len(parts) == 4 and parts[3] == "end":
            return self._end_session if method == "POST" else None
        if parts[0] != "history" or not parts[1:2] or _uint(parts[1]) is None:
            return None
        if len(parts) == 2 and method == "GET":
            return self._history_entry
        if parts[2:] == ["restore"] and method == "POST":
            return self._restore
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
        try:
            known = self._known(request)
        except SettingsUnavailable:
            release()
            raise
        if not self.gate.pace(known):
            release()
            return self._limited(request, "pace")
        started = self._monotonic()
        took = False
        try:
            with self.gate.hashing() as took:
                if not took:
                    release()
                    return self._limited(request, "busy")
                user_agent = request.header("User-Agent") or ""
                try:
                    outcome = self._accounts.login(
                        body["password"], body["code"], request.client, user_agent
                    )
                except SettingsUnavailable:
                    # Only the account read ahead of the password check
                    # raises this: it says nothing about the credentials, so
                    # it is not a failure of this address. Any other error
                    # keeps the reservation.
                    release()
                    raise
            return self._login_answer(request, outcome, release)
        finally:
            if took:
                self.gate.floor(started)

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
        secret = self.gate.setup_secret()
        # Anyone holding the token gets the same secret; this line shows
        # every fetch, so one from an unexpected address stands out.
        event(log, logging.WARNING, "admin_setup_started", client=request.client)
        return _json(200, {"secret": crypto.secret_text(secret), "uri": crypto.otpauth_uri(secret)})

    def _setup_finish(self, request: Request) -> Response:
        body = _body(request, ("token", "password", "code"))
        if body is None:
            return _error(400, "bad_request")
        secret = self.gate.setup_secret_shown()
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
        self.gate.end_setup()
        event(log, logging.WARNING, "admin_account_created", client=request.client)
        return _json(200, {"recovery_codes": codes})

    def _config(self, _request: Request, _session: Session) -> Response:
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

    def _save(self, request: Request, _session: Session) -> Response:
        payload = _object(request)
        based_on = _version(payload)
        if payload is None or based_on is None:
            return _error(400, "bad_request")
        target = payload.get("target")
        if isinstance(target, str) and len(target) > MAX_TARGET:
            return _error(400, "bad_request")
        return self._commit(
            payload.get("state"),
            based_on,
            "edit",
            target if isinstance(target, str) else None,
            request.client,
        )

    def _commit(
        self, raw: object, based_on: int, kind: str, target: str | None, client: str
    ) -> Response:
        try:
            state = state_from_dict(raw)
            validate_state(state)
            stored, version = self._settings.load()
            if version != based_on:
                return _json(409, {"error": "stale", "version": version})
            reason = refused(state, stored, self._samples)
            if reason:
                return _json(400, {"error": "invalid", "message": reason})
            version, changed = self._settings.commit_changed(state, based_on, kind, target)
        except ConfigError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        except StaleVersion as error:
            return _json(409, {"error": "stale", "version": error.current})
        # An unchanged save still asks for the apply: after a failed one the
        # stored version is ahead of the live one, and this is the retry.
        applied = self._apply() >= version
        if not changed:
            return _json(200, {"version": version, "changed": False, "applied": applied})
        if not applied:
            event(log, logging.WARNING, "admin_change_not_live", client=client, version=version)
        event(
            log,
            logging.INFO,
            "admin_change",
            client=client,
            kind=kind,
            target=target,
            version=version,
        )
        return _json(200, {"version": version, "changed": True, "applied": applied})

    def _history(self, request: Request, _session: Session) -> Response:
        raw = urllib.parse.parse_qs(request.query).get("before", [""])[0]
        before = _uint(raw) if raw else None
        if raw and before is None:
            return _error(400, "bad_request")
        entries, more = self._settings.history(before)
        return _json(200, {"entries": [dataclasses.asdict(e) for e in entries], "more": more})

    def _export(self, _request: Request, _session: Session) -> Response:
        state, version = self._settings.load()
        day = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        text = f"# tunebox configuration, version {version}\n" + dumps(state_to_toml(state))
        return Response(
            200,
            text.encode(),
            "application/toml; charset=utf-8",
            (("Content-Disposition", f'attachment; filename="tunebox-{day}.toml"'),),
        )

    def _import_preview(self, request: Request, _session: Session) -> Response:
        body = _body(request, ("toml",))
        if body is None:
            return _error(400, "bad_request")
        try:
            state, notes = state_from_import(body["toml"])
            validate_state(state)
        except ConfigError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        reason = lint_all(state)
        if reason:
            return _json(400, {"error": "invalid", "message": reason})
        return _json(200, {"state": json.loads(state_to_json(state)), "notes": notes})

    def _import(self, request: Request, session: Session) -> Response:
        payload = _object(request)
        based_on = _version(payload)
        text = payload.get("toml") if payload else None
        if based_on is None or not isinstance(text, str):
            return _error(400, "bad_request")
        stale = self._fresh(session)
        if stale is not None:
            return stale
        try:
            state, _notes = state_from_import(text)
        except ConfigError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        return self._commit(
            json.loads(state_to_json(state)), based_on, "import", "panel", request.client
        )

    def _history_entry(self, request: Request, _session: Session) -> Response:
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

    def _restore(self, request: Request, _session: Session) -> Response:
        based_on = _version(_object(request))
        if based_on is None:
            return _error(400, "bad_request")
        entry_id = _history_id(request.path)
        found = self._settings.history_entry(entry_id)
        if found is None:
            return _error(404, "not_found")
        _, before, _ = found
        raw = json.loads(state_to_json(before))
        return self._commit(raw, based_on, "restore", f"history {entry_id}", request.client)

    def _fresh(self, session: Session) -> Response | None:
        """The answer to give when the session has not typed a code
        recently, else None."""
        if not self._accounts.fresh(session):
            return _error(403, "reauth_required")
        return None

    def _account(self, _request: Request, session: Session) -> Response:
        mine = session_id(session.token_hash)
        listed = [
            {**dataclasses.asdict(s), "current": s.id == mine} for s in self._accounts.sessions()
        ]
        return _json(200, {"sessions": listed, "fresh": self._accounts.fresh(session)})

    def _reauth(self, request: Request, session: Session) -> Response:
        body = _body(request, ("code",))
        if body is None:
            return _error(400, "bad_request")
        release = self.gate.reserve(request.client)
        if release is None:
            # Not why=address, which the CrowdSec ban scenario counts: this
            # is someone with a session typing codes wrong.
            return self._limited(request, "reauth")
        try:
            reason = self._accounts.reauth(session, body["code"])
        except SettingsUnavailable:
            # The account read ahead of the code check; no code was judged.
            release()
            raise
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

    def _password(self, request: Request, session: Session) -> Response:
        body = _body(request, ("password",))
        if body is None:
            return _error(400, "bad_request")
        stale = self._fresh(session)
        if stale is not None:
            return stale
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

    def _totp_start(self, _request: Request, session: Session) -> Response:
        stale = self._fresh(session)
        if stale is not None:
            return stale
        secret = crypto.new_totp_secret()
        self.gate.hold_totp(session.token_hash, secret)
        return _json(200, {"secret": crypto.secret_text(secret), "uri": crypto.otpauth_uri(secret)})

    def _totp_finish(self, request: Request, session: Session) -> Response:
        body = _body(request, ("code",))
        if body is None:
            return _error(400, "bad_request")
        stale = self._fresh(session)
        if stale is not None:
            return stale
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
        except AccountError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        # Every session but this one has ended; their waiting secrets too.
        self.gate.drop_totp(keep=())
        event(log, logging.INFO, "admin_change", kind="totp", client=request.client)
        return Response(204, headers=(self._known_grant(generation),))

    def _recovery(self, request: Request, session: Session) -> Response:
        stale = self._fresh(session)
        if stale is not None:
            return stale
        try:
            codes, generation = self._accounts.replace_recovery_codes(session)
        except SessionEnded:
            return _error(401, "login_required")
        except AccountError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        event(log, logging.INFO, "admin_change", kind="recovery_codes", client=request.client)
        return _json(200, {"recovery_codes": codes}, self._known_grant(generation))

    def _end_session(self, request: Request, session: Session) -> Response:
        wanted = request.path.removeprefix("/admin/api/account/sessions/").split("/")[0]
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

    def _pin_status(self, _request: Request, _session: Session) -> Response:
        return _json(200, dataclasses.asdict(self._pin.status()))

    def _pin_change(self, request: Request, _session: Session) -> Response:
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


def _with_admin_headers(response: Response) -> Response:
    return Response(
        response.status,
        response.body,
        response.content_type,
        (*response.headers, *ADMIN_HEADERS.items()),
    )


def content_length(value: str | None) -> int | None:
    """A Content-Length header's value; None unless it is 1 to 18 ASCII digits."""
    return int(value) if value is not None and _LENGTH.fullmatch(value) else None


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
