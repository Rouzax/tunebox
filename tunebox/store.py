"""SQLite persistence for cover decisions and recent lists."""

from __future__ import annotations

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
"""


@dataclass(frozen=True)
class CoverDecision:
    key: str
    outcome: Outcome
    url: str | None
    match: str | None
    decided_at: float


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
        self._db = self._open(path)

    def _open(self, path: Path | None) -> sqlite3.Connection:
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SCHEMA)
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
