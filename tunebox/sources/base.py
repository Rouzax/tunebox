"""What every source of now-playing data provides."""

from __future__ import annotations

from typing import Protocol

from tunebox.tracks import RawEntry


class SourceError(Exception):
    """The source could not be asked or its answer could not be read."""


class Source(Protocol):
    def fetch(self, count: int) -> list[RawEntry]:
        """Up to count entries, newest first. A source that only knows the
        current track returns one entry without a start time."""
        ...
