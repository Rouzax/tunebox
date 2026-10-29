"""The panel's cover searches: candidates for one song's cover (Deezer and
iTunes by a typed query, or one Deezer album by a pasted link) or for an
artist's photo. Each candidate carries a signed token, and a fix is saved
from a token only, so the panel can never make tunebox fetch an address of
its own choosing."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, cast

from tunebox import crypto
from tunebox.catalogs import ArtistOffer, Catalogs, Offer, is_release_cover
from tunebox.config import encodable
from tunebox.coverfix import FixSource, allowed_image, is_photo_key
from tunebox.http import TIMEOUT_S
from tunebox.logs import event
from tunebox.pacer import Busy, Pacer

log = logging.getLogger(__name__)

# Half the iTunes allowance (catalogs.ITUNES_INTERVAL), which the automatic
# searches share.
PER_MINUTE = 10
# No catalogue call starts after this; the answer carries what it has.
BUDGET_S = 8.0
# The wait a refused search is told (Pacer.slot): a call begun just before
# the budget ran out usually ends within one socket timeout, but the timeout
# applies to each socket operation, so this is a hint, not a hard bound.
SLOT_BOUND_S = BUDGET_S + TIMEOUT_S
OFFERS = 12
# Release checks cost a call each; only the profiles with the most fans,
# the likely artist, get one.
PHOTOS = 10
PHOTOS_CHECKED = 5
TOKEN_S = 3600
# A token issued this far in the future still counts: the wall clock can
# step back a little between issue and redeem.
CLOCK_SKEW_S = 5
PURPOSE = "cover_candidate"
_NAMES = {"deezer": "Deezer", "itunes": "iTunes"}
# The whole input: https, Deezer's own host, an optional language, the
# album id in ASCII digits, and a query (sharing adds one) that is ignored.
_ALBUM = re.compile(
    r"https://(?:www\.)?deezer\.com(?:/[a-z]{2})?/album/([0-9]{1,12})(?:\?[^\s#]*)?"
)

SearchKind = Literal["cover", "link", "photo"]


class BadLink(ValueError):
    """Not a Deezer album link this search follows."""


class NothingToSearch(ValueError):
    """The search text (and, for a photo, the artist) is blank."""


def album_id(text: str) -> str | None:
    found = _ALBUM.fullmatch(text.strip())
    return found.group(1) if found else None


@dataclass(frozen=True)
class Pick:
    """What a token lets the panel save: the fix, as it was offered."""

    key: str
    link: str | None
    label: str
    source: FixSource
    artist: str
    title: str
    issued_at: float


@dataclass(frozen=True)
class Choice:
    """One candidate as the panel shows it. release: whether an artist
    picture is one of that artist's release covers; None when not checked."""

    token: str
    thumb: str
    artist: str
    album: str
    source: str
    fans: int | None = None
    release: bool | None = None


@dataclass(frozen=True)
class Answer:
    """unanswered names each catalogue that failed or was cut short by the
    budget; no_photo is the token of "No photo" on a photo search."""

    choices: list[Choice]
    unanswered: list[str]
    no_photo: str | None = None


class CoverSearch:
    def __init__(
        self,
        catalogs: Catalogs,
        secret: Callable[[], bytes],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._catalogs = catalogs
        self._secret = secret
        self._clock = clock
        self._monotonic = monotonic
        self._pacer = Pacer(PER_MINUTE, "search_running", "too_many_searches", monotonic)

    def search(self, kind: SearchKind, key: str, artist: str, title: str, text: str) -> Answer:
        """Raises BadLink for a link no search follows, NothingToSearch for
        blank text and ValueError for a key that does not fit the kind, all
        before the pace is spent; Busy when another search runs or the
        minute's are used up."""
        album = album_id(text) if kind == "link" else None
        if kind == "link" and album is None:
            raise BadLink(text)
        query = text.strip() or (artist.strip() if kind == "photo" else "")
        if not query:
            raise NothingToSearch(kind)
        if (kind == "photo") != is_photo_key(key):
            raise ValueError("the key does not fit the search")
        with self._logged(kind) as logged, self._pacer.slot(SLOT_BOUND_S):
            deadline = self._monotonic() + BUDGET_S

            def left() -> float:
                return deadline - self._monotonic()

            if kind == "photo":
                answer = self._photos(key, artist, query, left)
            elif album is not None:
                answer = self._album(key, artist, title, album, left)
            else:
                answer = self._covers(key, artist, title, query, left)
            asked = 1 if kind != "cover" else 2
            logged["results"] = len(answer.choices)
            logged["outcome"] = (
                "ok"
                if not answer.unanswered
                else "error"
                if len(answer.unanswered) == asked
                else "partial"
            )
            return answer

    def redeem(self, token: str) -> Pick | None:
        """The pick a token was issued for; None when it is forged, older
        than TOKEN_S, or names an image the rule no longer allows."""
        payload = crypto.unsign(self._secret(), token, PURPOSE)
        if payload is None:
            return None
        try:
            data: object = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        fields = cast("dict[str, object]", data)
        key, link, label, source, artist, title, at = (
            fields.get(name) for name in ("k", "l", "b", "s", "a", "t", "i")
        )
        if not (
            isinstance(key, str)
            and isinstance(label, str)
            and isinstance(artist, str)
            and isinstance(title, str)
            and isinstance(at, int | float)
            and source in ("deezer", "itunes", "link", "none")
            and (link is None or isinstance(link, str))
            and (source == "none") == (link is None)
        ):
            return None
        if not -CLOCK_SKEW_S <= self._clock() - at <= TOKEN_S:
            return None
        # json.loads restores lone surrogates from \u escapes; SQLite refuses them.
        if not all(encodable(text) for text in (key, label, artist, title, link or "")):
            return None
        if link is None and not is_photo_key(key):
            return None
        if link is not None and not allowed_image(link):
            return None
        return Pick(key, link, label, cast("FixSource", source), artist, title, float(at))

    def _token(self, pick: Pick) -> str:
        payload = json.dumps(
            {
                "k": pick.key,
                "l": pick.link,
                "b": pick.label,
                "s": pick.source,
                "a": pick.artist,
                "t": pick.title,
                "i": pick.issued_at,
            },
            separators=(",", ":"),
        )
        return crypto.sign(self._secret(), payload, PURPOSE)

    def _offered(
        self, key: str, artist: str, title: str, offer: Offer, source: FixSource
    ) -> Choice:
        label = f"{_NAMES[offer.source]}: {offer.artist} - {offer.album}"
        pick = Pick(key, offer.link, label, source, artist, title, self._clock())
        thumb = offer.thumb if allowed_image(offer.thumb) else offer.link
        return Choice(self._token(pick), thumb, offer.artist, offer.album, offer.source)

    def _covers(
        self, key: str, artist: str, title: str, query: str, left: Callable[[], float]
    ) -> Answer:
        found: dict[str, list[Offer]] = {}
        unanswered: list[str] = []
        for name, ask in (
            ("deezer", self._catalogs.deezer_offers),
            ("itunes", self._catalogs.itunes_offers),
        ):
            failures: list[str] = []
            offers = ask(query, failures, left)
            if failures:
                unanswered.append(name)
            found[name] = [offer for offer in offers if allowed_image(offer.link)]
        merged = _merge(found.get("deezer", []), found.get("itunes", []))
        choices = [
            self._offered(key, artist, title, offer, cast("FixSource", offer.source))
            for offer in merged
        ]
        return Answer(choices, unanswered)

    def _album(
        self, key: str, artist: str, title: str, album: str, left: Callable[[], float]
    ) -> Answer:
        failures: list[str] = []
        offer = self._catalogs.deezer_album(album, failures, left)
        if offer is None or not allowed_image(offer.link):
            return Answer([], ["deezer"] if failures else [])
        return Answer([self._offered(key, artist, title, offer, "link")], [])

    def _photos(self, key: str, artist: str, name: str, left: Callable[[], float]) -> Answer:
        failures: list[str] = []
        offers = [
            offer
            for offer in self._catalogs.deezer_artists(name, failures, left)
            if allowed_image(offer.link)
        ][:PHOTOS]
        choices = [
            self._photo(key, artist, offer, index < PHOTOS_CHECKED, left)
            for index, offer in enumerate(offers)
        ]
        none = Pick(key, None, "No photo", "none", artist, "", self._clock())
        return Answer(choices, ["deezer"] if failures else [], self._token(none))

    def _photo(
        self, key: str, artist: str, offer: ArtistOffer, check: bool, left: Callable[[], float]
    ) -> Choice:
        release: bool | None = None
        if check and offer.artist_id:
            covers = self._catalogs.release_covers(offer.artist_id, [], left)
            if covers is not None:
                release = is_release_cover(offer.link, covers)
        pick = Pick(
            key, offer.link, f"Deezer artist: {offer.name}", "deezer", artist, "", self._clock()
        )
        thumb = offer.thumb if allowed_image(offer.thumb) else offer.link
        return Choice(self._token(pick), thumb, offer.name, "", "deezer", offer.fans, release)

    @contextmanager
    def _logged(self, kind: SearchKind) -> Generator[dict[str, object]]:
        started = time.monotonic()
        logged: dict[str, object] = {"kind": kind, "outcome": "error", "results": 0}
        try:
            yield logged
        except Busy:
            logged["outcome"] = "busy"
            raise
        finally:
            event(
                log,
                logging.INFO if logged["outcome"] == "error" else logging.DEBUG,
                "cover_search",
                duration_ms=round((time.monotonic() - started) * 1000),
                **logged,
            )


def _merge(deezer: list[Offer], itunes: list[Offer]) -> list[Offer]:
    """Up to OFFERS, half from each catalogue, the other filling in where
    one has fewer; Deezer's first, each image once."""
    half = OFFERS // 2
    ordered = deezer[:half] + itunes[:half] + deezer[half:] + itunes[half:]
    seen: set[str] = set()
    merged: list[Offer] = []
    for offer in ordered:
        if offer.link not in seen:
            seen.add(offer.link)
            merged.append(offer)
    return merged[:OFFERS]
