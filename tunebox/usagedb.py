"""The SQL of the usage database. Every function runs on the connection it
is given: the writer thread's (open_writer) or a panel request's read-only
one (open_reader); nothing here holds state or starts threads."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import date, timedelta, tzinfo
from pathlib import Path
from typing import cast

from tunebox import usagecore
from tunebox.migrate import migrate
from tunebox.usagecore import PAGE, POLL_S, Interval, aggregate, day_bounds, day_of, interval_end

BATCH = 1_000
# Read at call time, so a test can lower it on this module.
DAILY_CAP = usagecore.DAILY_CAP
COMPUTED = (
    "seconds_channel",
    "seconds_stream",
    "seconds_device",
    "seconds_agent",
    "visits",
    "peak",
)
TOP_STATIONS = 5
_CAP_KINDS: tuple[str, ...] = ("visits", "devices", "listens")

# Applied by tunebox.migrate.migrate, which states the rules for entries.
MIGRATIONS: tuple[str, ...] = (
    "CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL,"
    " last_seen REAL NOT NULL, agent TEXT NOT NULL, played INTEGER NOT NULL DEFAULT 0);"
    "CREATE INDEX devices_unplayed ON devices (played) WHERE played = 0;"
    "CREATE TABLE visits (at REAL NOT NULL, device TEXT, agent TEXT NOT NULL, address TEXT);"
    "CREATE INDEX visits_at ON visits (at);"
    "CREATE INDEX visits_device ON visits (device, at);"
    "CREATE TABLE listens (id INTEGER PRIMARY KEY, device TEXT, channel TEXT NOT NULL,"
    " stream TEXT NOT NULL, started_at REAL NOT NULL, last_seen REAL NOT NULL,"
    " agent TEXT NOT NULL, address TEXT);"
    "CREATE INDEX listens_started ON listens (started_at);"
    "CREATE INDEX listens_last ON listens (last_seen);"
    "CREATE INDEX listens_device ON listens (device, started_at);"
    "CREATE TABLE daily (day TEXT NOT NULL, metric TEXT NOT NULL, key TEXT NOT NULL,"
    " value REAL NOT NULL, PRIMARY KEY (day, metric, key));"
    "CREATE INDEX daily_metric ON daily (metric, day);"
    "CREATE INDEX daily_key ON daily (metric, key);"
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

# A session's listened seconds: at least one poll interval.
_SECONDS = f"(max(last_seen, started_at + {POLL_S}) - started_at)"


def open_writer(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        # Deleted devices and cleared addresses are overwritten in the file.
        db.execute("PRAGMA secure_delete=ON")
        migrate(db, MIGRATIONS, "usage")
    except BaseException:
        db.close()
        raise
    return db


def open_reader(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout=5000")
    except BaseException:
        db.close()
        raise
    return db


@contextlib.contextmanager
def _transaction(db: sqlite3.Connection) -> Generator[None]:
    """A COMMIT that fails (disk full, I/O error) can leave the transaction
    open; it is rolled back too, or every later BEGIN on the writer's
    long-lived connection would fail."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def get_meta(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else cast(str, row[0])


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@dataclass
class VisitRow:
    at: float
    device: str
    agent: str
    address: str | None


@dataclass
class SessionRow:
    rowid: int | None
    cookie: str | None
    channel: str
    stream: str
    started_at: float
    last_seen: float
    agent: str
    address: str | None
    device: str | None = None
    resolved: bool = False
    # Refused by the listens cap (Written.dropped): still live listening,
    # never sent to write_batch again.
    discarded: bool = False
    # The last_seen the stored row holds, set after the COMMIT: an update
    # touches only the days from here to the new last_seen.
    stored_last_seen: float | None = None


@dataclass(frozen=True)
class Written:
    # Keyed by id() of each SessionRow stored: its rowid and resolved device.
    rowids: dict[int, tuple[int, str | None]]
    days: set[str]
    # id() of each SessionRow the listens cap refused: never to be resent.
    dropped: set[int]
    # Keyed like rowids: the last_seen each stored row now holds.
    last_seen: dict[int, float] = field(default_factory=dict[int, float])


class _Caps:
    """The day's cap counters, read from meta once per batch and written
    back in the same transaction. They start again when an event's day is
    newer than the counted one; an event of an older day (buffered through
    an outage) counts against the current day, so the bound still holds."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self.day = get_meta(db, "cap_day", "")
        self.counts = {kind: int(get_meta(db, f"cap_{kind}", "0")) for kind in _CAP_KINDS}

    def admit(self, kind: str, day: str) -> bool:
        if day > self.day:
            self.day = day
            self.counts = dict.fromkeys(_CAP_KINDS, 0)
        if self.counts[kind] >= DAILY_CAP:
            self._db.execute(
                "INSERT INTO daily VALUES (?, 'dropped', ?, 1)"
                " ON CONFLICT (day, metric, key) DO UPDATE SET value = value + 1",
                (day, kind),
            )
            return False
        self.counts[kind] += 1
        return True

    def save(self) -> None:
        set_meta(self._db, "cap_day", self.day)
        for kind, count in self.counts.items():
            set_meta(self._db, f"cap_{kind}", str(count))


def _days(first: float, last: float, zone: tzinfo) -> set[str]:
    day, end = date.fromisoformat(day_of(first, zone)), date.fromisoformat(day_of(last, zone))
    days: set[str] = set()
    while day <= end:
        days.add(day.isoformat())
        day += timedelta(days=1)
    return days


# Listening counts as seen: a player left open makes no new visit, and
# retention keeps a device as long as it is seen.
_HEARD = "UPDATE devices SET last_seen = max(last_seen, ?) WHERE id = ?"


def write_batch(
    db: sqlite3.Connection,
    visits: list[VisitRow],
    sessions: list[SessionRow],
    named: set[str],
    zone: tzinfo,
) -> Written:
    """One flush in one transaction. Visits come first, so a session from a
    page loaded in the same flush finds its device row. A session the cap
    refuses is in dropped, not in rowids, and touches no day. An update
    touches the days from the stored last_seen on, and none when it is
    unchanged, so an open session does not re-roll its first day on every
    flush."""
    rowids: dict[int, tuple[int, str | None]] = {}
    stored: dict[int, float] = {}
    days: set[str] = set()
    dropped: set[int] = set()
    with _transaction(db):
        caps = _Caps(db)
        for visit in visits:
            day = day_of(visit.at, zone)
            known = db.execute(
                "UPDATE devices SET last_seen = max(last_seen, ?), agent = ? WHERE id = ?",
                (visit.at, visit.agent, visit.device),
            ).rowcount
            if not known and caps.admit("devices", day):
                db.execute(
                    "INSERT INTO devices (id, first_seen, last_seen, agent) VALUES (?, ?, ?, ?)",
                    (visit.device, visit.at, visit.at, visit.agent),
                )
            if caps.admit("visits", day):
                db.execute(
                    "INSERT INTO visits VALUES (?, ?, ?, ?)",
                    (visit.at, visit.device, visit.agent, visit.address),
                )
                days.add(day)
        for row in sessions:
            # Read once: a request thread may move it on meanwhile.
            last_seen = row.last_seen
            if row.rowid is not None:
                db.execute("UPDATE listens SET last_seen = ? WHERE id = ?", (last_seen, row.rowid))
                if row.device is not None:
                    db.execute(_HEARD, (last_seen, row.device))
                rowids[id(row)] = (row.rowid, row.device)
                stored[id(row)] = last_seen
                before = row.started_at if row.stored_last_seen is None else row.stored_last_seen
                if last_seen != before:
                    days |= _days(before, interval_end(row.started_at, last_seen), zone)
                continue
            device = None
            if row.cookie is not None:
                found = db.execute("SELECT 1 FROM devices WHERE id = ?", (row.cookie,)).fetchone()
                device = row.cookie if found is not None else None
            if device not in named and not caps.admit("listens", day_of(row.started_at, zone)):
                dropped.add(id(row))
                continue
            cursor = db.execute(
                "INSERT INTO listens (device, channel, stream, started_at, last_seen, agent,"
                " address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    device,
                    row.channel,
                    row.stream,
                    row.started_at,
                    last_seen,
                    row.agent,
                    row.address,
                ),
            )
            if device is not None:
                db.execute("UPDATE devices SET played = 1 WHERE id = ?", (device,))
                db.execute(_HEARD, (last_seen, device))
            rowids[id(row)] = (cast(int, cursor.lastrowid), device)
            stored[id(row)] = last_seen
            days |= _days(row.started_at, interval_end(row.started_at, last_seen), zone)
        caps.save()
    return Written(rowids, days, dropped, stored)


def _intervals(rows: list[tuple[object, ...]]) -> list[Interval]:
    """Rows of (started_at, last_seen, channel, stream, device, agent)."""
    intervals: list[Interval] = []
    for started_at, last_seen, channel, stream, device, agent in rows:
        start = cast(float, started_at)
        intervals.append(
            Interval(
                start,
                interval_end(start, cast(float, last_seen)),
                cast(str, channel),
                cast(str, stream),
                cast(str | None, device),
                cast(str, agent),
            )
        )
    return intervals


def roll_up(db: sqlite3.Connection, day: str, zone: tzinfo) -> tuple[int, int]:
    """Replaces the day's computed totals whole, so a crash mid-way is
    redone on the next run; dropped counts are written by write_batch and
    add_dropped and kept. Returns the sessions and visits counted."""
    start, end = day_bounds(day, zone)
    with _transaction(db):
        intervals = _intervals(
            db.execute(
                "SELECT started_at, last_seen, channel, stream, device, agent FROM listens"
                " WHERE last_seen > ? AND started_at < ?",
                (start - POLL_S, end),
            ).fetchall()
        )
        visits = [
            cast(float, r[0])
            for r in db.execute("SELECT at FROM visits WHERE at >= ? AND at < ?", (start, end))
        ]
        totals = aggregate(intervals, visits, start, end)
        db.execute(
            f"DELETE FROM daily WHERE day = ? AND metric IN ({', '.join('?' * len(COMPUTED))})",
            (day, *COMPUTED),
        )
        rows: list[tuple[str, str, str, float]] = [
            (day, metric, key, value)
            for metric, sums in (
                ("seconds_channel", totals.seconds_channel),
                ("seconds_stream", totals.seconds_stream),
                ("seconds_device", totals.seconds_device),
                ("seconds_agent", totals.seconds_agent),
            )
            for key, value in sums.items()
        ]
        rows += [(day, "visits", "-", totals.visits), (day, "peak", "-", totals.peak)]
        db.executemany("INSERT INTO daily VALUES (?, ?, ?, ?)", rows)
    return len(intervals), totals.visits


def add_dropped(db: sqlite3.Connection, day: str, kind: str, count: int) -> None:
    with _transaction(db):
        db.execute(
            "INSERT INTO daily VALUES (?, 'dropped', ?, ?)"
            " ON CONFLICT (day, metric, key) DO UPDATE SET value = value + excluded.value",
            (day, kind, count),
        )


def oldest_day(db: sqlite3.Connection) -> str | None:
    return cast(str | None, db.execute("SELECT min(day) FROM daily").fetchone()[0])


def _one_batch(
    db: sqlite3.Connection, statement: str, table: str, where: str, args: tuple[object, ...]
) -> int:
    """Runs statement ("DELETE FROM t" or "UPDATE t SET ...") on at most
    BATCH rows in its own transaction, so the writer never holds the write
    lock long. table and where are constants of this module."""
    with _transaction(db):
        return db.execute(
            f"{statement} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {BATCH})",
            args,
        ).rowcount


def _batched(
    db: sqlite3.Connection,
    statement: str,
    table: str,
    where: str,
    args: tuple[object, ...],
    stop: threading.Event | None = None,
) -> int:
    total = 0
    while stop is None or not stop.is_set():
        done = _one_batch(db, statement, table, where, args)
        total += done
        if done < BATCH:
            break
    return total


def _delete(
    db: sqlite3.Connection,
    table: str,
    where: str,
    args: tuple[object, ...],
    stop: threading.Event | None = None,
) -> int:
    return _batched(db, f"DELETE FROM {table}", table, where, args, stop)


def retention(db: sqlite3.Connection, cutoff: float, stop: threading.Event) -> int:
    """Deletes rows older than cutoff and unnamed devices last seen before
    it, with their per-device totals. A device whose sessions are still
    newer is kept: a page left open plays on without a new visit, so the
    device's last_seen alone would orphan those sessions."""
    deleted = _delete(db, "visits", "at < ?", (cutoff,), stop)
    deleted += _delete(db, "listens", "last_seen < ?", (cutoff,), stop)
    while not stop.is_set():
        with _transaction(db):
            ids = [
                (cast(str, r[0]),)
                for r in db.execute(
                    "SELECT id FROM devices WHERE name IS NULL AND last_seen < ?"
                    " AND NOT EXISTS (SELECT 1 FROM listens WHERE listens.device = devices.id)"
                    f" LIMIT {BATCH}",
                    (cutoff,),
                )
            ]
            db.executemany("DELETE FROM daily WHERE metric = 'seconds_device' AND key = ?", ids)
            db.executemany("DELETE FROM devices WHERE id = ?", ids)
        deleted += len(ids)
        if len(ids) < BATCH:
            break
    return deleted


def clear_addresses(db: sqlite3.Connection) -> None:
    """Blanks every stored address, then truncates the WAL so the old pages
    leave the file too (secure_delete overwrites them in the database)."""
    for table in ("visits", "listens"):
        _batched(db, f"UPDATE {table} SET address = NULL", table, "address IS NOT NULL", ())
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def delete_device(db: sqlite3.Connection, device: str) -> bool:
    """The device row goes last, so a delete cut short is finished by
    repeating it."""
    if db.execute("SELECT 1 FROM devices WHERE id = ?", (device,)).fetchone() is None:
        return False
    _delete(db, "visits", "device = ?", (device,))
    _delete(db, "listens", "device = ?", (device,))
    _delete(db, "daily", "metric = 'seconds_device' AND key = ?", (device,))
    db.execute("DELETE FROM devices WHERE id = ?", (device,))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return True


def rename_device(db: sqlite3.Connection, device: str, name: str | None) -> bool:
    return db.execute("UPDATE devices SET name = ? WHERE id = ?", (name, device)).rowcount == 1


def named_devices(db: sqlite3.Connection) -> dict[str, str]:
    return {
        cast(str, r[0]): cast(str, r[1])
        for r in db.execute("SELECT id, name FROM devices WHERE name IS NOT NULL")
    }


def last_ended(db: sqlite3.Connection) -> dict[str, object] | None:
    row = db.execute(
        "SELECT device, channel, stream, started_at, last_seen, agent FROM listens"
        " ORDER BY last_seen DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return dict(
        zip(("device", "channel", "stream", "started_at", "last_seen", "agent"), row, strict=True)
    )


def daily_series(
    db: sqlite3.Connection, metric: str, first_day: str, last_day: str
) -> dict[str, float]:
    return {
        cast(str, r[0]): cast(float, r[1])
        for r in db.execute(
            "SELECT day, sum(value) FROM daily WHERE metric = ? AND day BETWEEN ? AND ?"
            " GROUP BY day",
            (metric, first_day, last_day),
        )
    }


def daily_tops(
    db: sqlite3.Connection, metric: str, first_day: str, last_day: str
) -> list[tuple[str, float]]:
    """Every key's total, largest first: the caller merges them with the
    open days' totals before cutting its top list."""
    return [
        (cast(str, r[0]), cast(float, r[1]))
        for r in db.execute(
            "SELECT key, sum(value) AS total FROM daily WHERE metric = ? AND day BETWEEN ? AND ?"
            " GROUP BY key ORDER BY total DESC, key",
            (metric, first_day, last_day),
        )
    ]


def daily_peak(db: sqlite3.Connection, first_day: str, last_day: str) -> tuple[int, str | None]:
    """The most listeners at once in the range and the latest day it happened."""
    row = db.execute(
        "SELECT value, day FROM daily WHERE metric = 'peak' AND day BETWEEN ? AND ?"
        " ORDER BY value DESC, day DESC LIMIT 1",
        (first_day, last_day),
    ).fetchone()
    if row is None or cast(float, row[0]) <= 0:
        return 0, None
    return int(cast(float, row[0])), cast(str, row[1])


# Unordered: an ORDER BY makes SQLite scan the whole table by
# listens_started instead of searching listens_last; aggregate sorts its own
# points.
LISTENS_AFTER = (
    "SELECT id, started_at, last_seen, channel, stream, device, agent FROM listens"
    " WHERE last_seen > ?"
)


def rows_after(
    db: sqlite3.Connection, since: float, exclude: set[int]
) -> tuple[list[Interval], list[float]]:
    """Sessions reaching past since and visits from since on: what the
    daily totals do not hold yet. exclude names the rowids of sessions the
    caller holds in memory, whose stored last_seen may be behind."""
    rows = [
        cast(tuple[object, ...], r[1:])
        for r in db.execute(
            LISTENS_AFTER,
            (since - POLL_S,),
        )
        if cast(int, r[0]) not in exclude
    ]
    visits = [
        cast(float, r[0])
        for r in db.execute("SELECT at FROM visits WHERE at >= ? ORDER BY at", (since,))
    ]
    return _intervals(rows), visits


# A session's listened seconds from :since on. It overlaps :since when its
# widened end (interval_end) is past it, so last_seen > :since - POLL_S
# finds every such session; one that started earlier counts its part after.
_SINCE = f"max(0.0, max(last_seen, started_at + {POLL_S}) - max(started_at, :since))"
_OVERLAPS = f"last_seen > :since - {POLL_S}"
DEVICES = (
    "SELECT d.id, d.name, d.agent, d.first_seen, d.last_seen,"
    f" (SELECT coalesce(sum({_SINCE}), 0.0) FROM listens"
    f"  WHERE listens.device = d.id AND {_OVERLAPS})"
    " FROM devices AS d WHERE d.played = 1 ORDER BY d.last_seen DESC, d.id"
)
DEVICE_TOP = (
    f"SELECT channel, sum({_SINCE}) AS total FROM listens"
    f" WHERE device = :device AND {_OVERLAPS} GROUP BY channel HAVING total > 0"
    " ORDER BY total DESC, channel LIMIT :top"
)


def devices_page(db: sqlite3.Connection, since_30d: float) -> tuple[list[dict[str, object]], int]:
    """Devices that played, newest first, with their listening since
    since_30d; and the count of devices that never played."""
    keys: tuple[str, ...] = ("id", "name", "agent", "first_seen", "last_seen", "seconds_30d")
    devices: list[dict[str, object]] = [
        dict(zip(keys, row, strict=True)) for row in db.execute(DEVICES, {"since": since_30d})
    ]
    never = cast(int, db.execute("SELECT count(*) FROM devices WHERE played = 0").fetchone()[0])
    return devices, never


def device_page(
    db: sqlite3.Connection, device: str, before: float | None, since_30d: float
) -> dict[str, object] | None:
    """One device: PAGE sessions newest first that started before the
    cursor, and its top stations by listening since since_30d."""
    row = db.execute(
        "SELECT id, name, agent, first_seen, last_seen FROM devices WHERE id = ?", (device,)
    ).fetchone()
    if row is None:
        return None
    page: dict[str, object] = dict(
        zip(("id", "name", "agent", "first_seen", "last_seen"), row, strict=True)
    )
    keys: tuple[str, ...] = (
        "id",
        "channel",
        "stream",
        "started_at",
        "last_seen",
        "seconds",
        "address",
    )
    page["sessions"] = [
        dict(zip(keys, r, strict=True))
        for r in db.execute(
            f"SELECT id, channel, stream, started_at, last_seen, {_SECONDS}, address"
            " FROM listens WHERE device = ? AND started_at < ? ORDER BY started_at DESC LIMIT ?",
            (device, float("inf") if before is None else before, PAGE),
        )
    ]
    page["top"] = [
        [cast(str, r[0]), cast(float, r[1])]
        for r in db.execute(DEVICE_TOP, {"device": device, "since": since_30d, "top": TOP_STATIONS})
    ]
    return page
