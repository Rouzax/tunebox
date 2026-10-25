"""Usage statistics for the admin panel. Request threads only touch memory
(Live); one writer thread owns usage.db. Exists only while the panel is
enabled."""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from pathlib import Path
from typing import cast

from tunebox import usagedb
from tunebox.disk import has_room
from tunebox.logs import event
from tunebox.migrate import SchemaTooNew
from tunebox.usagecore import (
    COMMAND_WAIT_S,
    FLUSH_S,
    IDLE_S,
    MAX_LIVE,
    MAX_PENDING,
    RETENTION_DEFAULT,
    RETENTION_MAX,
    RETENTION_MIN,
    RETRY_S,
    Interval,
    aggregate,
    day_bounds,
    day_of,
    interval_end,
)
from tunebox.usagedb import SessionRow, VisitRow, Written

log = logging.getLogger(__name__)

UNNAMED = "Unnamed device"
UNIDENTIFIED = "Unidentified player"

# (cookie id or "~" + short agent, channel, stream). A cookie id is hex, so
# the "~" keeps the two kinds apart.
type _Key = tuple[str, str, str]


def _key(cookie: str | None, agent: str, channel: str, stream: str) -> _Key:
    return (cookie if cookie is not None else "~" + agent, channel, stream)


def _seconds(started_at: float, last_seen: float) -> float:
    return interval_end(started_at, last_seen) - started_at


class Live:
    """Open listening sessions, pending visits and closed sessions waiting
    for the writer, and the last session that ended. Every method takes the
    one lock briefly and never touches SQLite. The session objects handed to
    the writer stay shared with memory, so adopt() reaches the open ones."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[_Key, SessionRow] = {}
        self._visits: list[VisitRow] = []
        self._closed: list[SessionRow] = []
        self._addresses = False
        self._names: dict[str, str] = {}
        # Pending rows dropped past MAX_PENDING, not yet taken by the writer.
        self._dropped = 0
        # The shape of usagedb.last_ended: device (or the unresolved cookie
        # id), channel, stream, started_at, last_seen, agent.
        self._last: dict[str, object] | None = None

    @property
    def addresses(self) -> bool:
        with self._lock:
            return self._addresses

    @addresses.setter
    def addresses(self, value: bool) -> None:
        """Off takes effect before this returns: nothing held in memory
        keeps an address the writer could still store."""
        with self._lock:
            self._addresses = value
            if value:
                return
            for session in (*self._open.values(), *self._closed):
                session.address = None
            for visit in self._visits:
                visit.address = None

    @property
    def names(self) -> dict[str, str]:
        with self._lock:
            return dict(self._names)

    @names.setter
    def names(self, value: dict[str, str]) -> None:
        with self._lock:
            self._names = dict(value)

    def visit(self, device: str, agent: str, address: str | None) -> None:
        with self._lock:
            kept = address if self._addresses else None
            self._visits.append(VisitRow(self._clock(), device, agent, kept))
            self._trim()

    def heard(
        self, cookie: str | None, channel: str, stream: str, agent: str, address: str | None
    ) -> None:
        with self._lock:
            now = self._clock()
            key = _key(cookie, agent, channel, stream)
            session = self._open.get(key)
            if session is not None:
                if now - session.last_seen < IDLE_S:
                    session.last_seen = max(session.last_seen, now)
                    return
                self._close(key)
            if cookie is not None:
                # A station or stream change: one device plays one stream.
                for other in [k for k, s in self._open.items() if s.cookie == cookie]:
                    self._close(other)
            if len(self._open) >= MAX_LIVE:
                self._close_idle(now)
            if len(self._open) >= MAX_LIVE:
                self._evict()
            kept = address if self._addresses else None
            self._open[key] = SessionRow(None, cookie, channel, stream, now, now, agent, kept)

    def live(self) -> list[dict[str, object]]:
        with self._lock:
            now = self._clock()
            rows: list[dict[str, object]] = []
            for session in sorted(self._open.values(), key=lambda s: s.started_at):
                if now - session.last_seen >= IDLE_S:
                    continue
                row: dict[str, object] = {
                    "label": self._session_label(session),
                    "channel": session.channel,
                    "stream": session.stream,
                    "seconds": _seconds(session.started_at, session.last_seen),
                }
                if self._addresses:
                    row["address"] = session.address
                rows.append(row)
            return rows

    def last_ended(self) -> dict[str, object] | None:
        with self._lock:
            if self._last is None:
                return None
            started_at = cast(float, self._last["started_at"])
            last_seen = cast(float, self._last["last_seen"])
            return {
                "label": self._label(
                    cast(str | None, self._last["device"]), cast(str, self._last["agent"])
                ),
                "channel": self._last["channel"],
                "stream": self._last["stream"],
                "seconds": _seconds(started_at, last_seen),
                "ended_at": last_seen,
            }

    def set_last_ended(self, row: dict[str, object] | None) -> None:
        with self._lock:
            self._last = None if row is None else dict(row)

    def detach(self) -> tuple[list[VisitRow], list[SessionRow], list[tuple[SessionRow, float]]]:
        """The writer's batch: pending visits and closed sessions (emptied
        here), and the open sessions to insert (rowid None) or to update
        (rowid set). A session the listens cap refused is never returned."""
        with self._lock:
            self._close_idle(self._clock())
            visits = self._visits
            closed = [s for s in self._closed if not s.discarded]
            self._visits, self._closed = [], []
            open_rows = [(s, s.last_seen) for s in self._open.values() if not s.discarded]
            return visits, closed, open_rows

    def adopt(
        self, written: Written, open_rows: list[tuple[SessionRow, float]], closed: list[SessionRow]
    ) -> None:
        """After the COMMIT only, so a rowid in memory always names a stored
        row. A closed row's rowid matters only if it is requeued."""
        with self._lock:
            for session in (*(s for s, _ in open_rows), *closed):
                if id(session) in written.dropped:
                    session.discarded = True
                    session.resolved = True
                    continue
                found = written.rowids.get(id(session))
                if found is not None:
                    session.rowid, session.device = found
                    session.resolved = True

    def requeue(self, visits: list[VisitRow], closed: list[SessionRow]) -> int:
        """A failed batch goes back in front of the rows queued since. Past
        MAX_PENDING in all, the oldest rows of either kind go first."""
        with self._lock:
            all_visits = [*visits, *self._visits]
            all_closed = [*closed, *self._closed]
            excess = len(all_visits) + len(all_closed) - MAX_PENDING
            if excess > 0:
                ages = sorted(
                    [(v.at, 0, i) for i, v in enumerate(all_visits)]
                    + [(s.last_seen, 1, i) for i, s in enumerate(all_closed)]
                )[:excess]
                gone = {(kind, i) for _, kind, i in ages}
                all_visits = [v for i, v in enumerate(all_visits) if (0, i) not in gone]
                all_closed = [s for i, s in enumerate(all_closed) if (1, i) not in gone]
            self._visits, self._closed = all_visits, all_closed
            self._dropped += max(excess, 0)
            return max(excess, 0)

    def take_dropped(self) -> int:
        """Rows dropped past MAX_PENDING since the last call, for the
        writer to store as dropped/pending."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
            return dropped

    def close_all(self) -> None:
        with self._lock:
            for key in list(self._open):
                self._close(key)

    def forget(self, device: str) -> None:
        with self._lock:
            for key in [k for k, s in self._open.items() if s.cookie == device]:
                del self._open[key]
            self._visits = [v for v in self._visits if v.device != device]
            self._closed = [s for s in self._closed if s.cookie != device]
            if self._last is not None and self._last["device"] == device:
                self._last = None

    def snapshot(self) -> tuple[set[int], list[Interval], list[float]]:
        """Taken together, so a session is either held here (its rowid
        listed) or read from the database, never both."""
        with self._lock:
            return self._rowids(), self._intervals(), [v.at for v in self._visits]

    def _held(self) -> list[SessionRow]:
        return [*self._open.values(), *(s for s in self._closed if not s.discarded)]

    def _rowids(self) -> set[int]:
        return {s.rowid for s in self._held() if s.rowid is not None}

    def _intervals(self) -> list[Interval]:
        # Unresolved sessions count as unidentified until their first flush.
        return [
            Interval(
                s.started_at,
                interval_end(s.started_at, s.last_seen),
                s.channel,
                s.stream,
                s.device if s.resolved else None,
                s.agent,
            )
            for s in self._held()
        ]

    def _close(self, key: _Key) -> None:
        session = self._open.pop(key)
        if not session.discarded:
            self._closed.append(session)
            self._trim()
        last = self._last
        if last is None or session.last_seen >= cast(float, last["last_seen"]):
            self._last = {
                "device": session.device if session.resolved else session.cookie,
                "channel": session.channel,
                "stream": session.stream,
                "started_at": session.started_at,
                "last_seen": session.last_seen,
                "agent": session.agent,
            }

    def _trim(self) -> None:
        """Bounds memory between flushes and through an outage: past
        MAX_PENDING the oldest pending row of either kind goes."""
        while len(self._visits) + len(self._closed) > MAX_PENDING:
            visits, closed = self._visits, self._closed
            if closed and (not visits or closed[0].last_seen < visits[0].at):
                closed.pop(0)
            else:
                visits.pop(0)
            self._dropped += 1

    def _close_idle(self, now: float) -> None:
        for key in [k for k, s in self._open.items() if now - s.last_seen >= IDLE_S]:
            self._close(key)

    def _evict(self) -> None:
        """Closes one session early: unidentified players first, then
        unnamed devices, then named ones; the longest idle within each."""

        def rank(item: tuple[_Key, SessionRow]) -> tuple[int, float]:
            session = item[1]
            if session.cookie is None:
                kind = 0
            elif session.cookie not in self._names:
                kind = 1
            else:
                kind = 2
            return kind, session.last_seen

        key, _ = min(self._open.items(), key=rank)
        self._close(key)

    def _session_label(self, session: SessionRow) -> str:
        if session.cookie is not None and session.cookie in self._names:
            return self._names[session.cookie]
        return self._label(session.device if session.resolved else session.cookie, session.agent)

    def _label(self, device: str | None, agent: str) -> str:
        if device is None:
            return f"{UNIDENTIFIED} · {agent}"
        name = self._names.get(device)
        return name if name is not None else f"{UNNAMED} · {agent}"


class UsageUnavailable(Exception):
    """usage.db cannot answer now; the panel answers 503 usage_unavailable."""


@dataclass(frozen=True)
class UsageSettings:
    retention_days: int
    store_addresses: bool
    saved: bool


type _Command = Callable[[sqlite3.Connection], object]
# A None command asks the writer for one round (work_once).
type _Item = tuple[_Command | None, Future[object]]

# How long the writer waits on its queue, so a set stop is seen quickly.
_WAIT_S = 0.5
# Free space a flush asks for on top of disk.MIN_FREE.
_ROOM = 1_000_000
_TOP = 10
# SQLite reads a negative LIMIT as none: finished days' tops are merged
# with the open days' before the top ten are cut.
_ALL = -1
_DAY_S = 86_400
_RANGES = ("7d", "30d", "12m", "all")


class _DiskLow(Exception):
    pass


def _retention_of(text: str) -> int:
    try:
        days = int(text)
    except ValueError:
        return RETENTION_DEFAULT
    return days if RETENTION_MIN <= days <= RETENTION_MAX else RETENTION_DEFAULT


def _months(first: date, last: date) -> list[str]:
    labels: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return labels


def _top(sums: dict[str, float]) -> list[list[object]]:
    ranked = sorted(sums.items(), key=lambda item: (-item[1], item[0]))[:_TOP]
    return [[key, value] for key, value in ranked]


class Usage:
    """Usage statistics: request threads feed Live and never wait on
    SQLite; one writer thread owns the read-write connection and runs every
    flush, roll-up, retention pass and panel command; panel reads open their
    own read-only connection.

    Locks: _state guards the flags shared with request and panel threads and
    is taken before Live's lock, never inside it. _flush is held by the
    writer from detach to adopt and by a summary over its snapshot and
    read, so a summary never counts a session both from memory and from the
    file, or from neither. No lock a request thread takes is held over
    SQLite."""

    def __init__(
        self,
        path: Path,
        *,
        zone: tzinfo,
        zone_name: str,
        free: Callable[[], int | None],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        flush_s: float = FLUSH_S,
    ) -> None:
        self._path = path
        self._zone = zone
        self._zone_name = zone_name
        self._free = free
        self._clock = clock
        self._monotonic = monotonic
        self._flush_s = flush_s
        self._live = Live(clock)
        self._commands: queue.Queue[_Item] = queue.Queue()
        self._state = threading.Lock()
        self._flush = threading.Lock()
        self._available = False
        self._logged_down = False
        self._clear_pending = False
        self._retention = RETENTION_DEFAULT
        self._seen: set[str] = set()
        self._thread: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        # Writer thread only from here on.
        self._db: sqlite3.Connection | None = None
        self._schema_newer = False
        self._retry_at = 0.0
        self._last_round = 0.0
        self._dropped = 0
        self._never = threading.Event()

    # Request threads.

    def visit(self, device: str, agent: str, address: str | None) -> None:
        try:
            self._live.visit(device, agent, address)
        except Exception as exc:  # noqa: BLE001 - a player request never fails because of usage
            self._failed(exc)

    def heard(
        self, cookie: str | None, channel: str, stream: str, agent: str, address: str | None
    ) -> None:
        try:
            self._live.heard(cookie, channel, stream, agent, address)
        except Exception as exc:  # noqa: BLE001 - a player request never fails because of usage
            self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        kind = type(exc).__name__
        if kind in self._seen:
            return
        self._seen.add(kind)
        # The kind only: the text could carry a device id or an address.
        event(log, logging.ERROR, "usage_failed", kind=kind)

    # Panel, from memory.

    def live(self) -> dict[str, object]:
        return {
            "sessions": self._live.live(),
            "last_ended": self._live.last_ended(),
            "saved": self.settings().saved,
            "addresses": self._live.addresses,
        }

    def settings(self) -> UsageSettings:
        with self._state:
            return UsageSettings(
                self._retention,
                self._live.addresses,
                self._available and not self._clear_pending,
            )

    # Panel commands, run on the writer.

    def rename(self, device: str, name: str | None) -> bool:
        def run(db: sqlite3.Connection) -> bool:
            changed = usagedb.rename_device(db, device, name)
            if changed:
                names = self._live.names
                if name is None:
                    names.pop(device, None)
                else:
                    names[device] = name
                self._live.names = names
            return changed

        return cast(bool, self._call(run))

    def delete(self, device: str) -> bool:
        def run(db: sqlite3.Connection) -> bool:
            # First, so no open or pending row of the device is written back.
            self._live.forget(device)
            deleted = usagedb.delete_device(db, device)
            names = self._live.names
            names.pop(device, None)
            self._live.names = names
            return deleted

        return cast(bool, self._call(run))

    def set_retention(self, days: int) -> UsageSettings:
        if not RETENTION_MIN <= days <= RETENTION_MAX:
            raise ValueError(f"retention_days must be {RETENTION_MIN} to {RETENTION_MAX}")

        def run(db: sqlite3.Connection) -> None:
            usagedb.set_meta(db, "retention_days", str(days))
            with self._state:
                self._retention = days
            self._log_settings()

        self._call(run)
        return self.settings()

    def set_addresses(self, on: bool) -> UsageSettings:
        """Off takes effect in memory before anything else and never fails:
        while the store cannot be written the answer says saved False and
        the writer stores it and clears addresses first on recovery."""
        if on:
            self._call(self._addresses_on)
            return self.settings()
        with self._state:
            self._live.addresses = False
            self._clear_pending = True
            available = self._available
        if available:
            with contextlib.suppress(UsageUnavailable):
                self._call(self._clear_if_pending)
        return self.settings()

    def _addresses_on(self, db: sqlite3.Connection) -> None:
        self._clear_if_pending(db)
        usagedb.set_meta(db, "store_addresses", "1")
        usagedb.set_meta(db, "addresses_cleared", "1")
        with self._state:
            # An off that arrived meanwhile wins.
            if not self._clear_pending:
                self._live.addresses = True
        self._log_settings()

    def _clear_if_pending(self, db: sqlite3.Connection) -> None:
        """addresses_cleared goes to 0 first, so a clear cut short by a
        crash is finished at the next start."""
        with self._state:
            if not self._clear_pending:
                return
        usagedb.set_meta(db, "addresses_cleared", "0")
        usagedb.set_meta(db, "store_addresses", "0")
        usagedb.clear_addresses(db)
        usagedb.set_meta(db, "addresses_cleared", "1")
        with self._state:
            self._clear_pending = False
        self._log_settings()

    def _log_settings(self) -> None:
        current = self.settings()
        event(
            log,
            logging.INFO,
            "usage_settings_changed",
            retention_days=current.retention_days,
            store_addresses=current.store_addresses,
        )

    def _call(self, command: _Command | None) -> object:
        future: Future[object] = Future()
        self._commands.put((command, future))
        try:
            return future.result(timeout=COMMAND_WAIT_S)
        except TimeoutError:
            if future.cancel():
                raise UsageUnavailable from None
        # Already running on the writer, so its outcome is the answer.
        try:
            return future.result(timeout=COMMAND_WAIT_S)
        except TimeoutError:
            raise UsageUnavailable from None

    # Panel reads.

    def summary(self, range_name: str) -> dict[str, object]:
        if range_name not in _RANGES:
            raise ValueError(f"unknown range {range_name!r}")
        return self._read(lambda db: self._summary(db, range_name))

    def devices(self) -> dict[str, object]:
        since = self._clock() - 30 * _DAY_S
        rows, never = self._read(lambda db: usagedb.devices_page(db, since))
        return {"devices": rows, "never_played": never}

    def device(self, device: str, before: float | None) -> dict[str, object] | None:
        since = self._clock() - 30 * _DAY_S
        return self._read(lambda db: usagedb.device_page(db, device, before, since))

    def _read[T](self, read: Callable[[sqlite3.Connection], T]) -> T:
        with self._state:
            available = self._available
        if not available:
            raise UsageUnavailable
        try:
            with contextlib.closing(usagedb.open_reader(self._path)) as db:
                return read(db)
        except sqlite3.Error as exc:
            self._note_down("read_failed")
            raise UsageUnavailable from exc

    def _summary(self, db: sqlite3.Connection, range_name: str) -> dict[str, object]:
        today = date.fromisoformat(day_of(self._clock(), self._zone))
        rolled = date.fromisoformat(
            usagedb.get_meta(db, "last_rollup_day", (today - timedelta(days=1)).isoformat())
        )
        if range_name in ("7d", "30d"):
            first = today - timedelta(days=int(range_name[:-1]) - 1)
            labels = [
                (first + timedelta(days=n)).isoformat() for n in range((today - first).days + 1)
            ]
            by_day = True
        else:
            if range_name == "12m":
                months = today.year * 12 + today.month - 12
                first = date(months // 12, months % 12 + 1, 1)
            else:
                oldest = usagedb.oldest_day(db)
                first = min(date.fromisoformat(oldest) if oldest else today, today).replace(day=1)
            labels = _months(first, today)
            by_day = False

        def label(day: str) -> str:
            return day if by_day else day[:7]

        seconds = dict.fromkeys(labels, 0.0)
        visits = dict.fromkeys(labels, 0)
        tops: dict[str, defaultdict[str, float]] = {
            name: defaultdict(float) for name in ("channel", "stream", "agent")
        }
        peak, peak_day = 0, None
        done = min(rolled, today)
        if first <= done:
            span = (first.isoformat(), done.isoformat())
            for day, value in usagedb.daily_series(db, "seconds_channel", *span).items():
                seconds[label(day)] += value
            for day, value in usagedb.daily_series(db, "visits", *span).items():
                visits[label(day)] += int(value)
            for name, sums in tops.items():
                for key, value in usagedb.daily_tops(db, f"seconds_{name}", *span, _ALL):
                    sums[key] += value
            peak, peak_day = usagedb.daily_peak(db, *span)
        day = max(first, rolled + timedelta(days=1))
        if day <= today:
            with self._flush:
                held, intervals, times = self._live.snapshot()
                stored, stored_times = usagedb.rows_after(
                    db, day_bounds(day.isoformat(), self._zone)[0], held
                )
            intervals += stored
            times += stored_times
            while day <= today:
                name = day.isoformat()
                totals = aggregate(intervals, times, *day_bounds(name, self._zone))
                seconds[label(name)] += sum(totals.seconds_channel.values())
                visits[label(name)] += totals.visits
                for sums, part in (
                    (tops["channel"], totals.seconds_channel),
                    (tops["stream"], totals.seconds_stream),
                    (tops["agent"], totals.seconds_agent),
                ):
                    for key, value in part.items():
                        sums[key] += value
                # On a tie the latest day is named, as daily_peak does.
                if totals.peak and totals.peak >= peak:
                    peak, peak_day = totals.peak, name
                day += timedelta(days=1)
        return {
            "zone": self._zone_name,
            "buckets": [
                {"label": name, "seconds": seconds[name], "visits": visits[name]} for name in labels
            ],
            "channels": _top(tops["channel"]),
            "streams": _top(tops["stream"]),
            "agents": _top(tops["agent"]),
            "peak": {"value": peak, "day": peak_day},
        }

    # The writer.

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(
            target=self.run_forever, args=(stop,), name="usage", daemon=True
        )
        self._thread.start()

    def stop_and_join(self, deadline: float) -> None:
        """Only joins: the caller sets stop, and the writer flushes open
        sessions before it returns."""
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))

    def work_once(self) -> None:
        """One round. While run_forever runs it is queued to the writer,
        whose thread alone may use the connection."""
        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            self._call(None)
        else:
            self._round(self._never)

    def run_forever(self, stop: threading.Event) -> None:
        self._writer = threading.current_thread()
        try:
            self._round(stop)
            while not stop.is_set():
                try:
                    item = self._commands.get(timeout=_WAIT_S)
                except queue.Empty:
                    pass
                else:
                    self._run(item, stop)
                if self._monotonic() - self._last_round >= self._flush_s:
                    self._round(stop)
            self._live.close_all()
            self._round(stop, final=True)
        finally:
            self._writer = None
            self._fail_quietly()
            while True:
                try:
                    _, future = self._commands.get_nowait()
                except queue.Empty:
                    break
                if future.set_running_or_notify_cancel():
                    future.set_exception(UsageUnavailable())

    def _run(self, item: _Item, stop: threading.Event) -> None:
        command, future = item
        if not future.set_running_or_notify_cancel():
            return
        if command is None:
            self._round(stop)
            future.set_result(None)
            return
        if self._db is None:
            self._reopen(final=False)
        db = self._db
        if db is None:
            future.set_exception(UsageUnavailable())
            return
        try:
            future.set_result(command(db))
        except (sqlite3.Error, OSError) as exc:
            self._fail("write_failed")
            error = UsageUnavailable()
            error.__cause__ = exc
            future.set_exception(error)
        except Exception as exc:  # noqa: BLE001 - the command's own error is the caller's answer
            future.set_exception(exc)

    def _round(self, stop: threading.Event, final: bool = False) -> None:
        """Flush, then the roll-up and retention when due. The last round
        before stopping only flushes."""
        self._last_round = self._monotonic()
        try:
            self._dropped += self._live.take_dropped()
            if self._db is None:
                self._reopen(final)
            db = self._db
            if db is None:
                return
            self._clear_if_pending(db)
            written = self._write(db)
            if self._dropped:
                today = day_of(self._clock(), self._zone)
                usagedb.add_dropped(db, today, "pending", self._dropped)
                self._dropped = 0
            self._note_up()
            if not final:
                self._roll(db, written.days, stop)
        except _DiskLow:
            self._fail("disk_low")
        except sqlite3.Error, OSError:
            self._fail("write_failed")
        except Exception as exc:  # noqa: BLE001 - a bug in one round must not stop the writer
            self._failed(exc)

    def _write(self, db: sqlite3.Connection) -> Written:
        with self._flush:
            visits, closed, open_rows = self._live.detach()
            try:
                if not has_room(self._free(), _ROOM):
                    raise _DiskLow
                # Addresses may have been switched off since detach: what is
                # written after the switch carries none.
                if not self._live.addresses:
                    for visit in visits:
                        visit.address = None
                    for session in (*closed, *(s for s, _ in open_rows)):
                        session.address = None
                written = usagedb.write_batch(
                    db,
                    visits,
                    [*closed, *(s for s, _ in open_rows)],
                    set(self._live.names),
                    self._zone,
                )
            except BaseException:
                self._live.requeue(visits, closed)
                raise
            # After the COMMIT only, so a rowid in memory names a stored row.
            self._live.adopt(written, open_rows, closed)
        return written

    def _roll(self, db: sqlite3.Connection, touched: set[str], stop: threading.Event) -> None:
        """Re-rolls finished days the flush touched, rolls each day after
        last_rollup_day up to yesterday, then runs retention once a day,
        only when the roll-up has reached yesterday."""
        began = self._monotonic()
        days = sessions = visits = deleted = 0
        outcome = "error"
        try:
            now = self._clock()
            today = date.fromisoformat(day_of(now, self._zone))
            yesterday = (today - timedelta(days=1)).isoformat()
            last = usagedb.get_meta(db, "last_rollup_day", yesterday)
            after = date.fromisoformat(last) + timedelta(days=1)
            following = [
                (after + timedelta(days=n)).isoformat() for n in range((today - after).days)
            ]
            for day in [*sorted(d for d in touched if d <= last), *following]:
                if stop.is_set():
                    break
                counted = usagedb.roll_up(db, day, self._zone)
                sessions, visits, days = sessions + counted[0], visits + counted[1], days + 1
                if day > last:
                    usagedb.set_meta(db, "last_rollup_day", day)
                    last = day
            if (
                last == yesterday
                and not stop.is_set()
                and usagedb.get_meta(db, "last_retention_day", "") != today.isoformat()
            ):
                with self._state:
                    keep_s = self._retention * _DAY_S
                cutoff = min(now, day_bounds(last, self._zone)[1]) - keep_s
                deleted = usagedb.retention(db, cutoff, stop)
                if not stop.is_set():
                    usagedb.set_meta(db, "last_retention_day", today.isoformat())
            outcome = "success"
        finally:
            if days or deleted or outcome == "error":
                event(
                    log,
                    logging.INFO,
                    "usage_rolled_up",
                    days=days,
                    sessions=sessions,
                    visits=visits,
                    deleted=deleted,
                    duration_ms=round((self._monotonic() - began) * 1000),
                    outcome=outcome,
                )

    def _reopen(self, final: bool) -> None:
        """Opens usage.db when no failure is waiting out RETRY_S; the last
        round before stopping tries regardless. A newer schema is final."""
        if self._schema_newer or (not final and self._monotonic() < self._retry_at):
            return
        if not has_room(self._free(), _ROOM):
            self._fail("disk_low")
            return
        try:
            db = usagedb.open_writer(self._path)
        except SchemaTooNew:
            self._schema_newer = True
            self._fail("schema_newer")
            return
        except sqlite3.Error, OSError:
            self._fail("open_failed")
            return
        self._db = db
        try:
            self._load(db)
            # Before any row: an address-off from the outage, or one a
            # crash cut short.
            self._clear_if_pending(db)
        except sqlite3.Error, OSError:
            self._fail("open_failed")

    def _load(self, db: sqlite3.Connection) -> None:
        if not usagedb.get_meta(db, "zone", ""):
            usagedb.set_meta(db, "zone", self._zone_name)
        if not usagedb.get_meta(db, "last_rollup_day", ""):
            today = date.fromisoformat(day_of(self._clock(), self._zone))
            usagedb.set_meta(db, "last_rollup_day", (today - timedelta(days=1)).isoformat())
        retention = _retention_of(usagedb.get_meta(db, "retention_days", ""))
        stored = usagedb.get_meta(db, "store_addresses", "0") == "1"
        unfinished = usagedb.get_meta(db, "addresses_cleared", "1") == "0"
        self._live.names = usagedb.named_devices(db)
        # Memory is newer once a session has ended since start-up.
        if self._live.last_ended() is None:
            self._live.set_last_ended(usagedb.last_ended(db))
        with self._state:
            self._retention = retention
            self._clear_pending = self._clear_pending or unfinished
            self._live.addresses = stored and not self._clear_pending
            self._available = True

    def _fail(self, reason: str) -> None:
        self._fail_quietly()
        self._retry_at = self._monotonic() + RETRY_S
        self._note_down(reason)

    def _fail_quietly(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            with contextlib.suppress(sqlite3.Error):
                db.close()
        with self._state:
            self._available = False
            # Addresses count as off while usage.db cannot be read.
            self._live.addresses = False

    def _note_down(self, reason: str) -> None:
        with self._state:
            if self._logged_down:
                return
            self._logged_down = True
        event(log, logging.WARNING, "usage_store_unavailable", reason=reason)

    def _note_up(self) -> None:
        with self._state:
            if not self._logged_down:
                return
            self._logged_down = False
        event(log, logging.INFO, "usage_store_recovered")
