"""The track lists on NPO's radio sites."""

from __future__ import annotations

from typing import Any, cast
from zoneinfo import ZoneInfo

from tunebox.http import HttpError, HttpGet
from tunebox.sources import jsonfeed
from tunebox.sources.base import SourceError
from tunebox.tracks import RawEntry

SITES = {
    "nporadio1": "https://www.nporadio1.nl/api/tracks",
    "nporadio2": "https://www.nporadio2.nl/api/tracks",
    "npo3fm": "https://www.npo3fm.nl/api/tracks",
}
# The sites give local times without an offset.
AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def parse(body: bytes) -> list[RawEntry]:
    data = jsonfeed.load(body)
    items = cast("dict[str, Any]", data).get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SourceError("unexpected answer: no data list")
    entries: list[RawEntry] = []
    for raw in cast("list[object]", items):
        if not isinstance(raw, dict):
            raise SourceError("unexpected track entry")
        item = cast("dict[str, Any]", raw)
        started = jsonfeed.time_ms(item.get("startdatetime"), AMSTERDAM)
        if started is None:
            continue
        entries.append(
            RawEntry(jsonfeed.text(item.get("artist")), jsonfeed.text(item.get("title")), started)
        )
    return jsonfeed.newest_first(entries)


class NpoSource:
    def __init__(self, site: str, http_get: HttpGet) -> None:
        self._url = SITES[site]
        self._get = http_get

    def fetch(self, count: int) -> list[RawEntry]:
        try:
            body = self._get(self._url)
        except HttpError as error:
            raise SourceError(str(error)) from error
        return parse(body)[:count]
