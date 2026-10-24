"""Cover images in covers/ under the data folder: one file per catalogue link,
named by the link's SHA-256, so the file is the record and the database
needs no column for it. A disposable cache like logos/: deleting it costs
only downloads. Access time records when tunebox last handed a cover out
(set explicitly, whatever the mount does); modification time versions the
address, so a browser may keep a cover for good."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from tunebox.disk import free_bytes, has_room
from tunebox.logs import event
from tunebox.paths import inside

log = logging.getLogger(__name__)

MAX_COVER = 2_000_000
MAX_COVERS = 500_000_000
# Cleaning stops here rather than at the cap, so it does not run on every download.
LOW_WATER = 450_000_000
TOUCH_S = 86_400
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
KINDS = tuple(CONTENT_TYPES)
# A partial file is never in use for this long, so an older one is a crash's leftover.
PARTIAL_STALE_S = 3600
_NAME = re.compile(rf"[0-9a-f]{{32}}\.({'|'.join(KINDS)})")
_PARTIAL = re.compile(r"\.[0-9a-f]+\.tmp")


def sniff_cover(body: bytes) -> str | None:
    """The image kind by its first bytes; a host's Content-Type is not trusted.
    Raster only: a cover is shown as an <img> and sampled on a canvas."""
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return None


def name_for(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:32]


def sized(link: str) -> str:
    """The link at 600x600: sharp at the largest cover the player draws
    (about 512 CSS px) at a third of the 1000x1000 bytes. Deezer takes the
    size in the path, Apple in the file name; other links stay as they are."""
    if "/1000x1000-" in link:
        return link.replace("/1000x1000-", "/600x600-", 1)
    if link.endswith("/1000x1000bb.jpg"):
        return link[: -len("1000x1000bb.jpg")] + "600x600bb.jpg"
    return link


class SaveResult(Enum):
    SAVED = "saved"
    UNCHANGED = "unchanged"
    NOT_IMAGE = "not_image"
    TOO_LARGE = "too_large"
    DISK_LOW = "disk_low"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CleanReport:
    files_removed: int
    bytes_freed: int
    files: int
    bytes: int


class CoverFiles:
    def __init__(
        self,
        directory: Path,
        free: Callable[[], int | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = directory
        self._free = free or (lambda: free_bytes(directory.parent))
        self._clock = clock
        self._touched: dict[str, float] = {}

    def free(self) -> int | None:
        return self._free()

    def _stat(self, link: str) -> tuple[Path, os.stat_result] | None:
        name = name_for(link)
        for kind in KINDS:
            path = self._dir / f"{name}.{kind}"
            try:
                info = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                return path, info
        return None

    def path_for(self, link: str) -> Path | None:
        found = self._stat(link)
        return found[0] if found else None

    def found(self, link: str) -> tuple[Path, str] | None:
        """The file for the link and its address, from one stat of the file."""
        found = self._stat(link)
        if found is None:
            return None
        path, info = found
        return path, f"/covers/{path.name}?v={info.st_mtime_ns:x}"

    def address_for(self, link: str) -> str | None:
        found = self.found(link)
        return found[1] if found else None

    def save(self, link: str, body: bytes) -> SaveResult:
        if len(body) > MAX_COVER:
            return SaveResult.TOO_LARGE
        kind = sniff_cover(body)
        if kind is None:
            return SaveResult.NOT_IMAGE
        existing = self.path_for(link)
        if existing is not None and existing.suffix == f".{kind}":
            with contextlib.suppress(OSError):
                if existing.read_bytes() == body:
                    return SaveResult.UNCHANGED
        if not has_room(self._free(), len(body)):
            return SaveResult.DISK_LOW
        target = self._dir / f"{name_for(link)}.{kind}"
        partial = self._dir / f".{secrets.token_hex(8)}.tmp"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            partial.replace(target)
        except OSError:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            return SaveResult.UNAVAILABLE
        if existing is not None and existing != target:
            with contextlib.suppress(OSError):
                existing.unlink(missing_ok=True)
        return SaveResult.SAVED

    def touch(self, path: Path) -> None:
        """Marks the cover as handed out now, at most once a day per file;
        the modification time, and so the address, stays."""
        now = self._clock()
        if now - self._touched.get(path.name, -TOUCH_S) < TOUCH_S:
            return
        self._touched[path.name] = now
        with contextlib.suppress(OSError):
            # Nanoseconds: a float mtime rounds and would change the address.
            os.utime(path, ns=(int(now * 1e9), path.stat().st_mtime_ns))

    def file(self, name: str) -> Path | None:
        if not _NAME.fullmatch(name):
            return None
        with contextlib.suppress(OSError):
            return inside(self._dir.resolve(), name)
        return None

    def clean(self, keep: set[str] | None) -> CleanReport:
        """Removes files no decision's link names (all of them stay when keep
        is None, the links being unknown), then the least recently
        handed-out files while the total is over MAX_COVERS, down to
        LOW_WATER. Also removes partial files a crash left behind. A file
        that cannot be removed is logged and skipped."""
        removed = freed = 0
        entries: list[tuple[float, int, Path]] = []
        try:
            listing = list(self._dir.iterdir())
        except OSError:
            listing = []
        for path in listing:
            partial = _PARTIAL.fullmatch(path.name) is not None
            if not partial and not _NAME.fullmatch(path.name):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if partial:
                if self._clock() - info.st_mtime > PARTIAL_STALE_S and self._remove(path):
                    removed, freed = removed + 1, freed + info.st_size
                continue
            if keep is not None and path.name.split(".")[0] not in keep:
                if self._remove(path):
                    removed, freed = removed + 1, freed + info.st_size
                continue
            entries.append((info.st_atime, info.st_size, path))
        total = sum(size for _, size, _ in entries)
        if total > MAX_COVERS:
            for _, size, path in sorted(entries, key=lambda e: e[0]):
                if total <= LOW_WATER:
                    break
                if self._remove(path):
                    removed, freed, total = removed + 1, freed + size, total - size
                elif not path.exists():
                    total -= size  # gone by other means: no longer in the folder's total
        files = sum(1 for _, _, path in entries if path.exists())
        return CleanReport(removed, freed, files, total)

    def _remove(self, path: Path) -> bool:
        """True only when this call removed the file; one that vanished
        meanwhile, or could not be removed, is False."""
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            event(log, logging.WARNING, "cover_not_removed", file=path.name, error=str(error))
            return False
        self._touched.pop(path.name, None)
        return True
