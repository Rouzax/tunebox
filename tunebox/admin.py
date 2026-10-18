"""The admin panel's HTTP side: its pages, its JSON API under /admin/api/,
and the rules every admin request passes. The server hands every /admin
request here, so the panel's pages are never reachable through the player's
static files."""

from __future__ import annotations

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
from tunebox.config import AdminConfig
from tunebox.limits import FailureLimit, Pace, group
from tunebox.logs import event
from tunebox.settings import SettingsUnavailable, SettingsWriteError

log = logging.getLogger(__name__)

JSON = "application/json; charset=utf-8"
MAX_BODY = 65_536
SESSION_COOKIE = "__Secure-tunebox_admin"
KNOWN_COOKIE = "__Secure-tunebox_known"
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


def _cookie(name: str, value: str, max_age: int) -> tuple[str, str]:
    attributes = f"Path=/admin; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"
    return ("Set-Cookie", f"{name}={value}; {attributes}")


class Admin:
    def __init__(
        self,
        accounts: Accounts,
        config: AdminConfig,
        web_dir: Path,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._accounts = accounts
        self._origin = config.public_origin
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
        name = request.path.removeprefix("/admin/api/")
        routes: dict[tuple[str, str], Callable[[Request], Response]] = {
            ("GET", "state"): self._state,
            ("POST", "login"): self._login,
            ("POST", "logout"): self._logout,
            ("POST", "setup/start"): self._setup_start,
            ("POST", "setup/finish"): self._setup_finish,
        }
        route = routes.get((request.method, name))
        if route is None:
            return _error(404, "not_found")
        if request.method == "POST":
            if not self._origin or request.header("Origin") != self._origin:
                event(
                    log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="origin"
                )
                return _error(403, "bad_origin")
            content_type = (request.header("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                return _error(415, "json_only")
        return route(request)

    def _session(self, request: Request) -> Session | None:
        token = request.cookie(SESSION_COOKIE)
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
            self._accounts.server_key(), f"k1.{self._accounts.generation()}.{expiry}"
        )
        return _json(
            200,
            {"csrf": outcome.csrf},
            _cookie(SESSION_COOKIE, outcome.token or "", ABSOLUTE_S),
            _cookie(KNOWN_COOKIE, grant, KNOWN_S),
        )

    def _logout(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            # Already logged out (or expired): nothing to protect.
            return Response(204, headers=(_cookie(SESSION_COOKIE, "", 0),))
        given = request.header("X-Tunebox-CSRF") or ""
        if not _same(given, session.csrf):
            event(log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="token")
            return _error(403, "bad_csrf")
        self._accounts.logout(request.cookie(SESSION_COOKIE) or "")
        return Response(204, headers=(_cookie(SESSION_COOKIE, "", 0),))

    def _known(self, request: Request) -> bool:
        """A browser that logged in before, by a signed cookie naming the
        current account generation; it gets its own lane in the pace."""
        value = request.cookie(KNOWN_COOKIE)
        payload = crypto.unsign(self._accounts.server_key(), value) if value else None
        if payload is None:
            return False
        try:
            version, generation, expiry = payload.split(".")
            current = int(generation) == self._accounts.generation()
            return version == "k1" and current and int(expiry) > self._clock()
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
