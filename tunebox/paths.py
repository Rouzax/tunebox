"""Serving files from a folder: the one rule both the player and the admin
pages use to keep a request inside its folder."""

from __future__ import annotations

from pathlib import Path


def inside(root: Path, relative: str) -> Path | None:
    """The file relative names under root, resolved; None when it is
    outside root (by .. or a link), a folder, or missing. root must be
    resolved already."""
    try:
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except OSError, ValueError:  # ValueError: an embedded NUL byte
        pass
    return None
