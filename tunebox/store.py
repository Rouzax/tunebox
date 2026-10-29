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
"""

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
    g.last_at, c.outcome, c.url, c.match, c.decided_at
FROM grouped g
JOIN newest n ON n.key = g.key AND n.n = 1
LEFT JOIN covers c ON c.key = g.key
"""


@dataclass(frozen=True)
class CoverDecision:
    key: str
    outcome: Outcome
    url: str | None
    match: str | None
    decided_at: float


_HeardRow = tuple[
    str, str, str, str, str, str, int, int, Outcome | None, str | None, str | None, float | None
]


@dataclass(frozen=True)
class HeardSong:
    """One song of the panel's list: its text from the newest play, the
    stations it played on, its first and last play (ms, the tracks'
    started_at) and its automatic cover decision."""

    key: str
    photo_key: str
    artist: str
    artists: tuple[str, ...]
    title: str
    channels: tuple[str, ...]
    first_at: int
    last_at: int
    cover: CoverDecision | None


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
                self._path = path
                return db
            except (OSError, sqlite3.Error) as error:
                self._report(error)
                self.degraded = True
        db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        db.executescript(_SCHEMA)
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
        in-memory store, whose list says nothing about what was played."""
        if self._path is None:
            return None
        try:
            with contextlib.closing(
                sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                rows = db.execute(_HEARD, (since_ms,)).fetchall()
        except sqlite3.Error as error:
            self._report(error)
            return None
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
            ) = cast(_HeardRow, row)
            cover = (
                CoverDecision(key, outcome, url, match, decided_at)
                if outcome is not None and decided_at is not None
                else None
            )
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
                    cover,
                )
            )
        return songs
