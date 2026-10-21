"""Station logos: downloaded from the configured address when it is new or
changes, then served by tunebox, so listeners' browsers never contact the
logo host. A large PNG also gets a small copy in small/, for the station
list: logo hosts often offer 512 px for an icon shown at about 40, and on
mobile data the list would cost megabytes. The original stays for the cover."""

from __future__ import annotations

import logging
import queue
import re
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from tunebox.config import BLOB_REF, ChannelConfig
from tunebox.http import HttpError, HttpGet
from tunebox.logs import event
from tunebox.png import PngError, shrink

if TYPE_CHECKING:
    from tunebox.blobs import Blobs

log = logging.getLogger(__name__)

EXTENSIONS = ("png", "jpg", "webp", "svg")
CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}
# Sharp at about 40 CSS px on a 3x screen.
SMALL_PX = 128
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(png|jpg|webp|svg)$")


def sniff(body: bytes) -> str | None:
    """The image kind by its first bytes; a host's Content-Type is not trusted."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    head = body[:1024].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith(b"<svg"):
        return "svg"
    # An XML declaration, comment or SVG doctype may come first; an HTML
    # page may hold an inline <svg> and is not a logo.
    prologue = head.startswith((b"<?xml", b"<!--", b"<!doctype svg"))
    if prologue and b"<svg" in head and b"<html" not in head:
        return "svg"
    return None


class Logos:
    def __init__(self, directory: Path, http_get: HttpGet, blobs: Blobs | None = None) -> None:
        self._dir = directory
        self._get = http_get
        self._blobs = blobs
        self._queue: queue.Queue[ChannelConfig] = queue.Queue()
        self._unavailable_reported = False

    def _folder(self, small: bool) -> Path:
        return self._dir / "small" if small else self._dir

    def path_for(self, channel_id: str, small: bool = False) -> str | None:
        """The logo's address, versioned by the file's modification time so
        it can be cached for good: a replaced logo gets a new address."""
        prefix = "/logos/small" if small else "/logos"
        for ext in ("png",) if small else EXTENSIONS:
            try:
                saved = (self._folder(small) / f"{channel_id}.{ext}").stat()
            except OSError:
                continue
            if not stat.S_ISREG(saved.st_mode):
                continue
            return f"{prefix}/{channel_id}.{ext}?v={saved.st_mtime_ns:x}"
        return None

    def file(self, name: str, small: bool = False) -> Path | None:
        if not _NAME.match(name) or (small and not name.endswith(".png")):
            return None
        path = self._folder(small) / name
        return path if path.is_file() else None

    def request(self, channel: ChannelConfig) -> None:
        """Queues a channel whose logo is new or changed."""
        self._queue.put(channel)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                channel = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.refresh(channel)

    def refresh(self, channel: ChannelConfig) -> None:
        """Downloads the logo when its address is new, or copies an uploaded
        one, and keeps the small copy in step."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            if not self._unavailable_reported:
                self._unavailable_reported = True
                event(log, logging.WARNING, "logos_unavailable", error=str(error))
            return
        if channel.logo and not self._current(channel.id, channel.logo):
            if BLOB_REF.fullmatch(channel.logo):
                self._copy_blob(channel.id, channel.logo)
            else:
                self._fetch(channel.id, channel.logo)
        self._make_small(channel.id)

    def _make_small(self, channel_id: str) -> None:
        """Keeps small/<id>.png in step with the logo: made when the logo is
        a PNG larger than SMALL_PX and newer than the copy, removed otherwise."""
        original = self._dir / f"{channel_id}.png"
        copy = self._dir / "small" / f"{channel_id}.png"
        try:
            if not original.is_file():
                copy.unlink(missing_ok=True)
                return
            if copy.is_file() and copy.stat().st_mtime_ns >= original.stat().st_mtime_ns:
                return
            body = original.read_bytes()
        except OSError as error:
            event(log, logging.WARNING, "logo_not_shrunk", channel=channel_id, error=str(error))
            return
        try:
            small = shrink(body, SMALL_PX)
        except PngError as error:
            event(log, logging.WARNING, "logo_not_shrunk", channel=channel_id, error=str(error))
            small = None
        try:
            if small is None:
                copy.unlink(missing_ok=True)
                return
            copy.parent.mkdir(exist_ok=True)
            partial = copy.parent / f".{channel_id}.tmp"
            partial.write_bytes(small)
            partial.replace(copy)
        except OSError as error:
            event(log, logging.WARNING, "logo_not_shrunk", channel=channel_id, error=str(error))
            return
        event(
            log,
            logging.INFO,
            "logo_shrunk",
            channel=channel_id,
            bytes=len(body),
            small_bytes=len(small),
        )

    def _current(self, channel_id: str, url: str) -> bool:
        try:
            saved = (self._dir / f"{channel_id}.url").read_text(encoding="utf-8")
        except OSError:
            return False
        return saved == url and self.path_for(channel_id) is not None

    def _copy_blob(self, channel_id: str, ref: str) -> None:
        source = self._blobs.path(ref) if self._blobs else None
        if source is None:
            event(log, logging.WARNING, "logo_missing", channel=channel_id, logo=ref)
            return
        try:
            body = source.read_bytes()
        except OSError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        kind = source.suffix.removeprefix(".")
        if sniff(body) != kind:
            event(log, logging.WARNING, "logo_rejected", channel=channel_id, bytes=len(body))
            return
        self._store(channel_id, body, kind, ref)

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
        self._store(channel_id, body, kind, url)

    def _store(self, channel_id: str, body: bytes, kind: str, source: str) -> None:
        """Makes body the channel's logo and records source (an address or a
        blob reference) as what it came from."""
        try:
            partial = self._dir / f".{channel_id}.tmp"
            partial.write_bytes(body)
            for other in EXTENSIONS:
                if other != kind:
                    (self._dir / f"{channel_id}.{other}").unlink(missing_ok=True)
            partial.replace(self._dir / f"{channel_id}.{kind}")
            (self._dir / f"{channel_id}.url").write_text(source, encoding="utf-8")
        except OSError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        event(log, logging.INFO, "logo_saved", channel=channel_id, kind=kind, bytes=len(body))
