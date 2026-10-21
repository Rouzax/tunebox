"""Free space on the data folder's file system. tunebox.db, the logo cache,
uploads and the log share it: a write an admin asks for is refused while
less than MIN_FREE would be left, so the database can always be written."""

from __future__ import annotations

import shutil
from pathlib import Path

MIN_FREE = 268_435_456


def free_bytes(path: Path) -> int | None:
    """The bytes free for tunebox under path; None when it cannot be read."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def has_room(free: int | None, adding: int) -> bool:
    """Whether adding bytes leaves MIN_FREE. Unknown free space is no reason
    to refuse: the write then fails, or not, on its own."""
    return free is None or free - adding >= MIN_FREE
