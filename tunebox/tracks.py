"""Turns raw now-playing entries into tracks, using a channel's cleanup rules."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Kind = Literal["track", "station_id"]

# The regex escape keeps the right single quotation mark out of the source.
_APOSTROPHES = re.compile(r"['\u2019`]")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class RawEntry:
    artist: str
    title: str
    started_at_ms: int | None


@dataclass(frozen=True)
class Rules:
    station_id: re.Pattern[str] | None = None
    # Group 1 is the "who played it" part that is kept.
    played_by: re.Pattern[str] | None = None
    artist_separator: str = ","
    # The first group that took part in the match is the badge (a chart
    # position, a show label); the whole match leaves the title. Alternatives
    # with their own group let one pattern take a prefix or a suffix.
    badge: re.Pattern[str] | None = None


@dataclass(frozen=True)
class Track:
    kind: Kind
    artist: str
    artists: tuple[str, ...]
    title: str
    full_title: str
    played_by: str | None
    started_at: int
    key: str
    badge: str | None = None

    @property
    def ident(self) -> tuple[int, str]:
        return (self.started_at, self.key)

    @property
    def photo_key(self) -> str:
        """The key of the artists' photo, shared by all their songs. No song
        key can equal it: a song key's artist part is either letters and
        digits only (no colon) or holds no ASCII letter at all."""
        return f"photo:{_key_part(' '.join(self.artists))}"


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    # Apostrophes vanish rather than split a word: "Somebody's" must equal "Somebodys".
    text = _APOSTROPHES.sub("", text)
    return _SPACES.sub(" ", _NON_ALNUM.sub(" ", text)).strip()


# Rules run on every title; a feed could send megabytes in one.
MAX_TITLE = 300


def _tidy(text: str) -> str:
    return _SPACES.sub(" ", _repair(text)).strip()


def _repair(text: str) -> str:
    """Undoes UTF-8 that was read as Windows-1252 on the way to tunebox (Radio
    538's names arrived as "TI\u00c3\u2039STO" for TI\u00cbSTO). Only text whose
    bytes then decode as UTF-8 changes; correct accented text does not."""
    if text.isascii():
        return text
    raw = bytearray()
    for char in text:
        try:
            raw += char.encode("cp1252")
        except UnicodeEncodeError:
            # The five bytes Windows-1252 leaves undefined come through
            # as the control characters of the same number.
            if ord(char) > 0xFF:
                return text
            raw.append(ord(char))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return text


def _key_part(text: str) -> str:
    # norm() keeps only ASCII letters and digits; a name in another script
    # would otherwise reduce to nothing and share one key with all such names.
    return norm(text) or _tidy(text).casefold()


def clean(raw: RawEntry, rules: Rules, seen_at_ms: int) -> Track:
    separator = rules.artist_separator
    artist = _tidy(raw.artist)
    while artist.endswith(separator):
        artist = artist[: -len(separator)].rstrip()
    # Only the separator splits: "&", "." and "/" occur inside single names
    # ("LILLY WOOD & THE PRICK", "X CLUB.", "AU/RA").
    artists = tuple(part for part in (_tidy(p) for p in artist.split(separator)) if part)

    full_title = _tidy(raw.title)[:MAX_TITLE]
    title, played_by = full_title, None
    if rules.played_by is not None and (match := rules.played_by.search(full_title)):
        played_by = _tidy(match.group(1)) or None
        title = _tidy(full_title[: match.start()])
    badge = None
    if rules.badge is not None and (match := rules.badge.search(title)):
        rest = _tidy(title[: match.start()] + " " + title[match.end() :])
        # A title that is nothing but the badge stays as it is.
        if rest:
            badge = next((_tidy(g) for g in match.groups() if g is not None), "") or None
            title = rest

    is_station_id = (
        not artist and rules.station_id is not None and bool(rules.station_id.match(full_title))
    )
    started_at = raw.started_at_ms if raw.started_at_ms is not None else seen_at_ms
    return Track(
        kind="station_id" if is_station_id else "track",
        artist=artist,
        artists=artists,
        title=title,
        full_title=full_title,
        played_by=played_by,
        started_at=started_at,
        key=f"{_key_part(' '.join(artists))}|{_key_part(title)}",
        badge=badge,
    )


def reclean(track: Track, rules: Rules) -> Track:
    """The track as the given rules read it. A stored or carried track was
    cleaned by the rules of its day; its artist and full title are the
    tidied feed text, which clean() reads back unchanged."""
    return clean(
        RawEntry(track.artist, track.full_title, track.started_at), rules, track.started_at
    )
