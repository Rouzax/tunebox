"""Cover fixes: a cover or an artist's photo the owner chose in the panel,
which wins over the automatic decision and is never searched again. The
table lives in the settings database; CoverFixes holds it whole in memory,
so the /now path and the cover worker never read the database."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import astuple, dataclass, replace
from types import MappingProxyType
from typing import Literal, Protocol, cast

from tunebox.logs import event
from tunebox.matching import searchable
from tunebox.settings import Settings, SettingsUnavailable
from tunebox.store import CoverDecision, HeardSong
from tunebox.tracks import Track, norm

log = logging.getLogger(__name__)

FixSource = Literal["deezer", "itunes", "link", "upload", "none"]

_CATALOGUE_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*(?:dzcdn\.net|mzstatic\.com)")
# The failure reason of an uploaded cover whose blob is gone.
FETCH_LOST = "lost"

_Row = tuple[str, str | None, str, FixSource, str, str, float]


@dataclass(frozen=True)
class Fix:
    """key: a song key, or a photo key (photo:<artists>). value: an https
    catalogue image link, blob:<64 hex> for an upload, or None for "no
    photo" (photo keys only). artist and title are the song's text when the
    fix was set, for the panel only: nothing builds a Track from them."""

    key: str
    value: str | None
    label: str
    source: FixSource
    artist: str
    title: str
    set_at: float


class FixReader(Protocol):
    def get(self, key: str) -> Fix | None: ...

    def values(self) -> set[str] | None: ...


class NoFixes:
    """The reader of a cover service built without fixes."""

    def get(self, key: str) -> Fix | None:
        return None

    def values(self) -> set[str] | None:
        return set()


def _read(db: sqlite3.Connection) -> Mapping[str, Fix]:
    rows = cast(
        "list[_Row]",
        db.execute(
            "SELECT key, value, label, source, artist, title, set_at FROM cover_fixes"
        ).fetchall(),
    )
    return MappingProxyType({row[0]: Fix(*row) for row in rows})


class CoverFixes:
    """Readers take the current map without a lock: it is replaced whole,
    never changed in place. Writers are serialised, so the map left last is
    the one the last commit read."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._write = threading.Lock()
        self._map: Mapping[str, Fix] = MappingProxyType({})
        self._known = False
        try:
            with settings.read() as db:
                self._map = _read(db)
            self._known = True
        except SettingsUnavailable as error:
            # Covers fall back to automatic; values() stays unknown so the
            # daily sweep cannot remove the images of fixes it cannot see.
            event(log, logging.ERROR, "cover_fixes_unreadable", error=str(error))

    def get(self, key: str) -> Fix | None:
        return self._map.get(key)

    def all(self) -> list[Fix]:
        return list(self._map.values())

    def values(self) -> set[str] | None:
        """The images the fixes name, or None while the table is unread."""
        if not self._known:
            return None
        return {fix.value for fix in self._map.values() if fix.value is not None}

    def set(self, fix: Fix) -> None:
        with self._write:
            with self._settings.transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO cover_fixes VALUES (?, ?, ?, ?, ?, ?, ?)",
                    astuple(fix),
                )
                committed = _read(db)
            self._map = committed
            self._known = True

    def clear(self, key: str) -> Fix | None:
        """Removes the fix; returns the row deleted (read from the database,
        so it is exact even when the start-up read failed), or None when
        there was none."""
        with self._write:
            with self._settings.transaction() as db:
                row = db.execute(
                    "DELETE FROM cover_fixes WHERE key = ?"
                    " RETURNING key, value, label, source, artist, title, set_at",
                    (key,),
                ).fetchone()
                committed = _read(db)
            self._map = committed
            self._known = True
        if row is None:
            return None
        return Fix(*cast("_Row", row))


@dataclass(frozen=True)
class Shown:
    """What a song or photo key shows: its image link (None for no image),
    the image's kind, and the fix it comes from, if any."""

    url: str | None
    kind: Literal["album", "artist"]
    fix: Fix | None = None


def is_photo_key(key: str) -> bool:
    return key.startswith("photo:")


def is_artist_photo(decision: CoverDecision) -> bool:
    # An error decision keeps the previous URL and match, so the match text,
    # not the outcome, says what kind of image it is.
    return decision.outcome == "artist" or (decision.match or "").startswith("deezer artist:")


def effective(
    fix: Fix | None, automatic: CoverDecision | None, photo_fix: Fix | None
) -> Shown | None:
    """The one rule for what a key shows. fix is the key's own fix,
    automatic its decision from the covers table (ignored when fix is
    set), photo_fix the fix of the song's photo key. A song with an album
    cover of its own keeps that cover; any other song (its automatic image
    the artists' photo, no decision yet, a miss, a failed search without an
    earlier cover) shows the photo fix instead."""
    if fix is not None:
        return Shown(fix.value, "artist" if is_photo_key(fix.key) else "album", fix)
    own_cover = (
        automatic is not None and automatic.url is not None and not is_artist_photo(automatic)
    )
    if photo_fix is not None and not own_cover:
        return Shown(photo_fix.value, "artist", photo_fix)
    if automatic is None:
        return None
    return Shown(automatic.url, "artist" if is_artist_photo(automatic) else "album")


def choices(
    fix: Fix | None, automatic: CoverDecision | None, photo_fix: Fix | None
) -> list[Shown | None]:
    """What a song shows, then what it shows while a fixed image is not in
    the cover store yet: without the song's own fix, then without the photo
    fix either."""
    return [
        effective(fix, automatic, photo_fix),
        effective(None, automatic, photo_fix),
        effective(None, automatic, None),
    ]


def first_stored[T](
    options: Sequence[Shown | None], find: Callable[[str], T | None]
) -> tuple[T, Shown] | None:
    """The first option whose image find() locates in the cover store, with
    what find() returned. A fixed image not stored yet passes to the next
    option; no image ("no photo") and an automatic image not downloaded end
    the search, as the player then shows the station's own images."""
    for shown in options:
        if shown is None or shown.url is None:
            break
        found = find(shown.url)
        if found is not None:
            return found, shown
        if shown.fix is None:
            break
    return None


def allowed_image(link: str) -> bool:
    """An image link a fix may name: https on a catalogue image host, no
    port and no user. Checked when a candidate is signed and again before
    the worker fetches it."""
    if not link.isascii() or not link.isprintable() or " " in link or "\\" in link:
        return False
    try:
        parts = urllib.parse.urlsplit(link)
        port = parts.port
    except ValueError:
        return False
    # Browsers and urllib read some odd hosts differently, so the host must
    # be plain labels; an empty port ("host:") is refused for the same reason.
    return (
        parts.scheme == "https"
        and port is None
        and not parts.netloc.endswith(":")
        and parts.username is None
        and _CATALOGUE_HOST.fullmatch(parts.hostname or "") is not None
    )


View = Literal["problems", "all", "fixed"]
VIEWS: tuple[View, ...] = ("problems", "all", "fixed")
State = Literal["cover", "artist", "nothing", "looking", "fix", "fetching", "failed", "lost"]
PAGE = 100
# A song unresolved for longer than this is a problem; a younger one is most
# likely waiting its turn in the cover queue.
LOOKING_GRACE_MS = 86_400_000


@dataclass(frozen=True)
class Row:
    """One row of the panel's list. photo_key is None on the row of a photo
    fix (its key is the photo key). first_at and last_at are None for a fix
    not heard in the window. link is the image the row shows; image is its
    cover-store file name. A row shows an image only once that file is
    there."""

    key: str
    artist: str
    title: str
    photo_key: str | None
    channels: tuple[str, ...]
    first_at: int | None
    last_at: int | None
    sort_at: int
    state: State
    kind: Literal["album", "artist"] | None
    link: str | None
    fix: Fix | None
    reason: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class Totals:
    """Per song key over the heard songs."""

    songs: int
    cover: int
    artist: int
    nothing: int
    fixed: int


@dataclass(frozen=True)
class Listing:
    rows: list[Row]
    next: tuple[int, str] | None
    totals: Totals


def song_list(
    songs: Sequence[HeardSong],
    fixes: Mapping[str, Fix],
    image: Callable[[str], str | None],
    failure: Callable[[str, str], str | None],
    now_ms: int,
    view: View,
    query: str,
    after: tuple[int, str] | None,
    limit: int = PAGE,
) -> Listing:
    """The panel's page: filter, search, newest first, keyset paging on
    (sort_at, key). image names the cover-store file of a link (a stat, no
    access-time touch); failure the reason the fetch of a fix value (key,
    value) failed."""
    heard = [_song_row(song, fixes, image, failure) for song in songs]
    totals = Totals(
        len(heard),
        sum(1 for r in heard if r.link is not None and r.kind == "album"),
        sum(1 for r in heard if r.link is not None and r.kind == "artist"),
        sum(1 for r in heard if r.link is None and r.state != "looking"),
        sum(1 for r in heard if r.fix is not None),
    )
    if view == "fixed":
        rows = _fixed_rows(heard, songs, fixes, image, failure)
    elif view == "problems":
        rows = [r for r in heard if _problem(r, now_ms)]
    else:
        rows = heard
    wanted, raw = norm(query), query.strip().casefold()
    rows = sorted(
        (r for r in rows if _matches(r, wanted, raw)),
        key=lambda r: (r.sort_at, r.key),
        reverse=True,
    )
    if after is not None:
        rows = [r for r in rows if (r.sort_at, r.key) < after]
    page = rows[:limit]
    more = len(rows) > limit
    return Listing(page, (page[-1].sort_at, page[-1].key) if more else None, totals)


def _song_row(
    song: HeardSong,
    fixes: Mapping[str, Fix],
    image: Callable[[str], str | None],
    failure: Callable[[str, str], str | None],
) -> Row:
    options = choices(fixes.get(song.key), song.cover, fixes.get(song.photo_key))
    shown = options[0]
    base = (
        song.key,
        song.artist,
        song.title,
        song.photo_key,
        song.channels,
        song.first_at,
        song.last_at,
        song.last_at,
    )
    if shown is None:
        track = Track(
            "track", song.artist, song.artists, song.title, song.title, None, song.last_at, song.key
        )
        return Row(*base, "looking" if searchable(track) else "nothing", None, None, None)
    if shown.fix is not None:
        # The state is the fix's; the image is the one lookup serves, which
        # stays the earlier image while the fix is fetched or failed.
        state, reason, _ = _fix_state(shown, image, failure)
        stored = first_stored(options, image)
        if stored is None:
            return Row(*base, state, shown.kind, shown.url, shown.fix, reason, None)
        file, served = stored
        return Row(*base, state, served.kind, served.url, shown.fix, reason, file)
    if song.cover is not None and song.cover.outcome == "error":
        # A failed search or download keeps the previous image, which lookup
        # still serves while its file is there; without it the song is
        # retried soon: still looking, not a miss.
        file = image(shown.url) if shown.url is not None else None
        if file is None:
            return Row(*base, "looking", None, None, None)
        return Row(*base, _kind_state(shown), shown.kind, shown.url, None, None, file)
    if shown.url is None:
        return Row(*base, "nothing", None, None, None)
    # lookup serves an automatic image only while its file is there; until
    # the download lands the song is still looking, not covered.
    file = image(shown.url)
    if file is None:
        return Row(*base, "looking", None, None, None)
    return Row(*base, _kind_state(shown), shown.kind, shown.url, None, None, file)


def _kind_state(shown: Shown) -> State:
    return "artist" if shown.kind == "artist" else "cover"


def _fix_state(
    shown: Shown,
    image: Callable[[str], str | None],
    failure: Callable[[str, str], str | None],
) -> tuple[State, str | None, str | None]:
    """A fix's state, reason and file: shown once its image is in the cover
    store, else fetching, failed (with the reason) or lost (an upload
    without its blob). A "no photo" fix has nothing to fetch."""
    assert shown.fix is not None
    if shown.url is None:
        return "fix", None, None
    file = image(shown.url)
    if file is not None:
        return "fix", None, file
    reason = failure(shown.fix.key, shown.url)
    if reason == FETCH_LOST:
        return "lost", reason, None
    return ("failed", reason, None) if reason else ("fetching", None, None)


def _fixed_rows(
    heard: list[Row],
    songs: Sequence[HeardSong],
    fixes: Mapping[str, Fix],
    image: Callable[[str], str | None],
    failure: Callable[[str, str], str | None],
) -> list[Row]:
    """Every fix, ordered by the later of its last play and when it was
    set. A cover fix no heard song has (not heard in the window, or a song
    key a title-rule change replaced) still has a row, from its stored text."""
    by_key = {row.key: row for row in heard}
    last_by_photo: dict[str, int] = {}
    channels_by_photo: dict[str, set[str]] = {}
    for song in songs:
        last_by_photo[song.photo_key] = max(last_by_photo.get(song.photo_key, 0), song.last_at)
        channels_by_photo.setdefault(song.photo_key, set()).update(song.channels)
    rows: list[Row] = []
    for fix in fixes.values():
        set_ms = round(fix.set_at * 1000)
        photo = is_photo_key(fix.key)
        row = None if photo else by_key.get(fix.key)
        if row is not None:
            rows.append(replace(row, sort_at=max(row.last_at or 0, set_ms)))
            continue
        last = last_by_photo.get(fix.key) if photo else None
        shown = Shown(fix.value, "artist" if photo else "album", fix)
        state, reason, file = _fix_state(shown, image, failure)
        rows.append(
            Row(
                fix.key,
                fix.artist,
                fix.title,
                None,
                tuple(sorted(channels_by_photo.get(fix.key, ()))) if photo else (),
                None,
                last,
                max(last or 0, set_ms),
                state,
                shown.kind,
                fix.value,
                fix,
                reason,
                file,
            )
        )
    return rows


def _problem(row: Row, now_ms: int) -> bool:
    if row.state in ("artist", "nothing"):
        return True
    return (
        row.state == "looking"
        and row.first_at is not None
        and row.first_at < now_ms - LOOKING_GRACE_MS
    )


def _matches(row: Row, wanted: str, raw: str) -> bool:
    """Case-insensitive substring on the normalized text (wanted is the
    query after norm()); a query with nothing left after norm() (another
    script) compares the text as typed (raw, casefolded)."""
    if wanted:
        return wanted in norm(row.artist) or wanted in norm(row.title)
    return not raw or raw in row.artist.casefold() or raw in row.title.casefold()
