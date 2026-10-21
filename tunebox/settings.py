"""The configuration the admin panel edits: stations, groups and settings,
with a version that every change bumps and a history of every change. The
same database holds the admin account, its sessions and recovery codes,
the listening PIN, and the server's signing key (used by accounts.py and
pin.py).

Unlike the cover store this never falls back to memory: edits that vanish
on the next restart are worse than a tunebox that refuses to start."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tunebox.config import (
    BLOB_REF,
    ChannelRow,
    ConfigError,
    ConfigState,
    keeps_a_channel,
    validate_state,
)
from tunebox.logs import event

log = logging.getLogger(__name__)

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
    """
    CREATE TABLE admin (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        password_hash TEXT NOT NULL,
        totp_secret BLOB NOT NULL,
        totp_last_step INTEGER NOT NULL,
        totp_fail_count INTEGER NOT NULL,
        totp_fail_since REAL,
        totp_fail_last REAL,
        generation INTEGER NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE recovery_codes (hash TEXT PRIMARY KEY, used_at REAL);
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        absolute_expiry REAL NOT NULL,
        address TEXT,
        user_agent TEXT
    );
    CREATE TABLE secrets (key TEXT PRIMARY KEY, value BLOB NOT NULL)
    """,
    """
    ALTER TABLE sessions ADD COLUMN reauth_at REAL;
    CREATE TABLE listener_pin (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        mode TEXT NOT NULL,
        digest TEXT,
        generation INTEGER NOT NULL
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


def state_from_dict(data: object) -> ConfigState:
    """The panel's JSON form of a state, checked for shape; validate_state
    checks the content when it is committed."""
    if not isinstance(data, dict):
        raise ConfigError("state: expected an object")
    table = cast("dict[str, object]", data)
    defaults = table.get("defaults", {})
    player = table.get("player", {})
    groups = table.get("groups", [])
    channels = table.get("channels", [])
    if not isinstance(defaults, dict):
        raise ConfigError("defaults: expected an object")
    if not isinstance(player, dict):
        raise ConfigError("player: expected an object")
    if not isinstance(groups, list):
        raise ConfigError("groups: expected a list")
    if not isinstance(channels, list):
        raise ConfigError("channels: expected a list")
    names: list[str] = []
    for index, name in enumerate(cast("list[object]", groups)):
        if not isinstance(name, str):
            raise ConfigError(f"groups[{index}]: expected a string")
        names.append(name)
    rows: list[ChannelRow] = []
    for index, raw in enumerate(cast("list[object]", channels)):
        where = f"channels[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected an object")
        entry = cast("dict[str, object]", raw)
        cid, group, enabled, body = (entry.get(k) for k in ("id", "group", "enabled", "doc"))
        if not isinstance(cid, str):
            raise ConfigError(f"{where}.id: expected a string")
        if group is not None and not isinstance(group, str):
            raise ConfigError(f"{where}.group: expected a string or null")
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}.enabled: expected true or false")
        if not isinstance(body, dict):
            raise ConfigError(f"{where}.doc: expected an object")
        if "group" in body:
            # Stored on the row only (doc_json is a [[channels]] table without
            # its group), so the panel and the player read one value.
            raise ConfigError(f"{where}.doc.group: the group belongs on the station, not its doc")
        rows.append(ChannelRow(cid, group, enabled, cast("dict[str, Any]", body)))
    return ConfigState(
        cast("dict[str, float]", defaults),
        cast("dict[str, Any]", player),
        tuple(names),
        tuple(rows),
    )


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    at: float
    # file, import, edit or restore
    kind: str
    target: str | None


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

    def version(self) -> int:
        """The latest committed version, without reading the state."""
        with self.read() as db:
            row = db.execute("SELECT version FROM config_version").fetchone()
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

    def import_once(self, state: ConfigState) -> int | None:
        """Stores the stations from tunebox.toml when the database has none;
        afterwards the panel is their source."""
        current, version = self.load()
        if current.channels or not state.channels:
            return None
        return self.commit(state, version, "import")

    def history(
        self, before: int | None = None, limit: int = 50
    ) -> tuple[list[HistoryEntry], bool]:
        """Newest first; before is the id of the last entry already shown.
        Also says whether older entries exist."""
        with self.read() as db:
            rows = cast(
                "list[tuple[int, float, str, str | None]]",
                db.execute(
                    "SELECT id, at, kind, target FROM history WHERE id < ?"
                    " ORDER BY id DESC LIMIT ?",
                    (before if before is not None else 2**62, limit + 1),
                ).fetchall(),
            )
        return [HistoryEntry(*row) for row in rows[:limit]], len(rows) > limit

    def history_entry(self, entry_id: int) -> tuple[HistoryEntry, ConfigState, ConfigState] | None:
        """The entry with the state before and after it."""
        with self.read() as db:
            row = db.execute(
                "SELECT id, at, kind, target, before_json, after_json FROM history WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            return None
        found, at, kind, target, before, after = cast(
            "tuple[int, float, str, str | None, str, str]", row
        )
        return (
            HistoryEntry(found, at, kind, target),
            state_from_dict(json.loads(before)),
            state_from_dict(json.loads(after)),
        )

    def oldest_change(self) -> float | None:
        """When the first change was stored; None while there is none."""
        with self.read() as db:
            row = db.execute("SELECT MIN(at) FROM history").fetchone()
        return cast("float | None", row[0])

    def referenced_blobs(self) -> set[str]:
        """Every uploaded-logo reference in the stored stations and in all
        history, which keeps a restore able to find its logos."""
        found: set[str] = set()
        with self.read() as db:
            for table, columns in (
                ("channels", "doc_json"),
                ("history", "before_json || after_json"),
            ):
                for (text,) in db.execute(
                    f"SELECT {columns} FROM {table} WHERE {columns} LIKE '%blob:%'"
                ):
                    found.update(f"blob:{sha}" for sha in BLOB_REF.findall(cast(str, text)))
        return found

    def commit(
        self, state: ConfigState, based_on: int, kind: str, target: str | None = None
    ) -> int:
        return self.commit_changed(state, based_on, kind, target)[0]

    def commit_changed(
        self, state: ConfigState, based_on: int, kind: str, target: str | None = None
    ) -> tuple[int, bool]:
        """Stores a whole new state if the stored one is still version
        based_on; returns the version now stored and whether this changed
        it. The change, its history entry and the version bump commit
        together or not at all. A state equal to the stored one stores
        nothing, so the history holds changes only."""
        validate_state(state)
        after = state_to_json(state)
        with self.transaction() as db:
            version = self._version()
            if version != based_on:
                raise StaleVersion(version)
            before = self._read_state()
            if state_to_json(before) == after:
                return version, False
            keeps_a_channel(before, state)
            self._write(state)
            db.execute(
                "INSERT INTO history (at, kind, target, before_json, after_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._clock(), kind, target, state_to_json(before), after),
            )
            db.execute("UPDATE config_version SET version = ?", (version + 1,))
        return version + 1, True

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """One write transaction: committed when the block ends, rolled back
        on any error. A database error becomes SettingsWriteError."""
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._db.execute("COMMIT")
            except BaseException as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise SettingsWriteError(str(error)) from error
                raise

    @contextlib.contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
            except sqlite3.Error as error:
                raise SettingsUnavailable(str(error)) from error

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
        event(log, logging.INFO, "migration_applied", version=number + 1)
