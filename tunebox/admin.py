"""The admin panel's HTTP side: its pages, its JSON API under /admin/api/,
and the rules every admin request passes. The server hands every /admin
request here, so the panel's pages are never reachable through the player's
static files."""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tunebox import crypto
from tunebox.accounts import ABSOLUTE_S, AccountError, Accounts, Session
from tunebox.config import (
    CODECS,
    DEFAULTS,
    SOURCE_CHOICES,
    SOURCE_SETTINGS,
    AdminConfig,
    ConfigError,
    ConfigState,
)
from tunebox.limits import FailureLimit, Pace, group
from tunebox.logs import event
from tunebox.rulecheck import check, lint
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
# A save sends the whole station list: about 40 kB for 50 stations. The
# limit is chosen by path before any session check, so anyone may send this
# much to that one route; the proxy's body limit bounds it in front.
MAX_STATE_BODY = 524_288
SESSION_COOKIE = "tunebox_admin"
KNOWN_COOKIE = "tunebox_known"
KNOWN_S = 365 * 86_400
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
    headers: Mapping[str, str]
    body: bytes
    client: str
    query: str = ""

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == wanted), None)

    def cookie(self, name: str) -> str | None:
        """Read pair by pair: SimpleCookie drops every cookie when one
        cookie of another app on the host is malformed."""
        for pair in (self.header("Cookie") or "").split(";"):
            key, sep, value = pair.strip().partition("=")
            if sep and key == name:
                return value
        return None


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = JSON
    headers: tuple[tuple[str, str], ...] = ()


def body_limit(path: str) -> int:
    """The largest body a POST to path may send."""
    return MAX_STATE_BODY if path == "/admin/api/config" else MAX_BODY


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
        apply: Callable[[], int] = lambda: 0,
        samples: Callable[[], list[str]] = list,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """apply reloads the running channels after a change; samples gives
        recent titles that new title rules are tried on."""
        self._accounts = accounts
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
        # A grant read on the network over plain http must not give a lane
        # once the panel moves to https.
        self._grant_version = "h1" if self.plain_http else "k1"
        if self.plain_http:
            event(log, logging.WARNING, "admin_plain_http", origin=config.public_origin)
        self._web = web_dir.resolve()
        self._clock = clock
        self._monotonic = monotonic
        self._failures = FailureLimit(5, window_s=900, lock_s=900, clock=monotonic)
        self._pace = Pace(1.0, clock=monotonic)
        self._known_pace = Pace(1.0, clock=monotonic)
        # scrypt takes about 33 MB and 0.1 s; more at once is a way to run
        # tunebox out of memory.
        self._hashing = threading.BoundedSemaphore(2)
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
                expires_in_s=SETUP_TOKEN_S,
            )

    def handle(self, request: Request) -> Response:
        response = self._dispatch(request)
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
        return handler(request)

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
        }
        if (method, name) in fixed:
            return fixed[(method, name)]
        parts = name.split("/")
        if parts[0] != "history" or not parts[1:2] or not parts[1].isdigit():
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
            {"account": self._accounts.exists(), "setup": self._setup_open(), "session": detail},
        )

    def _login(self, request: Request) -> Response:
        body = _body(request, ("password", "code"))
        if body is None:
            return _error(400, "bad_request")
        # The address lock first: a locked address must not spend the
        # shared pace, or one host could keep everyone else out.
        release = self._failures.reserve(group(request.client))
        if release is None:
            return self._limited(request, "address")
        known = self._known(request)
        if not (self._known_pace if known else self._pace).allow():
            release()
            return self._limited(request, "pace")
        if not self._hashing.acquire(blocking=False):
            release()
            return self._limited(request, "busy")
        try:
            user_agent = request.header("User-Agent") or ""
            outcome = self._accounts.login(
                body["password"], body["code"], request.client, user_agent
            )
        finally:
            self._hashing.release()
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
        expiry = int(self._clock() + KNOWN_S)
        grant = crypto.sign(
            self._accounts.server_key(),
            f"{self._grant_version}.{self._accounts.generation()}.{expiry}",
        )
        return _json(
            200,
            {"csrf": outcome.csrf},
            self._cookie(self.session_cookie, outcome.token or "", ABSOLUTE_S),
            self._cookie(self.known_cookie, grant, KNOWN_S),
        )

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
        return Response(204, headers=(self._cookie(self.session_cookie, "", 0),))

    def _known(self, request: Request) -> bool:
        """A browser that logged in before, by a signed cookie naming the
        current account generation; it gets its own lane in the pace."""
        value = request.cookie(self.known_cookie)
        payload = crypto.unsign(self._accounts.server_key(), value) if value else None
        if payload is None:
            return False
        try:
            version, generation, expiry = payload.split(".")
            current = int(generation) == self._accounts.generation()
            return version == self._grant_version and current and int(expiry) > self._clock()
        except ValueError:
            return False

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
        release = self._failures.reserve(group(request.client))
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
        if len(body["password"]) < crypto.MIN_PASSWORD:
            return _error(400, "password_too_short")
        try:
            body["password"].encode()
        except UnicodeEncodeError:  # a lone surrogate from a JSON escape
            return _error(400, "bad_request")
        step = crypto.matching_step(secret, body["code"].strip(), self._clock(), -1)
        if step is None:
            return _error(400, "code_wrong")
        codes = crypto.new_recovery_codes()
        try:
            self._accounts.create(body["password"], secret, codes, step)
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
        return self._commit(
            payload.get("state"), based_on, "edit", target if isinstance(target, str) else None
        )

    def _commit(self, raw: object, based_on: int, kind: str, target: str | None) -> Response:
        try:
            state = state_from_dict(raw)
            refused = self._refused_rule(state)
            if refused:
                return _json(400, {"error": "invalid", "message": refused})
            version = self._settings.commit(state, based_on, kind, target)
        except ConfigError as error:
            return _json(400, {"error": "invalid", "message": str(error)})
        except StaleVersion as error:
            return _json(409, {"error": "stale", "version": error.current})
        self._apply()
        event(log, logging.INFO, "admin_change", kind=kind, target=target, version=version)
        return _json(200, {"version": version})

    def _refused_rule(self, state: ConfigState) -> str | None:
        """Lints every rule, and runs the ones not stored yet in a child
        process: stored patterns have already run on live titles."""
        current, _ = self._settings.load()
        known = {pattern for row in current.channels for _, pattern in _patterns(row.doc)}
        new: dict[str, str] = {}
        for index, row in enumerate(state.channels):
            for key, pattern in _patterns(row.doc):
                field = f"channels[{index}].rules.{key}"
                reason = lint(pattern)
                if reason:
                    return f"{field}: {reason}"
                if pattern not in known:
                    new.setdefault(pattern, field)
        if not new:
            return None
        patterns = list(new)
        refused = check(patterns, self._samples())
        if refused is None:
            return None
        at, reason = refused
        return f"{new[patterns[at]] if at is not None else 'rules'}: {reason}"

    def _history(self, request: Request) -> Response:
        before = urllib.parse.parse_qs(request.query).get("before", [""])[0]
        entries, more = self._settings.history(int(before) if before.isdigit() else None)
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

    def _static(self, relative: str) -> Response:
        relative = urllib.parse.unquote(relative) or "index.html"
        target = self._web
        try:
            target = (self._web / relative).resolve()
            inside = target.is_relative_to(self._web) and target.is_file()
        except OSError, ValueError:  # ValueError: an embedded NUL byte
            inside = False
        content_type = _TYPES.get(target.suffix) if inside else None
        if content_type is None:
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


def _patterns(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """The title rules a station document sets, as (key, pattern)."""
    rules = doc.get("rules")
    if not isinstance(rules, dict):
        return []
    table = cast("dict[str, object]", rules)
    found = ((key, table.get(key)) for key in ("station_id", "played_by"))
    return [(key, value) for key, value in found if isinstance(value, str) and value]


def _history_id(path: str) -> int:
    return int(path.removeprefix("/admin/api/history/").split("/")[0])
