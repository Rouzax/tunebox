"""HTTP front end: the JSON API and the static player."""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from tunebox.admin import ADMIN_HEADERS, MAX_BODY, Admin, Request
from tunebox.bursts import Bursts
from tunebox.channel import Channel
from tunebox.config import Network, StreamConfig
from tunebox.limits import Once
from tunebox.logos import CONTENT_TYPES as LOGO_TYPES
from tunebox.logos import Logos
from tunebox.logs import bound, event
from tunebox.netguard import OTHER_FORWARDING, client_address, nearby, trusted_peer
from tunebox.paths import inside
from tunebox.pin import Access, Pin
from tunebox.runtime import Snapshot

log = logging.getLogger(__name__)

MAX_BUFFER_S = 120.0
# A PIN and its JSON wrapper; anything longer is not a PIN.
PIN_BODY = 256
NO_STORE = {"Cache-Control": "no-store"}
PIN_PAGE = "/pin.html"
# A request to an idle channel waits this long for its first poll, so the
# answer carries what is on now rather than nothing.
FRESH_WAIT_S = 2.5

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
# Folders here carry their version in the name. Everything else (the player's
# own code has no build step to fingerprint it) is revalidated on every load.
IMMUTABLE_DIRS = ("vendor/",)
# Covers come from catalogue CDNs and streams from redirect targets whose
# hosts vary, hence any https host for images and media. http media is for
# an http stream_url on a plain-http page; https pages block it anyway.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' https: data:",
        "media-src https: http:",
        "connect-src 'self'",
        "frame-ancestors *",
        "base-uri 'none'",
        "form-action 'none'",
    ]
)


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    # A compressing reverse proxy hands the ETag out weakened, as W/"...".
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


def _query(target: str, name: str) -> list[str] | None:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(target).query, keep_blank_values=True).get(
        name
    )


def _locked_route(parts: list[str]) -> bool:
    """What the PIN covers besides the pages (which _static judges by the
    file it would serve): the API except the PIN itself and PawTunes (which
    knows nothing of a PIN), and the logos. Scripts, styles and fonts are
    the player's public code and stay open, so the PIN page can load."""
    if parts[:1] == ["logos"]:
        return True
    return parts[:1] == ["api"] and parts[1:2] not in (["pin"], ["pawtunes"])


def _private(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """While a PIN is on, a shared cache must not hand one device's answer
    to another that has no grant."""
    out: list[tuple[str, str]] = []
    cached = False
    for name, value in pairs:
        if name.lower() == "cache-control":
            cached = True
            if not value.startswith(("private", "no-store")):
                value = f"private, {value}"
        out.append((name, value))
    if not cached:
        out.append(("Cache-Control", "private"))
    out.append(("Vary", "Cookie"))
    return out


def _buffer_s(target: str) -> float | None:
    """The player's buffer from ?buffer_s=, 0 when absent, None when unusable."""
    values = _query(target, "buffer_s")
    if not values:
        return 0.0
    try:
        value = float(values[0])
    except ValueError:
        return None
    # A live stream's buffer is seconds to a minute or two; nan and inf fail here too.
    return value if 0 <= value <= MAX_BUFFER_S else None


def _stream(target: str, channel: Channel) -> StreamConfig | None:
    """The stream the player plays, from ?stream=<name>; None when absent.
    A number names a position, as players before stream names send; a name
    that is a number wins. Raises ValueError when it names no stream."""
    values = _query(target, "stream")
    if not values:
        return None
    streams = channel.config.streams
    for stream in streams:
        if stream.name == values[0]:
            return stream
    if values[0].isdigit() and int(values[0]) < len(streams):
        return streams[int(values[0])]
    raise ValueError(values[0])


class TuneboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        snapshot: Callable[[], Snapshot],
        web_dir: Path,
        pin: Pin,
        logos: Logos | None = None,
        bursts: Bursts | None = None,
        admin: Admin | None = None,
        trusted: tuple[Network, ...] = (),
    ) -> None:
        self.current = snapshot
        self.admin = admin
        self.pin = pin
        self.trusted = trusted
        self.forwarding = Once(3600)
        self.refusals = Once(600)
        self.bursts = bursts
        self.web_dir = web_dir.resolve()
        self.logos = logos
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "tunebox"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Idle keep-alive connections from the reverse proxy are closed after this.
    timeout = 30
    _status = 0
    # Set per request by _route: the answer depends on the PIN cookie, and
    # stations that need the PIN are hidden from this device.
    _private = False
    _hidden = False

    @property
    def app(self) -> TuneboxServer:
        return cast(TuneboxServer, self.server)

    def do_GET(self) -> None:
        self._handle(head=False)

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def do_POST(self) -> None:
        self._handle(head=False)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: V107
        """Replaced by one structured line per request in _handle."""

    def _handle(self, head: bool) -> None:
        started = time.monotonic()
        self._status = 0
        self._private = False
        self._hidden = False
        target = urllib.parse.urlsplit(self.path)
        path = target.path
        is_admin = path == "/admin" or path.startswith("/admin/")
        is_pin = path == "/api/pin"
        body = b""
        if self.command == "POST" and not (is_admin or is_pin):
            # Only /admin and /api/pin take a POST: answer before reading a body nobody
            # asked for, and hang up so it is not parsed as a request.
            self.close_connection = True
            with bound("req"):
                self._send_json(405, {"error": "method_not_allowed"}, False)
            return
        if self.command != "POST" and (
            self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0"
        ):
            # GET and HEAD bodies are never read, so on a kept-alive connection
            # the body would be parsed as the next request. Answer, then hang up.
            self.close_connection = True
        # Admin and PIN requests are sensitive: the log formatter withholds
        # exception text, which can carry what was typed.
        with bound(
            "admin" if is_admin else "pin" if is_pin else "req", sensitive=is_admin or is_pin
        ):
            try:
                self._note_forwarding()
                if self.command == "POST":
                    if is_admin and self.app.admin is not None:
                        # The headers decide how much may be read.
                        limit = self.app.admin.body_limit(
                            self._admin_request(path, target.query, b"")
                        )
                    else:
                        limit = MAX_BODY if is_admin else PIN_BODY
                    read = self._read_body(limit, is_admin)
                    if read is None:
                        return
                    body = read
                if is_admin:
                    self._admin(path, target.query, head, body)
                elif is_pin:
                    self._pin(head, body)
                else:
                    self._route(path, head)
            except BrokenPipeError, ConnectionResetError:
                pass  # the client went away mid-response
            except Exception:  # keep one broken request out of socketserver's stderr
                log.error("request_crashed", exc_info=True, extra={"fields": {"path": path}})
                if self._status == 0:
                    admin_headers = ADMIN_HEADERS if is_admin else None
                    self._send_json(500, {"error": "internal"}, head, admin_headers)
            finally:
                event(
                    log,
                    logging.DEBUG,
                    "request",
                    method=self.command,
                    path=path,
                    status=self._status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )

    def _read_body(self, limit: int, admin: bool) -> bytes | None:
        """The request body, or None after answering 411 or 413, or after
        the client stalled or hung up before sending all of it. A body
        that is not read would be parsed as the next request, so a refused
        one also ends the connection."""
        headers = ADMIN_HEADERS if admin else None
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or length is None or not length.isdigit():
            self.close_connection = True
            self._send_json(411, {"error": "length_required"}, False, headers)
            return None
        if int(length) > limit:
            self.close_connection = True
            self._send_json(413, {"error": "too_large"}, False, headers)
            return None
        try:
            body = self.rfile.read(int(length))
        except OSError:  # TimeoutError: the client stalled
            body = None
        if body is None or len(body) < int(length):
            # A client that stalls or hangs up mid-body is not a server
            # fault, and the stream is no longer usable: hang up silently.
            self.close_connection = True
            event(
                log,
                logging.DEBUG,
                "request_body_incomplete",
                path=urllib.parse.urlsplit(self.path).path,
                expected=int(length),
                received=None if body is None else len(body),
            )
            return None
        return body

    def _admin(self, path: str, query: str, head: bool, body: bytes) -> None:
        admin = self.app.admin
        if admin is None:
            return self._send_json(404, {"error": "not_found"}, head)
        forwarded = self._forwarded()
        peer = self.client_address[0]
        client = self._client()
        if admin.plain_http:
            other = any(self.headers.get(name) for name in OTHER_FORWARDING)
            if not nearby(peer, forwarded, other, self.app.trusted):
                # Plain http carries the password readable: off the home
                # network the panel looks switched off.
                level = logging.WARNING if self.app.refusals.first(client) else logging.DEBUG
                event(log, level, "admin_not_nearby", client=client, peer=peer)
                return self._send_json(404, {"error": "not_found"}, head)
        method = "GET" if head else self.command
        response = admin.handle(self._admin_request(path, query, body, method))
        self._send(response.status, response.body, response.content_type, head, response.headers)

    def _admin_request(
        self, path: str, query: str, body: bytes, method: str | None = None
    ) -> Request:
        return Request(
            method or self.command,
            path,
            tuple(self.headers.items()),
            body,
            self._client(),
            query,
        )

    def _note_forwarding(self) -> None:
        """Forwarding headers from a peer that is not a trusted proxy: either
        trusted_proxies is unset or misses the real proxy (then every client
        shares the proxy's address, and limits and CrowdSec punish everyone
        at once), or a client is sending them itself."""
        peer = self.client_address[0]
        untrusted = self._forwarded() and not trusted_peer(peer, self.app.trusted)
        if untrusted and self.app.forwarding.first(peer):
            event(log, logging.WARNING, "untrusted_forwarding", peer=peer)

    def _forwarded(self) -> str | None:
        # Every line: a proxy may add its own line after the client's, and
        # the first line alone would be the client's word.
        return ", ".join(self.headers.get_all("X-Forwarded-For") or []) or None

    def _client(self) -> str:
        return client_address(self.client_address[0], self._forwarded(), self.app.trusted)

    def _https(self) -> bool:
        """Whether the browser reached the proxy over https, as a trusted
        proxy says. tunebox itself speaks plain http, so without a proxy's
        word the cookie must work over http (a player on the home network)."""
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https" and trusted_peer(self.client_address[0], self.app.trusted)

    def _local(self) -> bool:
        """A caller on this host itself: the Docker healthcheck. A reverse
        proxy on the same host connects from loopback too, and says so in
        its headers."""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
        proxied = self._forwarded() or any(self.headers.get(n) for n in OTHER_FORWARDING)
        return loopback and not proxied

    def _cookie_header(self) -> str | None:
        """A repeated Cookie line counts as absent, as on admin requests."""
        lines = self.headers.get_all("Cookie") or []
        return lines[0] if len(lines) == 1 else None

    def _pin(self, head: bool, body: bytes) -> None:
        pin = self.app.pin
        if self.command != "POST":
            return self._send_json(405, {"error": "method_not_allowed"}, head, NO_STORE)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        site = self.headers.get("Sec-Fetch-Site")
        if content_type != "application/json" or (site is not None and site != "same-origin"):
            # Another site's page making a visitor's browser post here. Counted
            # as a failure, it would lock the household out of the PIN, or get
            # its address banned. A form cannot send JSON, and a cross-site
            # fetch with it needs a preflight that tunebox never answers.
            event(log, logging.INFO, "listener_pin_cross_site", client=self._client())
            return self._send_json(403, {"error": "bad_request_origin"}, head, NO_STORE)
        try:
            payload: object = json.loads(body or b"null")
        except ValueError:
            payload = None
        given = cast("dict[str, object]", payload).get("pin") if isinstance(payload, dict) else None
        if not isinstance(given, str):
            return self._send_json(400, {"error": "bad_request"}, head, NO_STORE)
        client = self._client()
        result = pin.attempt(given, client, secure=self._https())
        if result.ok and result.cookie:
            event(log, logging.INFO, "listener_pin_granted", client=client)
            cookie = ("Set-Cookie", result.cookie)
            return self._send(204, b"", "text/plain", head, [*NO_STORE.items(), cookie])
        if result.reason == "off":
            return self._send(204, b"", "text/plain", head, NO_STORE)
        if result.reason == "busy":
            # Someone's flood, not this address's guess: no CrowdSec strike.
            event(log, logging.DEBUG, "listener_pin_paced", client=client)
            retry = {**NO_STORE, "Retry-After": "2"}
            return self._send_json(429, {"error": "busy"}, head, retry)
        event(log, logging.WARNING, "listener_pin_failed", client=client, reason=result.reason)
        if result.reason == "locked":
            return self._send_json(429, {"error": "slow_down"}, head, NO_STORE)
        return self._send_json(401, {"error": "pin_wrong"}, head, NO_STORE)

    def _route(self, path: str, head: bool) -> None:
        parts = [p for p in path.split("/") if p]
        # One snapshot per request: a reload in the meantime cannot mix two.
        snapshot = self.app.current()
        channels = snapshot.channels
        if path == "/healthz":
            if not self._local():
                return self._send_json(200, {"status": "ok"}, head)
            status = {cid: channel.status() for cid, channel in channels.items()}
            return self._send_json(200, {"status": "ok", "channels": status}, head)
        access = self.app.pin.access(self._cookie_header())
        if _locked_route(parts) and access.private:
            if access.locked:
                return self._send_json(401, {"error": "pin_required"}, head, NO_STORE)
            self._private = True
            self._hidden = access.hidden
        visible = {
            cid: channel
            for cid, channel in channels.items()
            if not (self._hidden and channel.config.needs_pin)
        }
        if parts == ["api", "channels"]:
            logos = self.app.logos
            listing = [
                {
                    **channel.info(),
                    "logo": logos.path_for(cid) if logos else None,
                    "logo_small": logos.path_for(cid, small=True) if logos else None,
                }
                for cid, channel in visible.items()
            ]
            headers = {
                "Cache-Control": "no-cache",
                "X-Tunebox-Version": str(snapshot.version),
                "X-Tunebox-Pin-Revision": access.revision,
            }
            if len(visible) < len(channels):
                # The player offers "More stations" for these.
                headers["X-Tunebox-Pin"] = "more"
            return self._send_json(200, listing, head, headers)
        if parts == ["api", "player"]:
            player = snapshot.player
            default = player.default_channel or next(iter(channels), None)
            body = {
                "latitude": player.latitude,
                "longitude": player.longitude,
                # A hidden default would name a station the device cannot see.
                "default_channel": default if default in visible else None,
            }
            return self._send_json(200, body, head, {"Cache-Control": "max-age=300"})
        if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "now":
            channel = visible.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            if channel.touch():
                channel.wait_fresh(FRESH_WAIT_S)
            buffer_s = _buffer_s(self.path)
            if buffer_s is None:
                return self._send_json(400, {"error": "bad_buffer_s"}, head)
            try:
                stream = _stream(self.path, channel)
            except ValueError:
                return self._send_json(400, {"error": "bad_stream"}, head)
            bursts = self.app.bursts
            if stream and bursts is not None:
                # The player cannot see the audio its stream sent on connect.
                bursts.request(stream)
                buffer_s += bursts.burst_s(stream.url)
            name = stream.name if stream else None
            body, etag = channel.now_view(
                buffer_s, version=snapshot.version, stream=name, pin=access.revision
            )
            if etag_matches(self.headers.get("If-None-Match"), etag):
                return self._not_modified(etag)
            headers = {"ETag": etag, "Cache-Control": "no-cache"}
            return self._send_json(200, body, head, headers)
        if len(parts) == 3 and parts[:2] == ["api", "pawtunes"]:
            channel = channels.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            if channel.touch():
                channel.wait_fresh(FRESH_WAIT_S)
            headers = {"Cache-Control": "no-cache"}
            return self._send_json(200, channel.pawtunes_view(), head, headers)
        if parts and parts[0] == "api":
            return self._send_json(404, {"error": "not_found"}, head)
        if parts and parts[0] == "logos":
            return self._logo(parts, head, hidden=set(channels) - set(visible))
        return self._static(path, head, access)

    def _logo(self, parts: list[str], head: bool, hidden: set[str]) -> None:
        if urllib.parse.unquote(parts[-1]).split(".")[0] in hidden:
            # A station hidden behind the PIN has no logo either.
            return self._send_json(404, {"error": "not_found"}, head)
        logos = self.app.logos
        small = len(parts) == 3 and parts[1] == "small"
        target = None
        if logos and (len(parts) == 2 or small):
            target = logos.file(urllib.parse.unquote(parts[-1]), small=small)
        if target is None:
            return self._send_json(404, {"error": "not_found"}, head)
        kind = target.suffix.removeprefix(".")
        headers = {
            "Cache-Control": "max-age=31536000, immutable",
            # An image opened directly cannot run anything on this origin,
            # whatever its kind claims to be.
            "Content-Security-Policy": "sandbox",
        }
        self._send(200, target.read_bytes(), LOGO_TYPES[kind], head, headers)

    def _static(self, path: str, head: bool, access: Access, locked: bool = False) -> None:
        """locked: the PIN page in place of a page the PIN keeps; never
        cached, and without an ETag a browser could revalidate the real
        page against."""
        relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
        web_dir = self.app.web_dir
        target = inside(web_dir, relative)
        content_type = CONTENT_TYPES.get(target.suffix) if target else None
        if target is None or content_type is None:
            return self._send_json(404, {"error": "not_found"}, head)
        page = target.suffix == ".html" and target != web_dir / PIN_PAGE.lstrip("/")
        if not locked and page and access.private:
            # Judged on the file about to be served, not the path as typed:
            # /index%2ehtml is the player page too.
            if access.locked:
                return self._static(PIN_PAGE, head, access, locked=True)
            self._private = True
        headers: dict[str, str] = dict(NO_STORE)
        if not locked:
            stat = target.stat()
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
            if etag_matches(self.headers.get("If-None-Match"), etag):
                return self._not_modified(etag)
            # Judged on the resolved path: /vendor/../app.js is not a vendor file.
            immutable = target.relative_to(web_dir).as_posix().startswith(IMMUTABLE_DIRS)
            headers = {
                "ETag": etag,
                "Cache-Control": "max-age=31536000, immutable" if immutable else "no-cache",
            }
        if target.suffix == ".html":
            headers["Content-Security-Policy"] = CSP
            headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        self._send(200, target.read_bytes(), content_type, head, headers)

    def _send_json(
        self, status: int, payload: object, head: bool, headers: Mapping[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._send(status, body, "application/json; charset=utf-8", head, headers)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        head: bool,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """headers may be pairs, so a header such as Set-Cookie can repeat."""
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        pairs: Iterable[tuple[str, str]]
        if isinstance(headers, Mapping):
            pairs = cast("Mapping[str, str]", headers).items()
        else:
            pairs = headers or ()
        if self._private:
            pairs = _private(pairs)
        for name, value in pairs:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _not_modified(self, etag: str) -> None:
        self._status = 304
        self.send_response(304)
        self.send_header("ETag", etag)
        if self._private:
            # The same headers as the 200 this stands in for, so a cache
            # that stores or revalidates the answer keeps it per device.
            self.send_header("Cache-Control", "private, no-cache")
            self.send_header("Vary", "Cookie")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", "0")
        self.end_headers()
