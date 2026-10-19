"""HTTP front end: the JSON API and the static player."""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from tunebox.admin import Admin, Request, body_limit
from tunebox.bursts import Bursts
from tunebox.channel import Channel
from tunebox.config import Network, StreamConfig
from tunebox.limits import Once
from tunebox.logos import CONTENT_TYPES as LOGO_TYPES
from tunebox.logos import Logos
from tunebox.logs import bound, event
from tunebox.netguard import OTHER_FORWARDING, client_address, nearby
from tunebox.runtime import Snapshot

log = logging.getLogger(__name__)

MAX_BUFFER_S = 120.0
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
        logos: Logos | None = None,
        bursts: Bursts | None = None,
        admin: Admin | None = None,
        trusted: tuple[Network, ...] = (),
    ) -> None:
        self.current = snapshot
        self.admin = admin
        self.trusted = trusted
        self.warned_proxies = False
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
        target = urllib.parse.urlsplit(self.path)
        path = target.path
        is_admin = path == "/admin" or path.startswith("/admin/")
        body = b""
        if self.command == "POST" and not is_admin:
            # Only /admin takes a POST: answer before reading a body nobody
            # asked for, and hang up so it is not parsed as a request.
            self.close_connection = True
            with bound("req"):
                self._send_json(405, {"error": "method_not_allowed"}, False)
            return
        if self.command == "POST":
            read = self._read_body(body_limit(path))
            if read is None:
                return
            body = read
        elif (
            self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0"
        ):
            # GET and HEAD bodies are never read, so on a kept-alive connection
            # the body would be parsed as the next request. Answer, then hang up.
            self.close_connection = True
        # The admin- context also tells the log formatter to withhold
        # exception text, which can carry what was typed.
        with bound("admin" if is_admin else "req"):
            try:
                if is_admin:
                    self._admin(path, target.query, head, body)
                else:
                    self._route(path, head)
            except BrokenPipeError, ConnectionResetError:
                pass  # the client went away mid-response
            except Exception:  # keep one broken request out of socketserver's stderr
                log.error("request_crashed", exc_info=True, extra={"fields": {"path": path}})
                if self._status == 0:
                    self._send_json(500, {"error": "internal"}, head)
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

    def _read_body(self, limit: int) -> bytes | None:
        """The request body, or None after answering 411 or 413. A body
        that is not read would be parsed as the next request, so a refused
        one also ends the connection."""
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or length is None or not length.isdigit():
            self.close_connection = True
            self._send_json(411, {"error": "length_required"}, False)
            return None
        if int(length) > limit:
            self.close_connection = True
            self._send_json(413, {"error": "too_large"}, False)
            return None
        return self.rfile.read(int(length))

    def _admin(self, path: str, query: str, head: bool, body: bytes) -> None:
        admin = self.app.admin
        if admin is None:
            return self._send_json(404, {"error": "not_found"}, head)
        # Every line: a proxy may add its own line after the client's, and
        # the first line alone would be the client's word.
        forwarded = ", ".join(self.headers.get_all("X-Forwarded-For") or []) or None
        peer = self.client_address[0]
        if forwarded and not self.app.trusted and not self.app.warned_proxies:
            self.app.warned_proxies = True
            # Every client would share the proxy's address: limits and
            # CrowdSec would then punish everyone at once.
            event(log, logging.WARNING, "trusted_proxies_unset", peer=peer)
        client = client_address(peer, forwarded, self.app.trusted)
        if admin.plain_http:
            other = any(self.headers.get(name) for name in OTHER_FORWARDING)
            if not nearby(peer, forwarded, other, self.app.trusted):
                # Plain http carries the password readable: off the home
                # network the panel looks switched off.
                level = logging.WARNING if self.app.refusals.first(client) else logging.DEBUG
                event(log, level, "admin_not_nearby", client=client, peer=peer)
                return self._send_json(404, {"error": "not_found"}, head)
        method = "GET" if head else self.command
        headers = dict(self.headers.items())
        response = admin.handle(Request(method, path, headers, body, client, query))
        self._send(response.status, response.body, response.content_type, head, response.headers)

    def _route(self, path: str, head: bool) -> None:
        parts = [p for p in path.split("/") if p]
        # One snapshot per request: a reload in the meantime cannot mix two.
        snapshot = self.app.current()
        channels = snapshot.channels
        if path == "/healthz":
            status = {cid: channel.status() for cid, channel in channels.items()}
            return self._send_json(200, {"status": "ok", "channels": status}, head)
        if parts == ["api", "channels"]:
            logos = self.app.logos
            listing = [
                {
                    **channel.info(),
                    "logo": logos.path_for(cid) if logos else None,
                    "logo_small": logos.path_for(cid, small=True) if logos else None,
                }
                for cid, channel in channels.items()
            ]
            headers = {"Cache-Control": "no-cache", "X-Tunebox-Version": str(snapshot.version)}
            return self._send_json(200, listing, head, headers)
        if parts == ["api", "player"]:
            player = snapshot.player
            body = {
                "latitude": player.latitude,
                "longitude": player.longitude,
                "default_channel": player.default_channel or next(iter(channels), None),
            }
            return self._send_json(200, body, head, {"Cache-Control": "max-age=300"})
        if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "now":
            channel = channels.get(parts[2])
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
            body, etag = channel.now_view(buffer_s, version=snapshot.version, stream=name)
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
            return self._logo(parts, head)
        return self._static(path, head)

    def _logo(self, parts: list[str], head: bool) -> None:
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

    def _static(self, path: str, head: bool) -> None:
        relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
        web_dir = self.app.web_dir
        target: Path | None
        try:
            candidate = (web_dir / relative).resolve()
            inside = candidate.is_relative_to(web_dir) and candidate.is_file()
            target = candidate if inside else None
        except OSError, ValueError:  # ValueError: an embedded NUL byte
            target = None
        content_type = CONTENT_TYPES.get(target.suffix) if target else None
        if target is None or content_type is None:
            return self._send_json(404, {"error": "not_found"}, head)
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", "0")
        self.end_headers()
