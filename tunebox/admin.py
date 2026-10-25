"""The admin panel's HTTP side: its pages, its JSON API under /admin/api/,
and the rules every admin request passes. The server hands every /admin
request here, so the panel's pages are never reachable through the player's
static files."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import math
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
from tunebox.blobs import MAX_BLOB, BlobError, Blobs, BlobStoreFull, BlobUnavailable
from tunebox.config import (
    CODECS,
    DEFAULTS,
    SOURCE_CHOICES,
    SOURCE_SETTINGS,
    AdminConfig,
    ConfigError,
    ConfigState,
    encodable,
    keeps_a_channel,
    state_from_import,
    state_to_toml,
    validate_state,
)
from tunebox.disk import has_room
from tunebox.http_headers import single
from tunebox.limits import Once, network
from tunebox.logos import CONTENT_TYPES
from tunebox.logs import event
from tunebox.paths import inside
from tunebox.pin import PIN_MAX, PIN_MIN, Pin, PinError, PinStale, read_cookie
from tunebox.rulecheck import check_new, lint_new
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
from tunebox.usage import Usage, UsageSettings, UsageUnavailable
from tunebox.usagecore import NAME_MAX, RETENTION_MAX, RETENTION_MIN, valid_device

log = logging.getLogger(__name__)

JSON = "application/json; charset=utf-8"
MAX_BODY = 65_536
# A save sends the whole station list: about 40 kB for 50 stations. Granted
# only to a request whose headers already pass the session checks, before
# its body is read.
MAX_STATE_BODY = 524_288
# Longest history target a save may carry.
MAX_TARGET = 200
# The image types a logo upload may declare; the bytes decide the kind.
UPLOAD_TYPES = ("image/png", "image/jpeg", "image/webp", "image/svg+xml")
JSON_ONLY = ("application/json",)
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


def _matches(template: str, name: str) -> bool:
    """Whether a path below /admin/api/ fits a route template: {id} is a
    history id, {part} one non-empty segment."""
    want, got = template.split("/"), name.split("/")
    if len(want) != len(got):
        return False
    for part, given in zip(want, got, strict=True):
        if part == "{id}":
            if _uint(given) is None:
                return False
        elif part == "{part}":
            if not given:
                return False
        elif part != given:
            return False
    return True


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


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


def _exposed(stored: ConfigState, state: ConfigState) -> list[str]:
    """The stations the PIN hides now that stay in state without needs_pin:
    importing a tunebox.toml that never set it would show them to every
    device. A station the state removes is not exposed."""
    hidden = {row.id for row in stored.channels if row.doc.get("needs_pin") is True}
    return [
        row.id
        for row in state.channels
        if row.id in hidden and row.doc.get("needs_pin") is not True
    ]


def _invalid(message: str, state: ConfigState | None) -> Response:
    """A refused state. The server names fields by position
    (channels[3].streams[0].url); the station's id next to it says which
    one that is."""
    if state is not None:

        def named(found: re.Match[str]) -> str:
            index = int(found.group(1))
            if index >= len(state.channels):
                return found.group(0)
            return f"{found.group(0)} ({state.channels[index].id})"

        message = re.sub(r"channels\[(\d+)\]", named, message)
    return _json(400, {"error": "invalid", "message": message})


@dataclass(frozen=True)
class Route:
    """What a request must bring before its handler runs. Exactly one of
    open (no session) and run (handed the checked session) is set."""

    run: Callable[[Request, Session], Response] | None = None
    open: Callable[[Request], Response] | None = None
    # The largest body, granted on the headers alone before it is read.
    body: int = MAX_BODY
    types: tuple[str, ...] = JSON_ONLY
    # A fresh code (re-auth) before the handler runs.
    code: bool = False

    def __post_init__(self) -> None:
        if (self.run is None) == (self.open is None):
            raise ValueError("a route sets exactly one of run and open")


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
        blobs: Blobs,
        free_space: Callable[[], int | None],
        usage: Usage | None = None,
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
        self._blobs = blobs
        self._free = free_space
        self._usage = usage
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
        self._lockouts = Once(1.0, monotonic)
        self.gate = Gate(accounts.server_key, self.plain_http, clock, monotonic, sleep)
        self._setup_token: str | None = None
        self._setup_until = 0.0
        self._spent: bytes | None = None
        self.routes: dict[tuple[str, str], Route] = {
            ("GET", "state"): Route(open=self._state),
            ("POST", "login"): Route(open=self._login),
            ("POST", "logout"): Route(open=self._logout),
            ("POST", "setup/start"): Route(open=self._setup_start),
            ("POST", "setup/finish"): Route(open=self._setup_finish),
            ("GET", "config"): Route(self._config),
            ("POST", "config"): Route(self._save, body=MAX_STATE_BODY),
            ("GET", "history"): Route(self._history),
            ("GET", "history/{id}"): Route(self._history_entry),
            ("POST", "history/{id}/restore"): Route(self._restore),
            ("GET", "export"): Route(self._export),
            ("POST", "import/preview"): Route(self._import_preview, body=MAX_STATE_BODY),
            ("POST", "import"): Route(self._import, body=MAX_STATE_BODY, code=True),
            ("POST", "logos"): Route(self._upload, body=MAX_BLOB, types=UPLOAD_TYPES),
            ("GET", "logos/{part}"): Route(self._blob),
            ("GET", "account"): Route(self._account),
            ("POST", "account/reauth"): Route(self._reauth),
            ("POST", "account/password"): Route(self._password, code=True),
            ("POST", "account/totp/start"): Route(self._totp_start, code=True),
            ("POST", "account/totp/finish"): Route(self._totp_finish, code=True),
            ("POST", "account/recovery"): Route(self._recovery, code=True),
            ("POST", "account/sessions/{part}/end"): Route(self._end_session),
            ("GET", "pin"): Route(self._pin_status),
            ("POST", "pin"): Route(self._pin_change, code=True),
            ("GET", "usage/live"): Route(self._using(self._usage_live)),
            ("GET", "usage/summary"): Route(self._using(self._usage_summary)),
            ("GET", "usage/devices"): Route(self._using(self._usage_devices)),
            ("GET", "usage/device"): Route(self._using(self._usage_device)),
            ("POST", "usage/device/name"): Route(self._using(self._usage_rename)),
            ("POST", "usage/device/delete"): Route(self._using(self._usage_delete)),
            ("GET", "usage/settings"): Route(self._using(self._usage_settings)),
            # A fresh code only when the change turns addresses on, so the
            # check is in the handler, not code=True.
            ("POST", "usage/settings"): Route(self._using(self._usage_change)),
        }
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
            except UsageUnavailable:
                return _error(503, "usage_unavailable")
        if request.method not in ("GET", "HEAD"):
            return _error(405, "method_not_allowed")
        return self._static(request.path.removeprefix("/admin/"))

    def route(self, method: str, name: str) -> Route | None:
        """The route a method and a path below /admin/api/ name, if any."""
        for (wanted, template), found in self.routes.items():
            if wanted == method and _matches(template, name):
                return found
        return None

    def _api(self, request: Request) -> Response:
        route = self.route(request.method, request.path.removeprefix("/admin/api/"))
        if route is None:
            return _error(404, "not_found")
        if route.run is None:
            assert route.open is not None
            refused = self._screen(request, route)
            return refused if refused is not None else route.open(request)
        checked = self._refusal(request, route)
        if isinstance(checked, Response):
            return checked
        if route.code and not self._accounts.fresh(checked):
            return _error(403, "reauth_required")
        return route.run(request, checked)

    def body_limit(self, request: Request) -> tuple[int, Response | None]:
        """The largest body this POST may send, judged on its headers alone
        (the body is not read yet), and the answer to give instead of
        reading a body over MAX_BODY. Only a route that takes a large body
        grants one, to a live session with its token and the right origin;
        any other request over MAX_BODY gets the refusal its handler would
        have given, so the page can tell an ended session from a size."""
        route = self.route("POST", request.path.removeprefix("/admin/api/"))
        declared = content_length(request.header("Content-Length"))
        if (
            request.method != "POST"
            or not request.path.startswith("/admin/api/")
            or route is None
            or route.run is None
            or declared is None
            or declared <= MAX_BODY
        ):
            # Within MAX_BODY the handler's own checks judge it after the read.
            return MAX_BODY, None
        try:
            refused = self._refusal(request, route)
        except SettingsUnavailable:
            refused = _error(503, "read_only")
        if isinstance(refused, Response):
            return MAX_BODY, _with_admin_headers(refused)
        if route.code and not self._accounts.fresh(refused):
            return MAX_BODY, _with_admin_headers(_error(403, "reauth_required"))
        return route.body, None

    def _screen(self, request: Request, route: Route) -> Response | None:
        """The origin and content type checks every POST must pass; a
        rejection is logged once."""
        if request.method != "POST":
            return None
        if not self._origin or request.header("Origin") != self._origin:
            event(log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="origin")
            return _error(403, "bad_origin")
        content_type = (request.header("Content-Type") or "").split(";")[0].strip()
        if content_type not in route.types:
            return _error(415, "json_only" if route.types == JSON_ONLY else "unsupported_type")
        return None

    def _refusal(self, request: Request, route: Route) -> Session | Response:
        """The checks a request must pass before the handler of a route that
        needs a session runs: the refusal, or the session found. A request
        body_limit refuses never reaches _api, so a rejection is logged
        once."""
        refused = self._screen(request, route)
        if refused is not None:
            return refused
        session = self._session(request)
        if session is None:
            return _error(401, "login_required")
        given = request.header("X-Tunebox-CSRF") or ""
        if request.method == "POST" and not _same(given, session.csrf):
            event(log, logging.WARNING, "admin_csrf_rejected", client=request.client, why="token")
            return _error(403, "bad_csrf")
        return session

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
                    "logo_max": MAX_BLOB,
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
                range=network(request.client),
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
        self._log_limited(request, why)
        return _error(429, "slow_down")

    def _log_limited(self, request: Request, why: str) -> None:
        # An address that is locked out keeps trying at its own pace; one
        # WARNING a second per address group, the rest at DEBUG.
        quiet = why == "address" and not self._lockouts.first(request.client)
        event(
            log,
            logging.DEBUG if quiet else logging.WARNING,
            "admin_rate_limited",
            client=request.client,
            range=network(request.client),
            why=why,
        )

    def _setup_open(self) -> bool:
        return (
            self._setup_token is not None
            and self._monotonic() < self._setup_until
            and not self._accounts.exists()
        )

    def _is_spent(self, token: str) -> bool:
        """The token of a setup that finished: its retry is not an attack."""
        return (
            self._spent is not None
            and hmac.compare_digest(self._spent, _digest(token))
            and self._accounts.exists()
        )

    def _setup_allowed(self, request: Request, token: str) -> bool:
        """A wrong token counts as a failed login for the address."""
        release = self.gate.reserve(request.client)
        if release is None:
            # Answered like a wrong token, so the refusal tells nothing about
            # the lockout; logged as the login path logs it.
            self._log_limited(request, "address")
            return False
        if self._setup_open() and _same(token, self._setup_token or ""):
            release()
            return True
        event(
            log,
            logging.WARNING,
            "admin_setup_token_rejected",
            client=request.client,
            range=network(request.client),
        )
        return False

    def _setup_start(self, request: Request) -> Response:
        body = _body(request, ("token",))
        if body is None:
            return _error(400, "bad_request")
        if self._is_spent(body["token"]):
            return _error(409, "already_set_up")
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
        if self._is_spent(body["token"]):
            return _error(409, "already_set_up")
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
        self._spent = _digest(body["token"])
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

    def _save(self, request: Request, session: Session) -> Response:
        payload = _object(request)
        based_on = _version(payload)
        if payload is None or based_on is None:
            return _error(400, "bad_request")
        target = payload.get("target")
        if isinstance(target, str) and (len(target) > MAX_TARGET or not encodable(target)):
            return _error(400, "bad_request")
        try:
            state = state_from_dict(payload.get("state"))
        except ConfigError as error:
            return _invalid(str(error), None)
        return self._commit(
            state,
            based_on,
            "edit",
            target if isinstance(target, str) else None,
            request.client,
            session,
        )

    def _judge(self, state: ConfigState, stored: ConfigState) -> str | None:
        """Why state may not replace stored, short of the timed rule check,
        or None. The import preview and every commit ask this one question,
        so a file the preview accepts meets no other refusal when it is
        imported than a stale version or a slow new pattern."""
        try:
            validate_state(state)
            keeps_a_channel(stored, state)
        except ConfigError as error:
            return str(error)
        return lint_new(state, stored) or self._blobs.missing(state)

    def _commit(
        self,
        state: ConfigState,
        based_on: int,
        kind: str,
        target: str | None,
        client: str,
        session: Session,
    ) -> Response:
        try:
            stored, version = self._settings.load()
            reason = self._judge(state, stored)
            if reason:
                return _invalid(reason, state)
            # The timed check starts a child process: never for a stale change.
            if version != based_on:
                return _json(409, {"error": "stale", "version": version})
            # A station the PIN hides now that this change shows to every
            # device: the one edit that exposes something needs a code.
            if _exposed(stored, state) and not self._accounts.fresh(session):
                return _error(403, "reauth_required")
            # The history row holds the state before and after. An unchanged
            # save stores nothing and is the retry for a failed apply.
            after = state_to_json(state)
            if after != state_to_json(stored) and not has_room(self._free(), 2 * len(after)):
                event(log, logging.WARNING, "admin_disk_low", client=client)
                return _error(507, "disk_low")
            reason = check_new(state, stored, self._samples)
            if reason:
                return _invalid(reason, state)
            version, changed = self._settings.commit_changed(state, based_on, kind, target)
        except ConfigError as error:
            return _invalid(str(error), state)
        except StaleVersion as error:
            return _json(409, {"error": "stale", "version": error.current})
        # An unchanged save still asks for the apply: after a failed one the
        # stored version is ahead of the live one, and this is the retry.
        try:
            applied = self._apply() >= version
        except Exception:
            # The change is stored whatever the apply raised; only "live" is in doubt.
            log.error("apply_failed", exc_info=True, extra={"fields": {"version": version}})
            applied = False
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

    def _upload(self, request: Request, _session: Session) -> Response:
        try:
            ref, kind = self._blobs.save(request.body)
        except BlobError:
            return _error(400, "not_an_image")
        except BlobStoreFull as error:
            name = "logo_store_full" if error.reason == "logo_store_full" else "admin_disk_low"
            event(log, logging.WARNING, name, client=request.client)
            return _error(507, error.reason)
        except BlobUnavailable as error:
            event(
                log, logging.WARNING, "logo_store_failed", client=request.client, errno=error.errno
            )
            return _error(503, "logo_store_failed")
        event(
            log,
            logging.INFO,
            "admin_logo_uploaded",
            client=request.client,
            kind=kind,
            bytes=len(request.body),
        )
        return _json(200, {"logo": ref, "kind": kind})

    def _blob(self, request: Request, _session: Session) -> Response:
        found = self._blobs.path(f"blob:{request.path.rsplit('/', 1)[-1]}")
        if found is None:
            return _error(404, "not_found")
        # The blob is the uploader's bytes, an SVG may carry script: sandbox
        # keeps it inert even when opened as a page of its own.
        return Response(
            200,
            found.read_bytes(),
            CONTENT_TYPES[found.suffix[1:]],
            (("Content-Security-Policy", "sandbox"),),
        )

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
        except ConfigError as error:
            return _invalid(str(error), None)
        stored = self._settings.load()[0]
        reason = self._judge(state, stored)
        if reason:
            return _invalid(reason, state)
        exposed = _exposed(stored, state)
        if exposed:
            notes.append(
                f"The listening PIN no longer hides {', '.join(exposed)}"
                " (needs_pin is not set in the file)"
            )
        return _json(200, {"state": json.loads(state_to_json(state)), "notes": notes})

    def _import(self, request: Request, session: Session) -> Response:
        payload = _object(request)
        based_on = _version(payload)
        text = payload.get("toml") if payload else None
        if based_on is None or not isinstance(text, str):
            return _error(400, "bad_request")
        try:
            state, _notes = state_from_import(text)
        except ConfigError as error:
            return _invalid(str(error), None)
        return self._commit(state, based_on, "import", "panel", request.client, session)

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

    def _restore(self, request: Request, session: Session) -> Response:
        based_on = _version(_object(request))
        if based_on is None:
            return _error(400, "bad_request")
        entry_id = _history_id(request.path)
        found = self._settings.history_entry(entry_id)
        if found is None:
            return _error(404, "not_found")
        _, before, _ = found
        return self._commit(
            before, based_on, "restore", f"history {entry_id}", request.client, session
        )

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
        secret = crypto.new_totp_secret()
        self.gate.hold_totp(session.token_hash, secret)
        return _json(200, {"secret": crypto.secret_text(secret), "uri": crypto.otpauth_uri(secret)})

    def _totp_finish(self, request: Request, session: Session) -> Response:
        body = _body(request, ("code",))
        if body is None:
            return _error(400, "bad_request")
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

    def _using(
        self, handler: Callable[[Request, Session, Usage], Response]
    ) -> Callable[[Request, Session], Response]:
        """A usage handler as a route's run, answering not found when this
        Admin was built without usage."""

        def run(request: Request, session: Session) -> Response:
            if self._usage is None:
                return _error(404, "not_found")
            return handler(request, session, self._usage)

        return run

    def _usage_live(self, _request: Request, _session: Session, usage: Usage) -> Response:
        return _json(200, usage.live())

    def _usage_summary(self, request: Request, _session: Session, usage: Usage) -> Response:
        query = _query(request)
        try:
            return _json(200, usage.summary(query["range"] if query else ""))
        except ValueError, KeyError:
            return _error(400, "bad_request")

    def _usage_devices(self, _request: Request, _session: Session, usage: Usage) -> Response:
        return _json(200, usage.devices())

    def _usage_device(self, request: Request, _session: Session, usage: Usage) -> Response:
        query = _query(request)
        if query is None:
            return _error(400, "bad_request")
        device = query.get("id")
        if device is None or not valid_device(device):
            return _error(404, "not_found")
        before: float | None = None
        if "before" in query:
            try:
                before = float(query["before"])
            except ValueError:
                return _error(400, "bad_request")
            if not math.isfinite(before):
                return _error(400, "bad_request")
        page = usage.device(device, before)
        return _error(404, "not_found") if page is None else _json(200, page)

    def _usage_rename(self, request: Request, _session: Session, usage: Usage) -> Response:
        body = _body(request, ("id", "name"))
        if body is None:
            return _error(400, "bad_request")
        name = body["name"].strip()
        if len(name) > NAME_MAX or not encodable(name):
            return _error(400, "bad_request")
        if not valid_device(body["id"]) or not usage.rename(body["id"], name or None):
            return _error(404, "not_found")
        return Response(204)

    def _usage_delete(self, request: Request, _session: Session, usage: Usage) -> Response:
        body = _body(request, ("id",))
        if body is None:
            return _error(400, "bad_request")
        if not valid_device(body["id"]) or not usage.delete(body["id"]):
            return _error(404, "not_found")
        return Response(204)

    def _usage_settings(self, _request: Request, _session: Session, usage: Usage) -> Response:
        return _json(200, _usage_answer(usage.settings()))

    def _usage_change(self, request: Request, session: Session, usage: Usage) -> Response:
        """Every field is checked, and the fresh code too, before anything
        is applied; then retention first, addresses second."""
        payload = _object(request) or {}
        days = payload.get("retention_days")
        addresses = payload.get("store_addresses")
        if (days is None and addresses is None) or (
            days is not None
            and (
                not isinstance(days, int)
                or isinstance(days, bool)
                or not RETENTION_MIN <= days <= RETENTION_MAX
            )
        ):
            return _error(400, "bad_request")
        if addresses is not None and not isinstance(addresses, bool):
            return _error(400, "bad_request")
        if addresses and not usage.settings().store_addresses and not self._accounts.fresh(session):
            return _error(403, "reauth_required")
        current = usage.settings()
        if days is not None:
            current = usage.set_retention(days)
        if addresses is not None:
            current = usage.set_addresses(addresses)
        # Usage logs usage_settings_changed once a change is stored; this
        # line says who asked.
        event(
            log,
            logging.INFO,
            "admin_change",
            kind="usage_settings",
            retention_days=current.retention_days,
            store_addresses=current.store_addresses,
            client=request.client,
        )
        return _json(200, _usage_answer(current))

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
    except ValueError, RecursionError:
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
    except ValueError, RecursionError:
        return None
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def _query(request: Request) -> dict[str, str] | None:
    """The query's fields, or None when it has too many or names one twice."""
    try:
        fields = urllib.parse.parse_qs(request.query, max_num_fields=4)
    except ValueError:
        return None
    if any(len(values) != 1 for values in fields.values()):
        return None
    return {key: values[0] for key, values in fields.items()}


def _usage_answer(current: UsageSettings) -> dict[str, object]:
    return {
        **dataclasses.asdict(current),
        "retention_min": RETENTION_MIN,
        "retention_max": RETENTION_MAX,
    }


def _version(payload: dict[str, object] | None) -> int | None:
    """based_on: an int, and not a bool (which is an int in Python)."""
    value = payload.get("based_on") if payload else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _history_id(path: str) -> int:
    return int(path.removeprefix("/admin/api/history/").split("/")[0])
