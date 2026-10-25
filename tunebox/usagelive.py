"""Live usage state for the admin panel: open listening sessions, pending
visits and closed sessions waiting for the writer, and the last session
that ended. Request threads touch only this; usage.py owns the writer."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import cast

from tunebox.usagecore import IDLE_S, MAX_LIVE, MAX_PENDING, Interval, interval_end
from tunebox.usagedb import SessionRow, VisitRow, Written

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
            if not self._addresses:
                self._strip(all_visits, all_closed)
            self._visits, self._closed = all_visits, all_closed
            self._dropped += max(excess, 0)
            return max(excess, 0)

    def strip_if_off(self, visits: list[VisitRow], sessions: list[SessionRow]) -> None:
        """For rows already detached: checked and stripped under the lock,
        so an address-off cannot land between the two."""
        with self._lock:
            if not self._addresses:
                self._strip(visits, sessions)

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

    @staticmethod
    def _strip(visits: list[VisitRow], sessions: list[SessionRow]) -> None:
        for visit in visits:
            visit.address = None
        for session in sessions:
            session.address = None

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
