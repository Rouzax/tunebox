"""Uploaded logos, stored by their SHA-256 under blobs/ in the data folder,
apart from the logo cache in logos/ (which can be deleted). The file is
the record: a station's logo names blob:<sha256>, and the blob exists when
its file does, also after the database was reset (export, reset, import).
Unreferenced blobs are removed at start-up once they are a day old, so an
upload whose station has not been saved yet survives a restart."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from tunebox.config import BLOB_REF, ConfigState
from tunebox.logos import EXTENSIONS, sniff

MAX_BLOB = 2_000_000


class BlobError(Exception):
    pass


class BlobUnavailable(Exception):
    """The folder cannot be written (disk full, read-only); carries errno."""

    def __init__(self, errno: int | None) -> None:
        super().__init__(errno)
        self.errno = errno


def _holds(path: Path, body: bytes) -> bool:
    try:
        return path.stat().st_size == len(body) and path.read_bytes() == body
    except OSError:
        return False


def _sync(path: Path) -> None:
    """Flushes the file to disk before it is renamed into place, so a power
    loss cannot leave an empty file under the content's name."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Blobs:
    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._clock = clock

    def save(self, body: bytes) -> tuple[str, str]:
        """Store the image and answer (reference, kind). The kind comes from
        the first bytes, never from what the uploader declared."""
        if not body or len(body) > MAX_BLOB:
            raise BlobError("empty or too large")
        kind = sniff(body)
        if kind is None:
            raise BlobError("not an image")
        digest = hashlib.sha256(body).hexdigest()
        target = self._dir / f"{digest}.{kind}"
        if _holds(target, body):
            # The grace before collection counts from the latest upload.
            with contextlib.suppress(OSError):
                now = self._clock()
                os.utime(target, (now, now))
        else:
            # A unique temp name: two uploads of the same file at once must
            # not write one half-file over the other. An existing file under
            # this name that differs was cut short by a crash and is replaced.
            partial = self._dir / f".{digest}.{secrets.token_hex(4)}.tmp"
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(body)
                _sync(partial)
                partial.replace(target)
            except OSError as error:
                # On a read-only folder the cleanup fails too; a leftover dot
                # file goes at the next start-up.
                with contextlib.suppress(OSError):
                    partial.unlink(missing_ok=True)
                raise BlobUnavailable(error.errno) from error
        return f"blob:{digest}", kind

    def path(self, ref: str) -> Path | None:
        """The stored file of a reference, None when it is not a reference
        (exactly 64 lowercase hex digits, so never a path) or is absent."""
        match = BLOB_REF.fullmatch(ref)
        if match is None:
            return None
        for kind in EXTENSIONS:
            candidate = self._dir / f"{match.group(1)}.{kind}"
            if candidate.is_file():
                return candidate
        return None

    def missing(self, state: ConfigState) -> str | None:
        """The first station logo that names a blob that is not stored, with
        its field, for a save to refuse."""
        for index, row in enumerate(state.channels):
            logo = row.doc.get("logo")
            if isinstance(logo, str) and logo.startswith("blob:") and self.path(logo) is None:
                return f"channels[{index}].logo: no uploaded logo {logo}"
        return None

    def collect(self, keep: set[str], older_than_s: float) -> int:
        """Remove the blobs not in keep (references) that are older than
        older_than_s; the count removed."""
        removed = 0
        limit = self._clock() - older_than_s
        try:
            files = list(self._dir.iterdir())
        except OSError:
            return 0
        for path in files:
            # Runs at start-up, when no upload is in progress: an old dot file
            # is a temp file a crash left behind.
            if f"blob:{path.stem}" in keep:
                continue
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
