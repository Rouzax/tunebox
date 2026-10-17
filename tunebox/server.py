"""HTTP front end: the JSON API and the static player."""

from __future__ import annotations

import json
import logging
import mimetypes
import time
import urllib.parse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from tunebox.channel import Channel
from tunebox.logs import bound, event

log = logging.getLogger(__name__)

_TEXT_TYPES = {"text/html", "text/javascript", "text/css"}


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    # A compressing reverse proxy hands the ETag out weakened, as W/"...".
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


class TuneboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], channels: Mapping[str, Channel], web_dir: Path
    ) -> None:
        self.channels = channels
        self.web_dir = web_dir.resolve()
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
            listing = [channel.info() for channel in channels.values()]
            return self._send_json(200, listing, head, {"Cache-Control": "max-age=300"})
        if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "now":
            channel = channels.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            body, etag = channel.now_view()
            if etag_matches(self.headers.get("If-None-Match"), etag):
                return self._not_modified(etag)
            headers = {"ETag": etag, "Cache-Control": "no-cache"}
            return self._send_json(200, body, head, headers)
        if len(parts) == 3 and parts[:2] == ["api", "pawtunes"]:
            channel = channels.get(parts[2])
            if channel is None:
                return self._send_json(404, {"error": "unknown_channel"}, head)
            headers = {"Cache-Control": "no-cache"}
            return self._send_json(200, channel.pawtunes_view(), head, headers)
        if parts and parts[0] == "api":
            return self._send_json(404, {"error": "not_found"}, head)
        return self._static(path, head)

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
        if target is None:
            return self._send_json(404, {"error": "not_found"}, head)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # The page and its code must update with a new image; other files may be cached a day.
        cache = "no-cache" if content_type in _TEXT_TYPES else "max-age=86400"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type, head, {"Cache-Control": cache})

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
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _not_modified(self, etag: str) -> None:
        self._status = 304
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()
