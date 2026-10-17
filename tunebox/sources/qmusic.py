"""Qmusic's public play history."""

from __future__ import annotations

import urllib.parse
from typing import Any, cast

from tunebox.http import HttpError, HttpGet
from tunebox.sources import jsonfeed
from tunebox.sources.base import SourceError
from tunebox.tracks import RawEntry

FEED = "https://api.qmusic.nl/2.4/tracks/plays"
# The API answers at most this many plays.
MAX_FETCH = 50


def parse(body: bytes) -> list[RawEntry]:
    data = jsonfeed.load(body)
    plays = cast("dict[str, Any]", data).get("played_tracks") if isinstance(data, dict) else None
    if not isinstance(plays, list):
        raise SourceError("unexpected answer: no played_tracks list")
    entries: list[RawEntry] = []
    for raw in cast("list[object]", plays):
        if not isinstance(raw, dict):
            raise SourceError("unexpected play entry")
        play = cast("dict[str, Any]", raw)
        started = jsonfeed.time_ms(play.get("played_at"))
        # A play without a time cannot be placed in the history.
        if started is None:
            continue
        artist: object = play.get("artist")
        name = cast("dict[str, Any]", artist).get("name") if isinstance(artist, dict) else None
        entries.append(RawEntry(jsonfeed.text(name), jsonfeed.text(play.get("title")), started))
    return jsonfeed.newest_first(entries)


class QmusicSource:
    def __init__(self, station: str, http_get: HttpGet) -> None:
        self._station = station
        self._get = http_get

    def fetch(self, count: int) -> list[RawEntry]:
        query = urllib.parse.urlencode(
            {"limit": min(count, MAX_FETCH), "_station_id": self._station}
        )
        try:
            body = self._get(f"{FEED}?{query}")
        except HttpError as error:
            raise SourceError(str(error)) from error
        return parse(body)
