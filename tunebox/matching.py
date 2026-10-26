"""Decides whether a catalogue entry is the track that is playing."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tunebox.tracks import norm

_BRACKETS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
# A title that is a prefix of the other must cover at least this share of it.
# This keeps "It's Not Right (But It's Ok)" against "It's Not Right But It's
# Okay" (exactly half) and rejects "Running" against "Running Up That Hill".
MIN_PREFIX_SHARE = 0.5
_AMPERSAND = re.compile(r"\s+&\s+")
_VERSION_LABELS = re.compile(r"[\(\[]([^)\]]*)[\)\]]")
# Signs that a catalogue entry is a DJ mix, a compilation or a radio show
# rather than the song's own release: its cover then shows the mix, not the
# song. Read from norm() text of the album and the version labels.
_COMPILED = re.compile(
    r"\bmixed\b|\bvol(?:ume)?\b\.?\s*\d|\bradio\s+\d+|\bepisode\b|\bpresents\b"
    r"|\bcompilation\b|\bclassics\b|\bhits\b|\bbest of\b"
    r"|\b(?:festival|miami|ibiza|summer|winter)\b.*\b(?:19|20)\d\d\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    artist: str
    title: str
    image: str
    album: str = ""


def base_title(title: str) -> str:
    return _BRACKETS.sub("", title).strip()


def titles_match(wanted: str, found: str) -> bool:
    want, got = norm(base_title(wanted)), norm(base_title(found))
    if not want or not got:
        return False
    if want == got:
        return True
    shorter, longer = sorted((want, got), key=len)
    return longer.startswith(shorter + " ") and len(shorter) / len(longer) >= MIN_PREFIX_SHARE


def _contains_words(outer: str, inner: str) -> bool:
    return f" {inner} " in f" {outer} "


def artists_match(artists: Sequence[str], found: str) -> bool:
    got = norm(found)
    if not got:
        return False
    for artist in artists:
        want = norm(artist)
        if want and (_contains_words(got, want) or _contains_words(want, got)):
            return True
    return False


def search_names(artists: Sequence[str]) -> tuple[str, ...]:
    """The artists as given, then the parts of names joined by " & ". Some
    stations send a duet as one name ("AVICII & ALOE BLACC"), which the
    catalogues do not know as an artist; "&" also occurs inside single
    names ("Lilly Wood & The Prick"), so the parts are added, never put in
    place of the name."""
    names = list(artists)
    for artist in artists:
        names.extend(part for part in _AMPERSAND.split(artist) if part.strip())
    return tuple(dict.fromkeys(name.strip() for name in names if name.strip()))


def lead_artist(name: str) -> str:
    """The first-named artist of a name joined by " & ", else the name."""
    return _AMPERSAND.split(name, maxsplit=1)[0].strip() or name


def compiled(candidate: Candidate) -> bool:
    """Whether the album or a version label ("(Mixed by ...)") looks like a
    mix, compilation or show. The song's own name is left out of the album:
    "Mixed Emotions" on its single "Mixed Emotions" is no mix."""
    song = norm(base_title(candidate.title))
    album = f" {norm(candidate.album)} "
    if song:
        album = album.replace(f" {song} ", " ", 1)
    labels = " ".join(norm(label) for label in _VERSION_LABELS.findall(candidate.title))
    return bool(_COMPILED.search(f"{album} {labels}"))


def best_match(
    artists: Sequence[str], title: str, candidates: Sequence[Candidate]
) -> Candidate | None:
    """The first acceptable candidate, preferring the song's own release over
    a DJ mix, compilation or radio show (whose cover shows that, not the
    song), and then the exact version: the remix that is playing rather than
    the original, or the original rather than a remix. A song released only
    on a compilation still gets that cover."""
    exact = norm(title)
    acceptable = [
        c
        for c in candidates
        if c.image and titles_match(title, c.title) and artists_match(artists, c.artist)
    ]
    # Stable: within each rank the catalogue's own order decides.
    ranked = sorted(acceptable, key=lambda c: (compiled(c), norm(c.title) != exact))
    return ranked[0] if ranked else None
