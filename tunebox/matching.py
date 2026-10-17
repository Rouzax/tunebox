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


@dataclass(frozen=True)
class Candidate:
    artist: str
    title: str
    image: str


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


def best_match(
    artists: Sequence[str], title: str, candidates: Sequence[Candidate]
) -> Candidate | None:
    """The first acceptable candidate, preferring the exact version: the remix
    that is playing rather than the original, or the original rather than a remix."""
    exact = norm(title)
    acceptable = [
        c
        for c in candidates
        if c.image and titles_match(title, c.title) and artists_match(artists, c.artist)
    ]
    for candidate in acceptable:
        if norm(candidate.title) == exact:
            return candidate
    return acceptable[0] if acceptable else None
