"""Searches Deezer and iTunes for a track's cover, then Deezer for an artist photo."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from tunebox.http import HttpError, HttpGet
from tunebox.logs import event
from tunebox.matching import (
    Candidate,
    artists_match,
    base_title,
    best_match,
    lead_artist,
    search_names,
)
from tunebox.tracks import Track, norm

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"
ITUNES = "https://itunes.apple.com/search"
DEEZER_INTERVAL = 0.2
# Apple allows roughly 20 searches a minute per client.
ITUNES_INTERVAL = 3.1
# Releases compared with an artist picture; an artist with more keeps the
# older ones unchecked.
RELEASES_CHECKED = 100
# Deezer serves artist pictures and release covers from one image store, so
# a picture that is a release cover carries that cover's id.
_IMAGE_ID = re.compile(r"/images/(?:artist|cover)/([0-9a-f]{32})/")


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


def _image_id(link: str) -> str | None:
    found = _IMAGE_ID.search(link)
    return found.group(1) if found else None


def _same_image(one: str, other: str) -> bool:
    found = _image_id(one)
    return found is not None and found == _image_id(other)


def _id(item: dict[str, Any], *path: str) -> str:
    value: object = item
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = cast("dict[str, object]", value).get(key)
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else ""


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

    def artist_photo(self, track: Track) -> Found | None:
        """The artists' photo alone, shown beside a song's own cover. The
        song's own Deezer entry names its artist, which a name search cannot
        tell apart from others of the same name (Deezer has several Rafaels,
        and "Rafa'EL" reads as "rafael" too); the name search is for a song
        Deezer does not have."""
        failures: list[str] = []
        song = self._deezer_match(track, failures)
        # Deezer may list a song under its second artist; the photo is of
        # the first-named one, as the name search would choose.
        lead = lead_artist(track.artists[0])
        if (
            song is not None
            and _real_picture(song.artist_image)
            and artists_match([lead], song.artist)
            and not _same_image(song.artist_image, song.image)
            and self._not_a_release(song.artist_id, song.artist_image, failures)
        ):
            return Found(song.artist_image, "artist", f"deezer artist: {song.artist}")
        found = self._artist_photo(track, failures)
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
        found = self._deezer_match(track, failures)
        if found is None:
            return None
        return Found(found.image, "album", f"deezer: {found.artist} - {found.title}")

    def _deezer_match(self, track: Track, failures: list[str]) -> Candidate | None:
        names = search_names(track.artists)
        first, base = track.artists[0], base_title(track.title)
        queries = [f'artist:"{first}" track:"{track.title}"']
        if base != track.title:
            queries.append(f'artist:"{first}" track:"{base}"')
        # A duet sent as one name ("AVICII & ALOE BLACC") is searched loosely
        # by its first-named artist first: on the full name Deezer ranks
        # songs by the second-named one, covers and tributes highest.
        lead = lead_artist(first)
        queries.extend(dict.fromkeys((f"{lead} {base}", f"{first} {base}")))
        for q in queries:
            url = f"{DEEZER}/search?" + urllib.parse.urlencode({"q": q, "limit": 10})
            data = self._json(url, self._deezer, failures)
            candidates = [
                Candidate(
                    _text(i, "artist", "name"),
                    _text(i, "title"),
                    _text(i, "album", "cover_xl"),
                    _text(i, "album", "title"),
                    _text(i, "artist", "picture_xl"),
                    _id(i, "artist", "id"),
                )
                for i in _items(data, "data")
            ]
            if found := best_match(names, track.title, candidates):
                return found
        return None

    def _itunes_song(self, track: Track, failures: list[str]) -> Found | None:
        names = search_names(track.artists)
        first, base = track.artists[0], base_title(track.title)
        # The first-named artist first, as on Deezer; the full name stays as
        # the fallback for a band whose name holds "&" ("YEARS & YEARS").
        terms = [
            f"{name} {title}"
            for name in dict.fromkeys((lead_artist(first), first))
            for title in dict.fromkeys((track.title, base))
        ]
        for term in terms:
            params = {"term": term, "media": "music", "entity": "song", "limit": 10}
            url = f"{ITUNES}?" + urllib.parse.urlencode(params)
            data = self._json(url, self._itunes, failures)
            candidates = [
                Candidate(
                    _text(i, "artistName"),
                    _text(i, "trackName"),
                    _text(i, "artworkUrl100").replace("100x100bb", "1000x1000bb"),
                    _text(i, "collectionName"),
                )
                for i in _items(data, "results")
            ]
            if found := best_match(names, track.title, candidates):
                return Found(found.image, "album", f"itunes: {found.artist} - {found.title}")
        return None

    def _artist_photo(self, track: Track, failures: list[str]) -> Found | None:
        for name in track.artists:
            if not norm(name):
                continue  # nothing to compare: any name in another script would "match"
            url = f"{DEEZER}/search/artist?" + urllib.parse.urlencode({"q": name, "limit": 25})
            # Deezer keeps duplicate profiles under one name; a small one often
            # has an album cover for its picture and may be listed first. The
            # profile with the most fans is the artist's own.
            best: dict[str, Any] | None = None
            for item in _items(self._json(url, self._deezer, failures), "data"):
                if (
                    norm(_text(item, "name")) == norm(name)
                    and _real_picture(_text(item, "picture_xl"))
                    and (best is None or _fans(item) > _fans(best))
                ):
                    best = item
            picture = _text(best, "picture_xl") if best is not None else ""
            if best is not None and self._not_a_release(_id(best, "id"), picture, failures):
                return Found(picture, "artist", f"deezer artist: {_text(best, 'name')}")
        return None

    def _not_a_release(self, artist_id: str, picture: str, failures: list[str]) -> bool:
        """False for an artist without a photo whose picture is one of their
        release covers (Kotiēr's is the cover of "Ēyēs"), and when the
        releases cannot be had: the failure is recorded, so the search is
        tried again rather than settled."""
        if not artist_id or _image_id(picture) is None:
            return True
        query = urllib.parse.urlencode({"limit": RELEASES_CHECKED})
        data = self._json(f"{DEEZER}/artist/{artist_id}/albums?{query}", self._deezer, failures)
        if not data:
            return False
        return not any(_same_image(picture, _text(i, "cover_xl")) for i in _items(data, "data"))


def _real_picture(picture: str) -> bool:
    # Deezer's placeholder for an artist without a photo has an empty id.
    return bool(picture) and "/artist//" not in picture


def _fans(item: dict[str, Any]) -> int:
    fans = item.get("nb_fan")
    return fans if isinstance(fans, int) and not isinstance(fans, bool) else 0
