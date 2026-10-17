"""Station logos: downloaded once from the configured address, then served by
tunebox, so listeners' browsers never contact the logo host."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

from tunebox.config import ChannelConfig
from tunebox.http import HttpError, HttpGet
from tunebox.logs import event

log = logging.getLogger(__name__)

EXTENSIONS = ("png", "jpg", "webp", "svg")
CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(png|jpg|webp|svg)$")


def sniff(body: bytes) -> str | None:
    """The image kind by its first bytes; a host's Content-Type is not trusted."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    head = body[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


class Logos:
    def __init__(self, directory: Path, http_get: HttpGet) -> None:
        self._dir = directory
        self._get = http_get

    def path_for(self, channel_id: str) -> str | None:
        for ext in EXTENSIONS:
            if (self._dir / f"{channel_id}.{ext}").is_file():
                return f"/logos/{channel_id}.{ext}"
        return None

    def file(self, name: str) -> Path | None:
        if not _NAME.match(name):
            return None
        path = self._dir / name
        return path if path.is_file() else None

    def fetch_all(self, channels: Iterable[ChannelConfig]) -> None:
        """Downloads each logo whose address is new; runs once at startup."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            event(log, logging.WARNING, "logos_unavailable", error=str(error))
            return
        for channel in channels:
            if channel.logo and not self._current(channel.id, channel.logo):
                self._fetch(channel.id, channel.logo)

    def _current(self, channel_id: str, url: str) -> bool:
        try:
            saved = (self._dir / f"{channel_id}.url").read_text(encoding="utf-8")
        except OSError:
            return False
        return saved == url and self.path_for(channel_id) is not None

    def _fetch(self, channel_id: str, url: str) -> None:
        try:
            body = self._get(url)
        except HttpError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        kind = sniff(body)
        if kind is None:
            event(log, logging.WARNING, "logo_rejected", channel=channel_id, bytes=len(body))
            return
        try:
            partial = self._dir / f".{channel_id}.tmp"
            partial.write_bytes(body)
            for other in EXTENSIONS:
                if other != kind:
                    (self._dir / f"{channel_id}.{other}").unlink(missing_ok=True)
            partial.replace(self._dir / f"{channel_id}.{kind}")
            (self._dir / f"{channel_id}.url").write_text(url, encoding="utf-8")
        except OSError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        event(log, logging.INFO, "logo_saved", channel=channel_id, kind=kind, bytes=len(body))
