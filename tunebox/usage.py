"""Usage statistics for the admin panel. Request threads only touch memory
(usagelive.Live); one writer thread owns usage.db. Exists only while the
panel is enabled."""

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
    RETENTION_DEFAULT,
    RETENTION_MAX,
    RETENTION_MIN,
    RETRY_S,
    aggregate,
    day_bounds,
    day_of,
)
from tunebox.usagedb import Written
from tunebox.usagelive import Live

log = logging.getLogger(__name__)


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
        # Already running on the writer: waited for to the end, so that
        # UsageUnavailable always means nothing changed.
        return future.result()

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
        # Before the thread runs, so a work_once right after start() is
        # queued and never opens a connection on the caller's thread.
        self._writer = self._thread
        self._thread.start()

    def stop_and_join(self, deadline: float) -> None:
        """Only joins: the caller sets stop, and the writer flushes open
        sessions before it returns."""
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))

    def work_once(self) -> None:
        """One round. Once a writer has started it is queued to it, whose
        thread alone may use the connection; after the writer has ended it
        answers UsageUnavailable instead of opening one here."""
        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            self._call(None)
        else:
            self._round(self._never)

    def run_forever(self, stop: threading.Event) -> None:
        if self._writer is None:
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
            # _writer stays set: a later work_once must not reopen here.
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
                sessions = [*closed, *(s for s, _ in open_rows)]
                # Addresses may have been switched off since detach: what is
                # written after the switch carries none.
                self._live.strip_if_off(visits, sessions)
                written = usagedb.write_batch(
                    db, visits, sessions, set(self._live.names), self._zone
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
