"""SQLite persistence for cover decisions, recent lists and the songs the panel lists."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from tunebox.logs import event
from tunebox.tracks import Kind, Track

log = logging.getLogger(__name__)

Outcome = Literal["cover", "artist", "miss", "error"]
HealthChange = Literal["failing", "recovered", "write_failed"] | None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS covers (
    key TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    url TEXT,
    match TEXT,
    decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recent (
    channel TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    artist TEXT NOT NULL,
    artists TEXT NOT NULL,
    title TEXT NOT NULL,
    full_title TEXT NOT NULL,
    played_by TEXT,
    PRIMARY KEY (channel, started_at, key)
);
-- A cache of the songs the panel lists, outside the numbered migrations:
-- a later change of its shape drops and recreates it.
CREATE TABLE IF NOT EXISTS heard (
    key TEXT NOT NULL,
    channel TEXT NOT NULL,
    photo_key TEXT NOT NULL,
    artist TEXT NOT NULL,
    artists TEXT NOT NULL,
    title TEXT NOT NULL,
    first_at INTEGER NOT NULL,
    last_at INTEGER NOT NULL,
    PRIMARY KEY (key, channel)
);
CREATE INDEX IF NOT EXISTS heard_last ON heard(last_at);
-- heard_track by photo key, newest first: an undo of a photo fix.
CREATE INDEX IF NOT EXISTS heard_photo ON heard(photo_key, last_at);
-- The health check's latest result per station item, outside the numbered
-- migrations; the version is in the name, and _open drops a health table
-- of any other version, so a new shape starts empty.
CREATE TABLE IF NOT EXISTS health_v1 (
    channel TEXT NOT NULL,
    item TEXT NOT NULL,
    target TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT,
    codec TEXT,
    kbps INTEGER,
    failing_since INTEGER,
    PRIMARY KEY (channel, item)
);
CREATE TABLE IF NOT EXISTS health_run_v1 (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_started_at INTEGER,
    last_finished_at INTEGER,
    duration_ms INTEGER,
    items INTEGER,
    outcome TEXT
);
"""

_HEALTH_TABLES = ("health_v1", "health_run_v1")
_HEALTH_COLUMNS = "channel, item, target, checked_at, ok, reason, codec, kbps, failing_since"


def _drop_old_health(db: sqlite3.Connection) -> None:
    names = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'health%'"
        )
    ]
    for name in names:
        if name not in _HEALTH_TABLES:
            db.execute(f'DROP TABLE "{name}"')


# The panel lists the songs of this many days; older heard rows go daily.
HEARD_KEEP_MS = 30 * 86_400_000

_HEARD = """
WITH kept AS (SELECT * FROM heard WHERE last_at >= ?),
grouped AS (
    SELECT key, MIN(first_at) AS first_at, MAX(last_at) AS last_at,
        json_group_array(channel) AS channels
    FROM kept GROUP BY key
),
newest AS (
    SELECT key, photo_key, artist, artists, title,
        ROW_NUMBER() OVER (PARTITION BY key ORDER BY last_at DESC, channel) AS n
    FROM kept
)
SELECT g.key, n.photo_key, n.artist, n.artists, n.title, g.channels, g.first_at,
    g.last_at, c.outcome, c.url, c.match, c.decided_at,
    p.outcome, p.url, p.match, p.decided_at
FROM grouped g
JOIN newest n ON n.key = g.key AND n.n = 1
LEFT JOIN covers c ON c.key = g.key
LEFT JOIN covers p ON p.key = n.photo_key
"""


class HeardUnreadable(Exception):
    """The song list could not be read from the database file just now."""


@dataclass(frozen=True)
class CoverDecision:
    key: str
    outcome: Outcome
    url: str | None
    match: str | None
    decided_at: float


_HeardRow = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    int,
    Outcome | None,
    str | None,
    str | None,
    float | None,
    Outcome | None,
    str | None,
    str | None,
    float | None,
]


@dataclass(frozen=True)
class HeardSong:
    """One song of the panel's list: its text from the newest play, the
    stations it played on, its first and last play (ms, the tracks'
    started_at), its automatic cover decision and the automatic decision
    of its photo key."""

    key: str
    photo_key: str
    artist: str
    artists: tuple[str, ...]
    title: str
    channels: tuple[str, ...]
    first_at: int
    last_at: int
    cover: CoverDecision | None
    photo: CoverDecision | None


@dataclass(frozen=True)
class HealthRow:
    channel: str
    item: str
    target: str
    checked_at: int
    ok: bool
    reason: str | None
    codec: str | None
    kbps: int | None
    failing_since: int | None


@dataclass(frozen=True)
class HealthRun:
    last_started_at: int | None = None
    last_finished_at: int | None = None
    duration_ms: int | None = None
    items: int | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class Written:
    change: HealthChange
    # failing_since before this write: how long a recovered item failed.
    since: int | None


def transition(before: HealthRow | None, row: HealthRow) -> tuple[HealthChange, int | None]:
    """A failure of the same target keeps its first failing_since; a new
    target starts over, so a fixed address is never reported as the old
    one recovering."""
    same = before is not None and before.target == row.target
    was_failing = same and before is not None and not before.ok
    if row.ok:
        return ("recovered" if was_failing else None), None
    if was_failing and before is not None:
        return None, before.failing_since
    return "failing", row.failing_since


def _health_row(values: Sequence[object]) -> HealthRow:
    channel, item, target, checked_at, ok, reason, codec, kbps, failing_since = values
    return HealthRow(
        cast("str", channel),
        cast("str", item),
        cast("str", target),
        cast("int", checked_at),
        bool(ok),
        cast("str | None", reason),
        cast("str | None", codec),
        cast("int | None", kbps),
        cast("int | None", failing_since),
    )


class Store:
    """Safe to share between threads. If the database cannot be opened or
    written, that is logged once and the server carries on in memory."""

    def __init__(self, path: Path | None) -> None:
        self._lock = threading.Lock()
        self._reported = False
        # True when a database was asked for but the server runs on the in-memory
        # fallback, whose contents say nothing about what is on disk.
        self.degraded = False
        # Set by the first write that fails: decisions may then be missing from
        # the database, so its list of links no longer says which files are unused.
        self._write_failed = False
        self._path: Path | None = None
        self._db = self._open(path)

    def _open(self, path: Path | None) -> sqlite3.Connection:
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SCHEMA)
                _drop_old_health(db)
                self._path = path
                return db
            except (OSError, sqlite3.Error) as error:
                self._report(error)
                self.degraded = True
        db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        db.executescript(_SCHEMA)
        _drop_old_health(db)
        return db

    def _write_error(self, error: Exception) -> None:
        self._write_failed = True
        self._report(error)

    def _report(self, error: Exception) -> None:
        if not self._reported:
            self._reported = True
            event(log, logging.ERROR, "store_unavailable", error=str(error))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get_cover(self, key: str) -> CoverDecision | None:
        with self._lock:
            try:
                row = self._db.execute(
                    "SELECT key, outcome, url, match, decided_at FROM covers WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error as error:
                self._report(error)
                return None
        if row is None:
            return None
        found_key, outcome, url, match, decided_at = cast(
            "tuple[str, Outcome, str | None, str | None, float]", row
        )
        return CoverDecision(found_key, outcome, url, match, decided_at)

    def put_cover(self, decision: CoverDecision) -> None:
        row = (decision.key, decision.outcome, decision.url, decision.match, decision.decided_at)
        with self._lock:
            try:
                self._db.execute("INSERT OR REPLACE INTO covers VALUES (?, ?, ?, ?, ?)", row)
            except sqlite3.Error as error:
                self._write_error(error)

    def prune_covers(self, older_than: float) -> list[str]:
        """Deletes decisions last made before older_than; returns their keys."""
        with self._lock:
            try:
                rows = self._db.execute(
                    "DELETE FROM covers WHERE decided_at < ? RETURNING key", (older_than,)
                ).fetchall()
            except sqlite3.Error as error:
                self._write_error(error)
                return []
        return [cast("str", row[0]) for row in rows]

    def cover_links(self) -> set[str] | None:
        """Every link a decision names, or None when that cannot be told
        (unreadable database, the in-memory fallback, or a write that failed
        in this process), so a caller never mistakes "unknown" for "none"."""
        if self.degraded or self._write_failed:
            return None
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT DISTINCT url FROM covers WHERE url IS NOT NULL"
                ).fetchall()
            except sqlite3.Error as error:
                self._report(error)
                return None
        return {cast("str", row[0]) for row in rows}

    def load_recent(self, channel: str) -> list[Track]:
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT kind, artist, artists, title, full_title, played_by, started_at, key"
                    " FROM recent WHERE channel = ? ORDER BY started_at DESC",
                    (channel,),
                ).fetchall()
            except sqlite3.Error as error:
                self._report(error)
                return []
        tracks: list[Track] = []
        for row in rows:
            kind, artist, artists, title, full_title, played_by, started_at, key = cast(
                "tuple[Kind, str, str, str, str, str | None, int, str]", row
            )
            names = tuple(cast("list[str]", json.loads(artists)))
            tracks.append(Track(kind, artist, names, title, full_title, played_by, started_at, key))
        return tracks

    def save_recent(self, channel: str, tracks: Sequence[Track]) -> None:
        rows = [
            (
                channel,
                t.started_at,
                t.key,
                t.kind,
                t.artist,
                json.dumps(list(t.artists)),
                t.title,
                t.full_title,
                t.played_by,
            )
            for t in tracks
        ]
        with self._lock:
            try:
                self._db.execute("BEGIN")
                self._db.execute("DELETE FROM recent WHERE channel = ?", (channel,))
                self._db.executemany(
                    "INSERT OR REPLACE INTO recent VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
                self._db.execute("COMMIT")
            except sqlite3.Error as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._write_error(error)

    def record_heard(self, channel: str, tracks: Sequence[Track]) -> None:
        """One upsert per song in one transaction: the first play stays, the
        last moves on. A failure is reported once and does not mark the
        decisions unknown: it says nothing about them."""
        rows = [
            (
                t.key,
                channel,
                t.photo_key,
                t.artist,
                json.dumps(list(t.artists)),
                t.title,
                t.started_at,
                t.started_at,
            )
            for t in tracks
        ]
        with self._lock:
            try:
                self._db.execute("BEGIN")
                self._db.executemany(
                    "INSERT INTO heard VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (key, channel) DO UPDATE SET"
                    " photo_key = excluded.photo_key, artist = excluded.artist,"
                    " artists = excluded.artists, title = excluded.title,"
                    " first_at = MIN(first_at, excluded.first_at),"
                    " last_at = MAX(last_at, excluded.last_at)",
                    rows,
                )
                self._db.execute("COMMIT")
            except sqlite3.Error as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._report(error)

    def prune_heard(self, older_than_ms: int) -> int:
        with self._lock:
            try:
                cursor = self._db.execute("DELETE FROM heard WHERE last_at < ?", (older_than_ms,))
            except sqlite3.Error as error:
                self._report(error)
                return 0
        return cursor.rowcount

    def health_write(self, row: HealthRow) -> Written:
        """Store the latest result of one item. A failure is reported once and
        does not mark the cover decisions unknown: a health table says nothing
        about them."""
        with self._lock:
            try:
                self._db.execute("BEGIN")
                found = self._db.execute(
                    f"SELECT {_HEALTH_COLUMNS} FROM health_v1 WHERE channel = ? AND item = ?",
                    (row.channel, row.item),
                ).fetchone()
                before = _health_row(found) if found else None
                change, since = transition(before, row)
                self._db.execute(
                    "INSERT OR REPLACE INTO health_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.channel,
                        row.item,
                        row.target,
                        row.checked_at,
                        int(row.ok),
                        row.reason,
                        row.codec,
                        row.kbps,
                        since,
                    ),
                )
                self._db.execute("COMMIT")
            except sqlite3.Error as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._report(error)
                return Written("write_failed", None)
        return Written(change, before.failing_since if before is not None else None)

    def health_rows(self) -> list[HealthRow] | None:
        """Every stored item, or None when the table cannot be read."""
        with self._lock:
            try:
                found = self._db.execute(
                    f"SELECT {_HEALTH_COLUMNS} FROM health_v1 ORDER BY channel, item"
                ).fetchall()
            except sqlite3.Error as error:
                self._report(error)
                return None
        return [_health_row(values) for values in found]

    def health_prune(self, keep: set[tuple[str, str]]) -> None:
        """Drop the items that are no longer checked."""
        with self._lock:
            try:
                self._db.execute("BEGIN")
                stored = self._db.execute("SELECT channel, item FROM health_v1").fetchall()
                self._db.executemany(
                    "DELETE FROM health_v1 WHERE channel = ? AND item = ?",
                    [(c, i) for c, i in stored if (c, i) not in keep],
                )
                self._db.execute("COMMIT")
            except sqlite3.Error as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._report(error)

    def health_run_started(self, now_ms: int) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO health_run_v1 (id, last_started_at) VALUES (1, ?)"
                    " ON CONFLICT (id) DO UPDATE SET"
                    " last_started_at = excluded.last_started_at",
                    (now_ms,),
                )
            except sqlite3.Error as error:
                self._report(error)

    def health_run_finished(self, now_ms: int, duration_ms: int, items: int, outcome: str) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO health_run_v1"
                    " (id, last_finished_at, duration_ms, items, outcome)"
                    " VALUES (1, ?, ?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET"
                    " last_finished_at = excluded.last_finished_at,"
                    " duration_ms = excluded.duration_ms, items = excluded.items,"
                    " outcome = excluded.outcome",
                    (now_ms, duration_ms, items, outcome),
                )
            except sqlite3.Error as error:
                self._report(error)

    def health_run(self) -> HealthRun:
        with self._lock:
            try:
                found = self._db.execute(
                    "SELECT last_started_at, last_finished_at, duration_ms, items, outcome"
                    " FROM health_run_v1 WHERE id = 1"
                ).fetchone()
            except sqlite3.Error as error:
                self._report(error)
                return HealthRun()
        return HealthRun(*found) if found else HealthRun()

    def delete_cover(self, key: str) -> None:
        """A failure leaves the old answer, which still names its link, so
        the decisions stay known."""
        with self._lock:
            try:
                self._db.execute("DELETE FROM covers WHERE key = ?", (key,))
            except sqlite3.Error as error:
                self._report(error)

    def heard_track(self, key: str) -> Track | None:
        """The newest heard song for a song key, or for a photo key the
        newest song by those artists, as a Track under that key: what a
        search for the key needs."""
        column = "photo_key" if key.startswith("photo:") else "key"
        with self._lock:
            try:
                row = self._db.execute(
                    f"SELECT artist, artists, title, last_at FROM heard WHERE {column} = ?"
                    " ORDER BY last_at DESC LIMIT 1",
                    (key,),
                ).fetchone()
            except sqlite3.Error as error:
                self._report(error)
                return None
        if row is None:
            return None
        artist, artists, title, last_at = cast("tuple[str, str, str, int]", row)
        names = tuple(cast("list[str]", json.loads(artists)))
        return Track("track", artist, names, title, title, None, last_at, key)

    def heard_songs(self, since_ms: int) -> list[HeardSong] | None:
        """The panel's list, on a read-only connection of its own so it
        never waits for (or holds up) the store's lock. None on the
        in-memory store, whose list says nothing about what was played.
        Raises HeardUnreadable when the file cannot be read now; that is
        logged as cover_list_unreadable on every failure and leaves the
        one-shot store_unavailable to the store's own connection."""
        if self._path is None:
            return None
        try:
            with contextlib.closing(
                sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                rows = db.execute(_HEARD, (since_ms,)).fetchall()
        except sqlite3.Error as error:
            event(log, logging.WARNING, "cover_list_unreadable", error=str(error))
            raise HeardUnreadable from error
        songs: list[HeardSong] = []
        for row in rows:
            (
                key,
                photo_key,
                artist,
                artists,
                title,
                channels,
                first_at,
                last_at,
                outcome,
                url,
                match,
                decided_at,
                photo_outcome,
                photo_url,
                photo_match,
                photo_decided_at,
            ) = cast(_HeardRow, row)
            songs.append(
                HeardSong(
                    key,
                    photo_key,
                    artist,
                    tuple(cast("list[str]", json.loads(artists))),
                    title,
                    tuple(sorted(cast("list[str]", json.loads(channels)))),
                    first_at,
                    last_at,
                    _decision(key, outcome, url, match, decided_at),
                    _decision(photo_key, photo_outcome, photo_url, photo_match, photo_decided_at),
                )
            )
        return songs


def _decision(
    key: str,
    outcome: Outcome | None,
    url: str | None,
    match: str | None,
    decided_at: float | None,
) -> CoverDecision | None:
    """A LEFT JOIN's decision, or None where the key has no row."""
    if outcome is None or decided_at is None:
        return None
    return CoverDecision(key, outcome, url, match, decided_at)
