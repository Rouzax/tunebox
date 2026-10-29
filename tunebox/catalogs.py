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
# Asked of a catalogue per panel search; coversearch.OFFERS caps the total.
OFFERS_ASKED = 12
# Asked of Deezer's artist search; duplicate profiles share a name.
ARTISTS_ASKED = 25
# Deezer serves artist pictures and release covers from one image store, so
# a picture that is a release cover carries that cover's id.
# Deezer's error code for an id it has no data for.
_NO_DATA = 800
_NO_IMAGE = "d41d8cd98f00b204e9800998ecf8427e"
_IMAGE_ID = re.compile(r"/images/(?:artist|cover)/([0-9a-f]{32})/")


@dataclass(frozen=True)
class Found:
    url: str
    kind: Literal["album", "artist"]
    match: str


@dataclass(frozen=True)
class Offer:
    """A cover the panel offers: the full-size link a fix stores, a small
    one to show, and what the catalogue calls it."""

    link: str
    thumb: str
    artist: str
    album: str
    source: Literal["deezer", "itunes"]


@dataclass(frozen=True)
class ArtistOffer:
    link: str
    thumb: str
    name: str
    fans: int
    artist_id: str


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

    def wait(self, left: Callable[[], float] | None = None) -> bool:
        """left: the seconds a caller with a budget has left. When the call
        would start at or past that, returns False at once and keeps the
        caller's turn free; otherwise waits its turn and returns True."""
        with self._lock:
            delay = max(0.0, self._next - self._clock())
            if left is not None and delay >= left():
                return False
            if delay > 0:
                self._sleep(delay)
            self._next = self._clock() + self._interval
            return True


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
    """find() and artist_photo() run on the one cover worker thread, the
    panel's searches on a request thread; the throttles are thread-safe, so
    both keep to one rate limit per catalogue."""

    def __init__(
        self,
        http_get: HttpGet,
        deezer_interval: float = DEEZER_INTERVAL,
        itunes_interval: float = ITUNES_INTERVAL,
        deezer: str = DEEZER,
        itunes: str = ITUNES,
    ) -> None:
        self._get = http_get
        self._deezer_api = deezer
        self._itunes_api = itunes
        self._deezer = Throttle(deezer_interval)
        self._itunes = Throttle(itunes_interval)

    def find(self, track: Track) -> Found | None:
        failures: list[str] = []
        found = (
            self._deezer_song(track, failures)
            or self._itunes_song(track, failures)
            or self._artist_photo(track, failures, {})
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
        # Each artist's release covers, asked once per search: a refused song
        # picture leads to the name search, which often finds the same artist.
        releases: dict[str, frozenset[str] | None] = {}
        song = self._deezer_match(track, failures)
        # Deezer may list a song under its second artist; the photo is of
        # the first-named one, as the name search would choose.
        lead = lead_artist(track.artists[0])
        if (
            song is not None
            and _real_picture(song.artist_image)
            and artists_match([lead], song.artist)
            and not _same_image(song.artist_image, song.image)
            and self._not_a_release(song.artist_id, song.artist_image, failures, releases)
        ):
            return Found(song.artist_image, "artist", f"deezer artist: {song.artist}")
        found = self._artist_photo(track, failures, releases)
        if found is None and failures:
            raise SearchError("; ".join(failures))
        return found

    def deezer_offers(
        self, query: str, failures: list[str], left: Callable[[], float] | None = None
    ) -> list[Offer]:
        """Deezer's loose search, the form that also finds a song released
        under another title. No match rule: the owner judges."""
        url = f"{self._deezer_api}/search?" + urllib.parse.urlencode(
            {"q": query, "limit": OFFERS_ASKED}
        )
        return [
            Offer(
                _text(i, "album", "cover_xl"),
                _text(i, "album", "cover_medium") or _text(i, "album", "cover_xl"),
                _text(i, "artist", "name"),
                _text(i, "album", "title"),
                "deezer",
            )
            for i in _items(self._json(url, self._deezer, failures, left), "data")
            if _text(i, "album", "cover_xl")
        ]

    def itunes_offers(
        self, query: str, failures: list[str], left: Callable[[], float] | None = None
    ) -> list[Offer]:
        params = {"term": query, "media": "music", "entity": "song", "limit": OFFERS_ASKED}
        url = f"{self._itunes_api}?" + urllib.parse.urlencode(params)
        return [
            Offer(
                _text(i, "artworkUrl100").replace("100x100bb", "1000x1000bb"),
                _text(i, "artworkUrl100").replace("100x100bb", "300x300bb"),
                _text(i, "artistName"),
                _text(i, "collectionName"),
                "itunes",
            )
            for i in _items(self._json(url, self._itunes, failures, left), "results")
            if _text(i, "artworkUrl100")
        ]

    def deezer_album(
        self, album_id: str, failures: list[str], left: Callable[[], float] | None = None
    ) -> Offer | None:
        """album_id is digits only (coversearch.album_id). An id Deezer has
        no album for is an answer (None), not a failure."""
        url = f"{self._deezer_api}/album/{album_id}"
        data = self._json(url, self._deezer, failures, left, no_data_ok=True)
        link = _text(data, "cover_xl")
        if not link:
            return None
        thumb = _text(data, "cover_medium") or link
        return Offer(link, thumb, _text(data, "artist", "name"), _text(data, "title"), "deezer")

    def deezer_artists(
        self, name: str, failures: list[str], left: Callable[[], float] | None = None
    ) -> list[ArtistOffer]:
        """Profiles with a real picture, most fans first."""
        url = f"{self._deezer_api}/search/artist?" + urllib.parse.urlencode(
            {"q": name, "limit": ARTISTS_ASKED}
        )
        found = [
            ArtistOffer(
                _text(i, "picture_xl"),
                _text(i, "picture_medium") or _text(i, "picture_xl"),
                _text(i, "name"),
                _fans(i),
                _id(i, "id"),
            )
            for i in _items(self._json(url, self._deezer, failures, left), "data")
            if _real_picture(_text(i, "picture_xl"))
        ]
        return sorted(found, key=lambda offer: offer.fans, reverse=True)

    def release_covers(
        self, artist_id: str, failures: list[str], left: Callable[[], float] | None = None
    ) -> frozenset[str] | None:
        """The image ids of an artist's release covers; None when they
        cannot be had."""
        query = urllib.parse.urlencode({"limit": RELEASES_CHECKED})
        url = f"{self._deezer_api}/artist/{artist_id}/albums?{query}"
        data = self._json(url, self._deezer, failures, left)
        if not data:
            return None
        return frozenset(
            found
            for i in _items(data, "data")
            if (found := _image_id(_text(i, "cover_xl"))) is not None
        )

    def _json(
        self,
        url: str,
        throttle: Throttle,
        failures: list[str],
        left: Callable[[], float] | None = None,
        no_data_ok: bool = False,
    ) -> dict[str, Any]:
        """left: the caller's budget (Throttle.wait); a call it would not
        start is recorded as a failure, so the catalogue counts as not
        answering. no_data_ok: Deezer's "no data" error is an empty answer."""
        where = url.split("?")[0]
        if not throttle.wait(left):
            failures.append(f"{where}: out of time")
            return {}
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
            if no_data_ok and _error_code(result["error"]) == _NO_DATA:
                return {}
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
            url = f"{self._deezer_api}/search?" + urllib.parse.urlencode({"q": q, "limit": 10})
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
            url = f"{self._itunes_api}?" + urllib.parse.urlencode(params)
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

    def _artist_photo(
        self, track: Track, failures: list[str], releases: dict[str, frozenset[str] | None]
    ) -> Found | None:
        for name in track.artists:
            if not norm(name):
                continue  # nothing to compare: any name in another script would "match"
            url = f"{self._deezer_api}/search/artist?" + urllib.parse.urlencode(
                {"q": name, "limit": ARTISTS_ASKED}
            )
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
            if best is not None and self._not_a_release(
                _id(best, "id"), picture, failures, releases
            ):
                return Found(picture, "artist", f"deezer artist: {_text(best, 'name')}")
        return None

    def _not_a_release(
        self,
        artist_id: str,
        picture: str,
        failures: list[str],
        releases: dict[str, frozenset[str] | None],
    ) -> bool:
        """False for an artist without a photo whose picture is one of their
        release covers (Kotiēr's is the cover of "Ēyēs"), and when the
        releases cannot be had: the failure is recorded, so the search is
        tried again rather than settled."""
        picture_id = _image_id(picture)
        if not artist_id or picture_id is None:
            return True
        if artist_id not in releases:
            releases[artist_id] = self.release_covers(artist_id, failures)
        covers = releases[artist_id]
        return covers is not None and picture_id not in covers


def _error_code(error: object) -> int | None:
    if not isinstance(error, dict):
        return None
    code = cast("dict[str, object]", error).get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def is_release_cover(picture: str, covers: frozenset[str]) -> bool:
    found = _image_id(picture)
    return found is not None and found in covers


def _real_picture(picture: str) -> bool:
    # Deezer's grey outline for an artist without a photo comes with an
    # empty image id, or with the md5 of an empty string as its id.
    return bool(picture) and "/artist//" not in picture and f"/{_NO_IMAGE}/" not in picture


def _fans(item: dict[str, Any]) -> int:
    fans = item.get("nb_fan")
    return fans if isinstance(fans, int) and not isinstance(fans, bool) else 0
