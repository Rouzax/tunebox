"""Station health: whether each enabled station's streams and feed work.
The rules here are pure; HealthChecks runs the checks and keeps their
latest results in the cover store."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from datetime import time as daytime
from typing import Any, Literal, Protocol

from tunebox.channel import Liveness
from tunebox.config import ChannelConfig, SourceConfig
from tunebox.logs import bound, event
from tunebox.netguard import Probe
from tunebox.store import HealthRow, Store, transition
from tunebox.streamcheck import check_feed, check_stream
from tunebox.tracks import RawEntry

log = logging.getLogger(__name__)

ITEM_PAUSE_S = 3.0
BUTTON_GAP_S = 300.0
# A poller failing this long is broken; a shorter spell is a hiccup it rides out.
LIVE_BROKEN_S = 600
# No new song for this long while polls succeed: the feed is most likely stuck.
STUCK_S = 7200
MAX_HEALTH_ITEMS = 300
START_DELAY_S = 300.0
STARTUP_STALE_S = 86_400.0
# A run started this recently makes a restart skip the start-up run, so a
# crash loop never checks every station on each start.
STARTUP_QUIET_S = 21_600.0
NETWORK_RETRY_S = 1800.0
# Between the first pass and the second attempts: a short outage of
# tunebox's own connection during the first pass is over by then.
RETRY_DELAY_S = 600.0
NETWORK_RETRIES = 3
WAIT_S = 60.0
# Before the morning drive, and outside the 02:00-03:00 hours in which most
# zones change their clocks, so a daylight-saving day still has exactly one
# 04:30 in those zones.
DAILY_AT = daytime(4, 30)

Status = Literal["ok", "warning", "broken"]
Trigger = Literal["daily", "startup", "button", "retry"]


@dataclass(frozen=True)
class Reason:
    code: str
    item: str | None


@dataclass(frozen=True)
class Judgement:
    status: Status
    reasons: tuple[Reason, ...]


def fingerprint(source: SourceConfig) -> str:
    """The feed a result belongs to: a changed setting is another feed."""
    text = json.dumps([source.type, sorted(source.settings.items())], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def targets(config: ChannelConfig) -> list[tuple[str, str]]:
    return [(f"stream:{s.name}", s.url) for s in config.streams] + [
        ("feed", fingerprint(config.source))
    ]


def _daily_after(now: float, zone: tzinfo) -> float:
    """The next DAILY_AT strictly after now: the loop keeps the time it
    planned and runs once the wall clock passes it, so a wake at 04:30:00.5
    still runs that day's check."""
    day = datetime.fromtimestamp(now, zone).date()
    today = datetime.combine(day, DAILY_AT, zone).timestamp()
    if today > now:
        return today
    return datetime.combine(day + timedelta(days=1), DAILY_AT, zone).timestamp()


def next_run(
    now: float,
    last_started: float | None,
    last_finished: float | None,
    process_start: float,
    zone: tzinfo,
    start_delay_s: float = START_DELAY_S,
) -> tuple[float, Literal["daily", "startup"]]:
    stale = last_finished is None or now - last_finished > STARTUP_STALE_S
    quiet = last_started is None or now - last_started > STARTUP_QUIET_S
    if stale and quiet:
        return max(now, process_start + start_delay_s), "startup"
    return _daily_after(now, zone), "daily"


def _newer(stored_ms: int | None, kept: float | None) -> float | None:
    """The later of a stored time (ms) and one kept in memory (s)."""
    stored = stored_ms / 1000 if stored_ms is not None else None
    if stored is None or kept is None:
        return kept if stored is None else stored
    return max(stored, kept)


def judge(
    config: ChannelConfig, rows: Mapping[str, HealthRow], live: Liveness, now_ms: int
) -> Judgement:
    """The one status rule. rows are this station's, by item; a row checked
    against another address or feed than the current one is not checked
    yet, never a failure."""
    broken: list[Reason] = []
    warning: list[Reason] = []
    info: list[Reason] = []
    current: dict[str, HealthRow] = {}
    for item, target in targets(config):
        row = rows.get(item)
        if row is None or row.target != target:
            info.append(Reason("not_checked", item))
        else:
            current[item] = row
    streams = [item for item, _ in targets(config) if item != "feed"]
    failing = [item for item in streams if item in current and not current[item].ok]
    if failing and len(failing) == len(streams):
        broken.append(Reason("streams_failing", None))
    else:
        warning.extend(Reason("stream_failing", item) for item in failing)
    for item in streams:
        row = current.get(item)
        if row is not None and row.ok and row.reason == "plain_http":
            warning.append(Reason("plain_http", item))
    feed = current.get("feed")
    if feed is not None and not feed.ok:
        if live.last_poll_ok is not None and live.last_poll_ok > feed.checked_at:
            warning.append(Reason("feed_check_failed_polls_ok", "feed"))
        else:
            broken.append(Reason("feed_failing", "feed"))
    elif feed is not None and feed.reason == "no_songs":
        warning.append(Reason("no_songs", "feed"))
    if live.active and live.stale_since is not None:
        if now_ms - live.stale_since > LIVE_BROKEN_S * 1000:
            broken.append(Reason("poll_failing", None))
    elif (
        live.active
        and live.polled_since_wake
        and live.last_change is not None
        and now_ms - live.last_change > STUCK_S * 1000
    ):
        warning.append(Reason("stuck", None))
    if broken:
        return Judgement("broken", tuple(broken))
    if warning:
        return Judgement("warning", tuple(warning))
    return Judgement("ok", tuple(info))


# Failure codes that mean the connection never got anywhere: all of them at
# once say more about tunebox's own network than about the stations.
NETWORK_CODES = frozenset({"unreachable", "timeout", "connect_failed"})
_ORDER = {"broken": 0, "warning": 1, "ok": 2}

Ask = Literal["started", "running", "too_soon", "stopped"]


class LiveChannel(Protocol):
    @property
    def config(self) -> ChannelConfig: ...

    def liveness(self) -> Liveness: ...


@dataclass(frozen=True)
class Asked:
    outcome: Ask
    retry_s: int = 0


@dataclass(frozen=True)
class RunResult:
    outcome: Literal["ok", "network_down", "stopped", "error"]
    items: int
    failing: int
    skipped: int
    write_failures: int


@dataclass(frozen=True)
class _Item:
    channel: str
    item: str
    target: str
    url: str | None
    source: SourceConfig | None


@dataclass(frozen=True)
class _Result:
    ok: bool
    reason: str | None
    codec: str | None = None
    kbps: int | None = None


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def _network_down(failed: list[_Result], items: int) -> bool:
    """Everything failing to connect at once is tunebox's own network, not
    every station breaking together."""
    lost = sum(1 for r in failed if r.reason in NETWORK_CODES)
    return items > 0 and (lost == items or (lost > 3 and lost >= 0.9 * items))


def _planned(channels: Mapping[str, LiveChannel]) -> list[_Item]:
    planned: list[_Item] = []
    for cid, channel in channels.items():
        config = channel.config
        urls = {f"stream:{s.name}": s.url for s in config.streams}
        for item, target in targets(config):
            if item == "feed":
                planned.append(_Item(cid, item, target, None, config.source))
            else:
                planned.append(_Item(cid, item, target, urls[item], None))
    return planned


def _item_view(
    config: ChannelConfig, item: str, target: str, row: HealthRow | None
) -> dict[str, Any]:
    if item == "feed":
        kind, name = "feed", config.source.type
    else:
        kind, name = "stream", item.removeprefix("stream:")
    view: dict[str, Any] = {
        "item": item,
        "name": name,
        "kind": kind,
        "checked_at": None,
        "ok": None,
        "reason": None,
        "codec": None,
        "kbps": None,
        "failing_since": None,
    }
    # A result for another address or feed says nothing about this one.
    if row is not None and row.target == target:
        view.update(
            checked_at=row.checked_at,
            ok=row.ok,
            reason=row.reason,
            codec=row.codec,
            kbps=row.kbps,
            failing_since=row.failing_since,
        )
    return view


class HealthChecks:
    """One worker thread runs the checks, one item at a time; request
    threads only read results and ask for a run."""

    def __init__(
        self,
        probe: Callable[[str], Probe],
        feed: Callable[[SourceConfig], list[RawEntry]],
        store: Store,
        channels: Callable[[], Mapping[str, LiveChannel]],
        zone: tzinfo,
        *,
        wall: Callable[[], float] = time.time,
        pause_s: float = ITEM_PAUSE_S,
        start_delay_s: float = START_DELAY_S,
        retry_wait_s: float = WAIT_S,
        retry_delay_s: float = RETRY_DELAY_S,
    ) -> None:
        self._probe = probe
        self._feed = feed
        self._store = store
        self._channels = channels
        self._zone = zone
        self._wall = wall
        self._pause_s = pause_s
        self._start_delay_s = start_delay_s
        self._retry_wait_s = retry_wait_s
        self._retry_delay_s = retry_delay_s
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # A press stays set until its run begins, so the gap between the
        # press and the run still reads as running.
        self._button = False
        self._running = False
        self._progress: dict[str, Any] | None = None
        self._last_end: float | None = None
        # The run row's times as this process wrote them: when the store
        # cannot write the row, the plan still sees this process's runs and
        # does not start the next one at once.
        self._last_started: float | None = None
        self._last_finished: float | None = None
        self._retries = 0
        self._thread: threading.Thread | None = None
        self._process_start = wall()

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(target=self._loop, args=(stop,), name="health", daemon=True)
        self._thread.start()

    def stop_and_join(self, deadline: float) -> None:
        """deadline is on time.monotonic(); the caller has set the stop event."""
        self._wake.set()
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request(self) -> Asked:
        with self._lock:
            if not self.alive():
                return Asked("stopped")
            if self._running or self._button:
                return Asked("running")
            if self._last_end is not None:
                left = BUTTON_GAP_S - (self._wall() - self._last_end)
                if left > 0:
                    return Asked("too_soon", math.ceil(left))
            self._button = True
        self._wake.set()
        return Asked("started")

    def _loop(self, stop: threading.Event) -> None:
        due: tuple[float, Trigger] | None = None
        while not stop.is_set():
            # Scheduling sits inside the handler too: a store that cannot be
            # read must not end the thread.
            try:
                with self._lock:
                    pressed = self._button
                if due is None:
                    due = self._plan(None)
                if pressed or self._wall() >= due[0]:
                    trigger: Trigger = "button" if pressed else due[1]
                    with bound("health"):
                        result = self.run_once(trigger, stop)
                    # After stop the store may already be closed.
                    if stop.is_set():
                        return
                    due = self._plan(result.outcome)
                    continue
                # Woken at least every WAIT_S to read the wall clock again, so
                # a suspended host or a clock change does not move the plan.
                self._wake.wait(min(WAIT_S, max(0.0, due[0] - self._wall())))
                # A press after this clear is still read from _button above.
                self._wake.clear()
            except Exception:
                log.error("health_check_crashed", exc_info=True)
                due = None
                stop.wait(self._retry_wait_s)

    def _plan(self, outcome: str | None) -> tuple[float, Trigger]:
        run = self._store.health_run()
        now = self._wall()
        with self._lock:
            started, finished = self._last_started, self._last_finished
        when, kind = next_run(
            now,
            _newer(run.last_started_at, started),
            _newer(run.last_finished_at, finished),
            self._process_start,
            self._zone,
            self._start_delay_s,
        )
        # A network-down run records its finish, so only this retry, not the
        # start-up rule, checks again soon.
        if outcome == "network_down" and self._retries < NETWORK_RETRIES:
            self._retries += 1
            retry = now + NETWORK_RETRY_S
            if retry < when:
                return retry, "retry"
        elif outcome != "network_down":
            self._retries = 0
        return when, kind

    def run_once(self, trigger: Trigger, stop: threading.Event) -> RunResult:
        started = self._wall()
        clock = time.monotonic()
        with self._lock:
            self._running = True
            self._button = False
            self._progress = {
                "done": 0,
                "total": 0,
                "started_at": _ms(started),
                "trigger": trigger,
            }
        result = RunResult("error", 0, 0, 0, 0)
        try:
            result = self._run(stop, started, clock)
            return result
        finally:
            with self._lock:
                self._running = False
                self._progress = None
                self._last_end = self._wall()
            if result.outcome != "stopped":
                ok = result.outcome == "ok" and not result.write_failures
                event(
                    log,
                    logging.INFO if ok else logging.WARNING,
                    "health_check_done",
                    trigger=trigger,
                    items=result.items,
                    failing=result.failing,
                    skipped=result.skipped,
                    write_failures=result.write_failures,
                    outcome=result.outcome,
                    duration_ms=round((time.monotonic() - clock) * 1000),
                )

    def _run(self, stop: threading.Event, started: float, clock: float) -> RunResult:
        items = 0
        try:
            planned = _planned(self._channels())
            skipped = max(0, len(planned) - MAX_HEALTH_ITEMS)
            planned = planned[:MAX_HEALTH_ITEMS]
            items = len(planned)
            stopped = RunResult("stopped", items, 0, skipped, 0)
            if stop.is_set():
                return stopped
            with self._lock:
                self._last_started = started
            self._store.health_run_started(_ms(started))
            before = {(r.channel, r.item): r for r in self._store.health_rows() or []}
            self._set_progress(total=items)
            # By position: an item holds its source's settings dict, so it
            # cannot be a key.
            first_at: list[int] = []
            results: list[_Result] = []
            crashed: set[tuple[str, str]] = set()
            for index, item in enumerate(planned):
                if self._paused(stop, index):
                    return stopped
                first_at.append(_ms(self._wall()))
                results.append(self._check(item, crashed))
                self._set_progress(done=index + 1)
            last_at = list(first_at)
            # A second attempt after RETRY_DELAY_S, so one dropped connection
            # or a short outage of tunebox's own network is no failure.
            failed = [index for index, result in enumerate(results) if not result.ok]
            if failed and stop.wait(self._retry_delay_s):
                return stopped
            self._set_progress(total=items + len(failed))
            for done, index in enumerate(failed, start=items):
                if self._paused(stop, done):
                    return stopped
                last_at[index] = _ms(self._wall())
                results[index] = self._check(planned[index], crashed)
                self._set_progress(done=done + 1)
            failing = [result for result in results if not result.ok]
            if _network_down(failing, items):
                if stop.is_set():
                    return stopped
                self._finished(clock, items, "network_down")
                event(
                    log, logging.WARNING, "health_network_down", items=items, failing=len(failing)
                )
                return RunResult("network_down", items, len(failing), skipped, 0)
            write_failures = 0
            for index, item in enumerate(planned):
                if stop.is_set():
                    return stopped
                if not self._write(item, results[index], first_at[index], last_at[index], before):
                    write_failures += 1
            if stop.is_set():
                return stopped
            keep = {
                (cid, item)
                for cid, channel in self._channels().items()
                for item, _ in targets(channel.config)
            }
            self._store.health_prune(keep)
            self._finished(clock, items, "ok")
            return RunResult("ok", items, len(failing), skipped, write_failures)
        except Exception:
            # After stop the store may already be closed.
            if not stop.is_set():
                self._finished(clock, items, "error")
            raise

    def _paused(self, stop: threading.Event, index: int) -> bool:
        """The pause before an item; True when the run must end."""
        if index:
            return stop.wait(self._pause_s)
        return stop.is_set()

    def _set_progress(self, **values: int) -> None:
        with self._lock:
            assert self._progress is not None, "progress exists while a run does"
            self._progress.update(values)

    def _finished(self, clock: float, items: int, outcome: str) -> None:
        duration = round((time.monotonic() - clock) * 1000)
        finished = self._wall()
        with self._lock:
            self._last_finished = finished
        self._store.health_run_finished(_ms(finished), duration, items, outcome)

    def _write(
        self,
        item: _Item,
        result: _Result,
        first_at: int,
        last_at: int,
        before: Mapping[tuple[str, str], HealthRow],
    ) -> bool:
        """Store one result and log its change; False when the write failed.
        A failed write still logs the change against the rows read at the
        start of the run."""
        key = (item.channel, item.item)
        row = HealthRow(
            item.channel,
            item.item,
            item.target,
            last_at,
            result.ok,
            result.reason,
            result.codec,
            result.kbps,
            None if result.ok else first_at,
        )
        written = self._store.health_write(row)
        stored = written.change != "write_failed"
        if stored:
            change, since = written.change, written.since
        else:
            change, _ = transition(before.get(key), row)
            since = before[key].failing_since if key in before else None
        if change == "failing":
            event(
                log,
                logging.WARNING,
                "health_failing",
                channel=item.channel,
                item=item.item,
                reason=result.reason,
            )
        elif change == "recovered":
            failing_s = round((row.checked_at - since) / 1000) if since is not None else None
            event(
                log,
                logging.INFO,
                "health_recovered",
                channel=item.channel,
                item=item.item,
                failing_s=failing_s,
            )
        return stored

    def _check(self, item: _Item, crashed: set[tuple[str, str]]) -> _Result:
        answer: dict[str, Any]
        crash: Exception | None
        try:
            if item.url is not None:
                answer = check_stream(self._probe, item.url)
                crash = None
            else:
                assert item.source is not None
                answer, crash = check_feed(self._feed, item.source)
        except Exception as error:  # noqa: BLE001 (a bug in a check costs one item, not the run)
            answer, crash = {"ok": False, "code": "unreadable"}, error
        if crash is not None and (item.channel, item.item) not in crashed:
            crashed.add((item.channel, item.item))
            event(
                log,
                logging.WARNING,
                "health_item_crashed",
                channel=item.channel,
                item=item.item,
                error=type(crash).__name__,
            )
        if answer.get("code") == "no_songs":
            return _Result(True, "no_songs")
        return _Result(
            bool(answer["ok"]), answer.get("code"), answer.get("codec"), answer.get("kbps")
        )

    def _stations(self) -> list[dict[str, Any]] | None:
        rows = self._store.health_rows()
        if rows is None:
            return None
        by_channel: dict[str, dict[str, HealthRow]] = {}
        for row in rows:
            by_channel.setdefault(row.channel, {})[row.item] = row
        now = _ms(self._wall())
        stations: list[dict[str, Any]] = []
        for cid, channel in self._channels().items():
            config = channel.config
            own = by_channel.get(cid, {})
            live = channel.liveness()
            judged = judge(config, own, live, now)
            stations.append(
                {
                    "id": cid,
                    "name": config.name,
                    "status": judged.status,
                    "reasons": [{"code": r.code, "item": r.item} for r in judged.reasons],
                    "items": [
                        _item_view(config, item, target, own.get(item))
                        for item, target in targets(config)
                    ],
                    "live": {
                        "active": live.active,
                        "last_poll_ok": live.last_poll_ok,
                        "stale_since": live.stale_since,
                        "last_change": live.last_change,
                    },
                }
            )
        # sort is stable: listing order holds within each status.
        stations.sort(key=lambda s: _ORDER[s["status"]])
        return stations

    def _status(self) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            progress = dict(self._progress) if self._progress is not None else None
            return self._running or self._button, progress

    def page(self) -> dict[str, Any] | None:
        """None when the rows cannot be read."""
        stations = self._stations()
        if stations is None:
            return None
        run = self._store.health_run()
        return {
            "stations": stations,
            "run": {
                "last_started_at": run.last_started_at,
                "last_finished_at": run.last_finished_at,
                "duration_ms": run.duration_ms,
                "items": run.items,
                "outcome": run.outcome,
            },
            "progress": self._status()[1],
        }

    def count(self) -> dict[str, Any]:
        stations = self._stations()
        running, progress = self._status()
        broken = None if stations is None else sum(1 for s in stations if s["status"] == "broken")
        return {"broken": broken, "running": running, "progress": progress}
