"""Station logos: downloaded from the configured address when it is new or
changes, then served by tunebox, so listeners' browsers never contact the
logo host. A large PNG also gets a small copy in small/, for the station
list: logo hosts often offer 512 px for an icon shown at about 40, and on
mobile data the list would cost megabytes. The original stays for the cover.
A channel's default_image is downloaded the same way into default/."""

from __future__ import annotations

import logging
import queue
import re
import secrets
import stat
import threading
from collections.abc import Collection
from dataclasses import dataclass
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
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\.(png|jpg|webp|svg)")
# What a channel leaves in a folder: its image or recorded source, or a partial file.
_CHANNEL_FILE = re.compile(r"\.?([a-z0-9][a-z0-9-]*)\.(?:png|jpg|webp|svg|url|tmp)")


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


def _is_local_path(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


def default_image_address(value: str | None, downloaded: str | None) -> str | None:
    """What the player gets as a station's default image. Its CSP allows
    images from tunebox only, so an https:// value is replaced by the local
    copy (None until downloaded), a path on tunebox passes unchanged, and
    anything else (http://, data:, a relative or protocol-relative address)
    is withheld."""
    if value is None:
        return None
    if value.startswith("https://"):
        return downloaded
    return value if _is_local_path(value) else None


def _unlink(path: Path) -> bool:
    """Removes a file; False when there was none."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True)
class _Forget:
    channel_id: str


class Logos:
    def __init__(self, directory: Path, http_get: HttpGet, blobs: Blobs) -> None:
        self._dir = directory
        self._get = http_get
        self._blobs = blobs
        self._queue: queue.Queue[ChannelConfig | _Forget] = queue.Queue()
        self._unavailable_reported = False
        self._unusable_reported: set[tuple[str, str]] = set()
        # The apply thread, request threads (revision reads) and the logo worker
        # all reach the counter. The random part makes a restart differ from any
        # revision a player already holds.
        self._boot = secrets.token_hex(4)
        self._changes = 0
        self._changes_lock = threading.Lock()

    def revision(self) -> str:
        """Changes whenever a logo, small logo or default image is saved or
        removed, and differs between runs; the player compares it to know its
        station list names stale image addresses."""
        with self._changes_lock:
            return f"{self._boot}.{self._changes}"

    def _changed(self) -> None:
        with self._changes_lock:
            self._changes += 1

    def _folder(self, small: bool, default: bool = False) -> Path:
        if default:
            return self._dir / "default"
        return self._dir / "small" if small else self._dir

    def path_for(self, channel_id: str, small: bool = False, default: bool = False) -> str | None:
        """The logo's (or, with default, the default image's) address,
        versioned by the file's modification time so it can be cached for
        good: a replaced file gets a new address."""
        folder = self._folder(small, default)
        prefix = "/logos/default" if default else "/logos/small" if small else "/logos"
        for ext in ("png",) if small else EXTENSIONS:
            try:
                saved = (folder / f"{channel_id}.{ext}").stat()
            except OSError:
                continue
            if not stat.S_ISREG(saved.st_mode):
                continue
            return f"{prefix}/{channel_id}.{ext}?v={saved.st_mtime_ns:x}"
        return None

    def file(self, name: str, small: bool = False, default: bool = False) -> Path | None:
        if not _NAME.fullmatch(name) or (small and not name.endswith(".png")):
            return None
        path = self._folder(small, default) / name
        return path if path.is_file() else None

    def request(self, channel: ChannelConfig) -> None:
        """Queues a channel whose logo or default image is new or changed."""
        self._queue.put(channel)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once(timeout=0.5)

    def run_once(self, timeout: float = 0) -> bool:
        """Handles the next queued item, in the order queued; False when none came."""
        try:
            item = self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        except queue.Empty:
            return False
        if isinstance(item, _Forget):
            self._remove_files(item.channel_id)
        else:
            self.refresh(item)
        return True

    def refresh(self, channel: ChannelConfig) -> None:
        """Downloads the logo when its address is new, or copies an uploaded
        one, and keeps the small copy in step; the default image is kept in
        step the same way."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            if not self._unavailable_reported:
                self._unavailable_reported = True
                event(log, logging.WARNING, "logos_unavailable", error=str(error))
            return
        if channel.logo and not self._current(channel.id, channel.logo, self._dir, default=False):
            if BLOB_REF.fullmatch(channel.logo):
                self._copy_blob(channel.id, channel.logo)
            else:
                self._fetch(channel.id, channel.logo, self._dir)
        self._make_small(channel.id)
        self._refresh_default(channel)

    def _refresh_default(self, channel: ChannelConfig) -> None:
        """Keeps default/<id>.* in step with default_image: downloaded for
        an https:// address, absent for a path on tunebox itself (sent to the
        player as it is) and for anything the CSP or the logo rules refuse."""
        value = channel.default_image
        folder = self._folder(False, default=True)
        if value is not None and value.startswith("https://"):
            if not self._current(channel.id, value, folder, default=True):
                self._fetch(channel.id, value, folder)
            return
        if value is not None and not _is_local_path(value):
            key = (channel.id, value)
            if key not in self._unusable_reported:
                self._unusable_reported.add(key)
                event(log, logging.WARNING, "default_image_unusable", channel=channel.id)
        for name in (*(f"{channel.id}.{ext}" for ext in EXTENSIONS), f"{channel.id}.url"):
            try:
                if _unlink(folder / name):
                    self._changed()
            except OSError as error:
                event(
                    log,
                    logging.WARNING,
                    "logo_failed",
                    channel=channel.id,
                    kind="default",
                    error=str(error),
                )

    def forget(self, channel_id: str) -> None:
        """Queues the removal of everything downloaded for a channel that is
        no longer live (deleted or disabled); enabling it again downloads
        again. Going through the queue puts it after any refresh of the same
        channel, so a refresh cannot bring the files back."""
        self._queue.put(_Forget(channel_id))

    def forget_all_except(self, live_ids: Collection[str]) -> None:
        """Queues the removal of the files of every channel that has some in
        the logo folders but is not live: those of a station removed while
        tunebox was stopped. Names that are not a channel's files are left."""
        gone: set[str] = set()
        for folder in (self._dir, self._dir / "small", self._dir / "default"):
            try:
                names = [path.name for path in folder.iterdir()]
            except OSError:
                continue
            for name in names:
                found = _CHANNEL_FILE.fullmatch(name)
                if found and found.group(1) not in live_ids:
                    gone.add(found.group(1))
        for channel_id in sorted(gone):
            self.forget(channel_id)

    def _remove_files(self, channel_id: str) -> None:
        """The logo and its small copy, and the default image, each with its
        recorded source, and a partial file a crash left. A missing file is fine."""
        names = (*(f"{channel_id}.{ext}" for ext in EXTENSIONS), f"{channel_id}.url")
        paths = [
            *(self._dir / name for name in names),
            self._dir / "small" / f"{channel_id}.png",
            *(self._folder(False, default=True) / name for name in names),
            *(folder / f".{channel_id}.tmp" for folder in (self._dir, self._dir / "small")),
            self._folder(False, default=True) / f".{channel_id}.tmp",
        ]
        for path in paths:
            try:
                if _unlink(path):
                    self._changed()
            except OSError as error:
                event(
                    log,
                    logging.WARNING,
                    "logo_failed",
                    channel=channel_id,
                    kind="forget",
                    error=str(error),
                )

    def _make_small(self, channel_id: str) -> None:
        """Keeps small/<id>.png in step with the logo: made when the logo is
        a PNG larger than SMALL_PX and newer than the copy, removed otherwise."""
        original = self._dir / f"{channel_id}.png"
        copy = self._dir / "small" / f"{channel_id}.png"
        try:
            if not original.is_file():
                if _unlink(copy):
                    self._changed()
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
                if _unlink(copy):
                    self._changed()
                return
            copy.parent.mkdir(exist_ok=True)
            partial = copy.parent / f".{channel_id}.tmp"
            partial.write_bytes(small)
            partial.replace(copy)
            self._changed()
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

    def _current(self, channel_id: str, url: str, folder: Path, default: bool) -> bool:
        try:
            saved = (folder / f"{channel_id}.url").read_text(encoding="utf-8")
        except OSError:
            return False
        return saved == url and self.path_for(channel_id, default=default) is not None

    def _copy_blob(self, channel_id: str, ref: str) -> None:
        source = self._blobs.path(ref)
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
        self._store(channel_id, body, kind, ref, self._dir)

    def _fetch(self, channel_id: str, url: str, folder: Path) -> None:
        try:
            body = self._get(url)
        except HttpError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        kind = sniff(body)
        if kind is None:
            event(log, logging.WARNING, "logo_rejected", channel=channel_id, bytes=len(body))
            return
        self._store(channel_id, body, kind, url, folder)

    def _store(self, channel_id: str, body: bytes, kind: str, source: str, folder: Path) -> None:
        """Makes body the channel's logo (or default image, by folder) and
        records source (an address or a blob reference) as what it came from."""
        visible = False
        try:
            folder.mkdir(exist_ok=True)
            partial = folder / f".{channel_id}.tmp"
            partial.write_bytes(body)
            # From here a served file may disappear or be replaced.
            visible = True
            for other in EXTENSIONS:
                if other != kind:
                    (folder / f"{channel_id}.{other}").unlink(missing_ok=True)
            partial.replace(folder / f"{channel_id}.{kind}")
            (folder / f"{channel_id}.url").write_text(source, encoding="utf-8")
        except OSError as error:
            event(log, logging.WARNING, "logo_failed", channel=channel_id, error=str(error))
            return
        finally:
            # Also after a failure past that point; before it nothing a player
            # can see has changed, so the revision stays.
            if visible:
                self._changed()
        event(log, logging.INFO, "logo_saved", channel=channel_id, kind=kind, bytes=len(body))
