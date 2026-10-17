"""HTTP front end: the JSON API and the static player."""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from tunebox.channel import Channel
from tunebox.config import PlayerConfig
from tunebox.logos import CONTENT_TYPES as LOGO_TYPES
from tunebox.logos import Logos
from tunebox.logs import bound, event

log = logging.getLogger(__name__)

MAX_BUFFER_S = 120.0

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
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
# hosts vary, hence https: for images and media.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' https: data:",
        "media-src https:",
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


def _buffer_s(target: str) -> float | None:
    """The player's buffer from ?buffer_s=, 0 when absent, None when unusable."""
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query).get("buffer_s")
    if not values:
        return 0.0
    try:
        value = float(values[0])
    except ValueError:
        return None
    # A live stream's buffer is seconds to a minute or two; nan and inf fail here too.
    return value if 0 <= value <= MAX_BUFFER_S else None


class TuneboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        channels: Mapping[str, Channel],
        web_dir: Path,
        logos: Logos | None = None,
        player: PlayerConfig | None = None,
    ) -> None:
        self.channels = channels
        self.web_dir = web_dir.resolve()
        self.logos = logos
        self.player = player or PlayerConfig()
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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: V107
        """Replaced by one structured line per request in _handle."""

    def _handle(self, head: bool) -> None:
        started = time.monotonic()
        self._status = 0
        path = urllib.parse.urlsplit(self.path).path
        # GET and HEAD bodies are never read, so on a kept-alive connection the
        # body would be parsed as the next request. Answer, then hang up.
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self.close_connection = True
        with bound("req"):
            try:
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

    def _route(self, path: str, head: bool) -> None:
        parts = [p for p in path.split("/") if p]
        channels = self.app.channels
        if path == "/healthz":
            status = {cid: channel.status() for cid, channel in channels.items()}
            return self._send_json(200, {"status": "ok", "channels": status}, head)
        if parts == ["api", "channels"]:
            logos = self.app.logos
            listing = [
                {**channel.info(), "logo": logos.path_for(cid) if logos else None}
                for cid, channel in channels.items()
            ]
            return self._send_json(200, listing, head, {"Cache-Control": "max-age=300"})
        if parts == ["api", "player"]:
            player = self.app.player
            body = {
                "latitude": player.latitude,
                "longitude": player.longitude,
                "default_channel": player.default_channel or next(iter(channels)),
            }
            return self._send_json(200, body, head, {"Cache-Control": "max-age=300"})
        if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "now":
            channel = channels.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            channel.touch()
            buffer_s = _buffer_s(self.path)
            if buffer_s is None:
                return self._send_json(400, {"error": "bad_buffer_s"}, head)
            body, etag = channel.now_view(buffer_s)
            if etag_matches(self.headers.get("If-None-Match"), etag):
                return self._not_modified(etag)
            headers = {"ETag": etag, "Cache-Control": "no-cache"}
            return self._send_json(200, body, head, headers)
        if len(parts) == 3 and parts[:2] == ["api", "pawtunes"]:
            channel = channels.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            channel.touch()
            headers = {"Cache-Control": "no-cache"}
            return self._send_json(200, channel.pawtunes_view(), head, headers)
        if parts and parts[0] == "api":
            return self._send_json(404, {"error": "not_found"}, head)
        if parts and parts[0] == "logos":
            return self._logo(parts, head)
        return self._static(path, head)

    def _logo(self, parts: list[str], head: bool) -> None:
        logos = self.app.logos
        target = logos.file(urllib.parse.unquote(parts[1])) if logos and len(parts) == 2 else None
        if target is None:
            return self._send_json(404, {"error": "not_found"}, head)
        kind = target.suffix.removeprefix(".")
        headers = {"Cache-Control": "max-age=86400"}
        if kind == "svg":
            headers["Content-Security-Policy"] = "sandbox"
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
        immutable = relative.startswith(IMMUTABLE_DIRS)
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
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
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
