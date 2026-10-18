"""The configuration the admin panel edits: stations, groups and settings,
with a version that every change bumps and a history of every change.

Unlike the cover store this never falls back to memory: edits that vanish
on the next restart are worse than a tunebox that refuses to start."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from tunebox.config import ChannelRow, ConfigError, ConfigState, validate_state

# Each entry upgrades the schema by one version; never edit a released one.
# Statements are separated by ";" and contain none themselves.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE config_version (version INTEGER NOT NULL);
    INSERT INTO config_version VALUES (0);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
    CREATE TABLE groups (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        position INTEGER NOT NULL
    );
    CREATE TABLE channels (
        id TEXT PRIMARY KEY,
        group_id INTEGER REFERENCES groups(id),
        position INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        doc_json TEXT NOT NULL
    );
    CREATE TABLE history (
        id INTEGER PRIMARY KEY,
        at REAL NOT NULL,
        kind TEXT NOT NULL,
        target TEXT,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        session_id TEXT
    )
    """,
)


class SettingsUnavailable(Exception):
    """The database cannot be opened, read or brought to this schema."""


class SettingsWriteError(Exception):
    """A change could not be written; nothing was changed."""


class StaleVersion(Exception):
    def __init__(self, current: int) -> None:
        super().__init__(f"the configuration changed elsewhere (now version {current})")
        self.current = current


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def state_to_json(state: ConfigState) -> str:
    return _json(
        {
            "defaults": state.defaults,
            "player": state.player,
            "groups": list(state.groups),
            "channels": [
                {"id": c.id, "group": c.group, "enabled": c.enabled, "doc": c.doc}
                for c in state.channels
            ],
        }
    )


class Settings:
    """Safe to share between threads; one lock serialises its connection."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        db: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            # The cover store writes to the same file from its own connection.
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            _migrate(db)
        except (OSError, sqlite3.Error) as error:
            if db is not None:
                db.close()
            raise SettingsUnavailable(f"{path}: {error}") from error
        self._db = db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def load(self) -> tuple[ConfigState, int]:
        with self._lock:
            try:
                return self._read_state(), self._version()
            except sqlite3.Error as error:
                raise SettingsUnavailable(str(error)) from error

    def history_count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM history").fetchone()
        return cast(int, row[0])

    def adopt_file(self, state: ConfigState) -> int | None:
        """Takes the stations from tunebox.toml when they differ from the
        stored ones; returns the new version, or None when nothing changed.
        A file without stations changes nothing."""
        if not state.channels:
            return None
        current, version = self.load()
        if state_to_json(current) == state_to_json(state):
            return None
        return self.commit(state, version, "file")

    def commit(
        self, state: ConfigState, based_on: int, kind: str, target: str | None = None
    ) -> int:
        """Stores a whole new state if the stored one is still version
        based_on, and returns the new version. The change, its history entry
        and the version bump commit together or not at all."""
        validate_state(state)
        after = state_to_json(state)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                version = self._version()
                if version != based_on:
                    raise StaleVersion(version)
                before = self._read_state()
                if any(c.enabled for c in before.channels) and not any(
                    c.enabled for c in state.channels
                ):
                    raise ConfigError("channels: at least one enabled channel is required")
                self._write(state)
                self._db.execute(
                    "INSERT INTO history (at, kind, target, before_json, after_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (self._clock(), kind, target, state_to_json(before), after),
                )
                self._db.execute("UPDATE config_version SET version = ?", (version + 1,))
                self._db.execute("COMMIT")
            except BaseException as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise SettingsWriteError(str(error)) from error
                raise
        return version + 1

    def _version(self) -> int:
        row = self._db.execute("SELECT version FROM config_version").fetchone()
        return cast(int, row[0])

    def _read_state(self) -> ConfigState:
        values = cast(
            "list[tuple[str, str]]",
            self._db.execute("SELECT key, value_json FROM settings").fetchall(),
        )
        settings = {key: cast(object, json.loads(value)) for key, value in values}
        groups = cast(
            "list[tuple[str]]",
            self._db.execute("SELECT name FROM groups ORDER BY position").fetchall(),
        )
        rows = cast(
            "list[tuple[str, str | None, int, str]]",
            self._db.execute(
                "SELECT c.id, g.name, c.enabled, c.doc_json FROM channels c"
                " LEFT JOIN groups g ON g.id = c.group_id ORDER BY c.position"
            ).fetchall(),
        )
        return ConfigState(
            cast("dict[str, float]", settings.get("defaults", {})),
            cast("dict[str, Any]", settings.get("player", {})),
            tuple(name for (name,) in groups),
            tuple(
                ChannelRow(cid, group, bool(enabled), cast("dict[str, Any]", json.loads(raw)))
                for cid, group, enabled, raw in rows
            ),
        )

    def _write(self, state: ConfigState) -> None:
        """Caller holds the lock and an open transaction."""
        self._db.execute("DELETE FROM channels")
        self._db.execute("DELETE FROM groups")
        self._db.execute("DELETE FROM settings")
        group_ids: dict[str, int | None] = {}
        for position, name in enumerate(state.groups):
            cursor = self._db.execute(
                "INSERT INTO groups (name, position) VALUES (?, ?)", (name, position)
            )
            group_ids[name] = cursor.lastrowid
        for position, row in enumerate(state.channels):
            self._db.execute(
                "INSERT INTO channels VALUES (?, ?, ?, ?, ?)",
                (
                    row.id,
                    group_ids[row.group] if row.group is not None else None,
                    position,
                    int(row.enabled),
                    _json(row.doc),
                ),
            )
        self._db.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [("defaults", _json(state.defaults)), ("player", _json(state.player))],
        )


def _migrate(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = db.execute("SELECT version FROM schema_version").fetchone()
    current = 0 if row is None else cast(int, row[0])
    if current > len(MIGRATIONS):
        raise sqlite3.DatabaseError(
            f"schema version {current} is newer than this tunebox knows ({len(MIGRATIONS)})"
        )
    for number in range(current, len(MIGRATIONS)):
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[number].split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("DELETE FROM schema_version")
            db.execute("INSERT INTO schema_version VALUES (?)", (number + 1,))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
