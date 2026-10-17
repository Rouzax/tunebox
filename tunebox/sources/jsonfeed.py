"""Shared reading of JSON now-playing answers."""

from __future__ import annotations

import json
from datetime import datetime, tzinfo

from tunebox.sources.base import SourceError
from tunebox.tracks import RawEntry


def load(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError as error:  # covers bad JSON and bad UTF-8
        raise SourceError(f"unreadable answer: {error}") from error


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def time_ms(value: object, zone: tzinfo | None = None) -> int | None:
    """Epoch milliseconds of an ISO 8601 time. A time without an offset is
    read in zone; without a zone it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        if zone is None:
            return None
        moment = moment.replace(tzinfo=zone)
    return int(moment.timestamp() * 1000)


def newest_first(entries: list[RawEntry]) -> list[RawEntry]:
    return sorted(entries, key=lambda e: e.started_at_ms or 0, reverse=True)
