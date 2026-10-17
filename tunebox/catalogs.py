"""Searches Deezer and iTunes for a track's cover, then Deezer for an artist photo."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from tunebox.http import HttpError, HttpGet
from tunebox.logs import event
from tunebox.matching import Candidate, base_title, best_match
from tunebox.tracks import Track, norm

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"
ITUNES = "https://itunes.apple.com/search"
DEEZER_INTERVAL = 0.2
# Apple allows roughly 20 searches a minute per client.
ITUNES_INTERVAL = 3.1


@dataclass(frozen=True)
class Found:
    url: str
    kind: Literal["album", "artist"]
    match: str


class SearchError(Exception):
    """A catalogue could not be asked, so finding nothing is not a real miss."""


class Throttle:
    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next = self._clock() + self._interval


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value: object = data.get(key)
    if not isinstance(value, list):
        return []
    return [cast("dict[str, Any]", i) for i in cast("list[object]", value) if isinstance(i, dict)]


def _text(item: dict[str, Any], *path: str) -> str:
    value: object = item
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = cast("dict[str, object]", value).get(key)
    return value if isinstance(value, str) else ""


class Catalogs:
    """find() is called from the one cover worker thread; the throttles are
    still thread-safe so a second caller could not break the rate limits."""

    def __init__(
        self,
        http_get: HttpGet,
        deezer_interval: float = DEEZER_INTERVAL,
        itunes_interval: float = ITUNES_INTERVAL,
    ) -> None:
        self._get = http_get
        self._deezer = Throttle(deezer_interval)
        self._itunes = Throttle(itunes_interval)

    def find(self, track: Track) -> Found | None:
        failures: list[str] = []
        found = (
            self._deezer_song(track, failures)
            or self._itunes_song(track, failures)
            or self._artist_photo(track, failures)
        )
        if found is None and failures:
            raise SearchError("; ".join(failures))
        return found

    def _json(self, url: str, throttle: Throttle, failures: list[str]) -> dict[str, Any]:
        throttle.wait()
        where = url.split("?")[0]
        try:
            data: object = json.loads(self._get(url))
        except (HttpError, ValueError) as error:
            failures.append(str(error))
            event(log, logging.WARNING, "search_failed", url=where, error=str(error))
            return {}
        if not isinstance(data, dict):
            failures.append(f"{where}: not a JSON object")
            event(log, logging.WARNING, "search_failed", url=where, error="not a JSON object")
            return {}
        result = cast("dict[str, Any]", data)
        if "error" in result:
            # Deezer reports quota and query errors with status 200 and an error object.
            failures.append(f"{where}: {result['error']}")
            event(log, logging.WARNING, "search_failed", url=where, error=str(result["error"]))
            return {}
        return result

    def _deezer_song(self, track: Track, failures: list[str]) -> Found | None:
        first, base = track.artists[0], base_title(track.title)
        queries = [f'artist:"{first}" track:"{track.title}"']
        if base != track.title:
            queries.append(f'artist:"{first}" track:"{base}"')
        queries.append(f"{first} {base}")
        for q in queries:
            url = f"{DEEZER}/search?" + urllib.parse.urlencode({"q": q, "limit": 10})
            data = self._json(url, self._deezer, failures)
            candidates = [
                Candidate(
                    _text(i, "artist", "name"), _text(i, "title"), _text(i, "album", "cover_xl")
                )
                for i in _items(data, "data")
            ]
            if found := best_match(track.artists, track.title, candidates):
                return Found(found.image, "album", f"deezer: {found.artist} - {found.title}")
        return None

    def _itunes_song(self, track: Track, failures: list[str]) -> Found | None:
        first, base = track.artists[0], base_title(track.title)
        terms = [f"{first} {track.title}"]
        if base != track.title:
            terms.append(f"{first} {base}")
        for term in terms:
            params = {"term": term, "media": "music", "entity": "song", "limit": 10}
            url = f"{ITUNES}?" + urllib.parse.urlencode(params)
            data = self._json(url, self._itunes, failures)
            candidates = [
                Candidate(
                    _text(i, "artistName"),
                    _text(i, "trackName"),
                    _text(i, "artworkUrl100").replace("100x100bb", "1000x1000bb"),
                )
                for i in _items(data, "results")
            ]
            if found := best_match(track.artists, track.title, candidates):
                return Found(found.image, "album", f"itunes: {found.artist} - {found.title}")
        return None

    def _artist_photo(self, track: Track, failures: list[str]) -> Found | None:
        for name in track.artists:
            url = f"{DEEZER}/search/artist?" + urllib.parse.urlencode({"q": name, "limit": 5})
            for item in _items(self._json(url, self._deezer, failures), "data"):
                picture = _text(item, "picture_xl")
                # Deezer's placeholder for an artist without a photo has an empty id.
                if (
                    norm(_text(item, "name")) == norm(name)
                    and picture
                    and "/artist//" not in picture
                ):
                    return Found(picture, "artist", f"deezer artist: {_text(item, 'name')}")
        return None
