import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from datetime import UTC
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from tunebox import usage as usage_module
from tunebox import usagedb, usagelive
from tunebox.usage import Usage, UsageSettings, UsageUnavailable
from tunebox.usagecore import COMMAND_WAIT_S, RETRY_S
from tunebox.usagedb import SessionRow, VisitRow
from tunebox.usagelive import Live

ID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
AGENT = "Chrome 120, Linux"
ADDRESS = "198.51.100.7"
DAY0 = 1_792_800_000.0  # 2026-10-24 00:00 UTC
REAL_WRITE = usagedb.write_batch


class Clock:
    def __init__(self) -> None:
        self.now = DAY0 + 43_200  # 2026-10-24 12:00

    def __call__(self) -> float:
        return self.now


class Free:
    def __init__(self) -> None:
        self.value: int | None = None

    def __call__(self) -> int | None:
        return self.value


@pytest.fixture(autouse=True)
def quick_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer sees a set stop within this, so each test ends quickly."""
    monkeypatch.setattr(usage_module, "_WAIT_S", 0.01)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def free() -> Free:
    return Free()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "usage.db"


def make(path: Path, clock: Clock, free: Free, flush_s: float = 1e9) -> Usage:
    # A flush interval no test reaches, so only work_once runs a round.
    return Usage(
        path, zone=UTC, zone_name="UTC", free=free, clock=clock, monotonic=clock, flush_s=flush_s
    )


@contextlib.contextmanager
def started(usage: Usage) -> Generator[Usage]:
    stop = threading.Event()
    usage.start(stop)
    try:
        yield usage
    finally:
        stop.set()
        usage.stop_and_join(time.monotonic() + 10)


@pytest.fixture
def usage(path: Path, clock: Clock, free: Free) -> Generator[Usage]:
    with started(make(path, clock, free)) as running:
        yield running


def query(path: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    with contextlib.closing(sqlite3.connect(path)) as db:
        return [tuple(r) for r in db.execute(sql, args)]


def execute(path: Path, sql: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(sql)


def meta(path: Path, key: str) -> object:
    rows = query(path, "SELECT value FROM meta WHERE key = ?", key)
    return rows[0][0] if rows else None


def addresses(path: Path) -> list[object]:
    return [
        r[0]
        for r in query(path, "SELECT address FROM visits UNION ALL SELECT address FROM listens")
    ]


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], r.__dict__["fields"])
        for r in caplog.records
        if r.getMessage() == name
    ]


def wait_for(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not check() and time.monotonic() < deadline:
        time.sleep(0.01)


def broken_write(*_: object) -> usagedb.Written:
    raise sqlite3.OperationalError("disk I/O error")


def sessions(usage: Usage) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], usage.live()["sessions"])


def test_a_round_writes_visits_and_sessions_and_resolves_the_device(
    usage: Usage, clock: Clock, path: Path
) -> None:
    usage.visit(ID, AGENT, None)
    usage.work_once()
    usage.heard(ID, "owr", "Default", AGENT, None)
    clock.now += 5
    usage.work_once()
    found = usage.devices()
    assert [d["id"] for d in cast(list[dict[str, object]], found["devices"])] == [ID]
    assert found["never_played"] == 0
    assert query(path, "SELECT device, channel FROM listens") == [(ID, "owr")]


def test_the_writer_runs_a_round_by_itself_after_the_flush_interval(
    path: Path, clock: Clock, free: Free
) -> None:
    with started(make(path, clock, free, flush_s=60)) as running:
        running.work_once()
        running.visit(ID, AGENT, None)
        clock.now += 61
        wait_for(lambda: bool(query(path, "SELECT 1 FROM visits")))
        assert query(path, "SELECT device FROM visits") == [(ID,)]


def test_a_rename_shows_in_live_at_once(usage: Usage) -> None:
    usage.visit(ID, AGENT, None)
    usage.work_once()
    usage.heard(ID, "owr", "Default", AGENT, None)
    assert usage.rename(ID, "Car") is True
    assert sessions(usage)[0]["label"] == "Car"
    assert usage.rename(OTHER, "Phone") is False
    assert usage.rename(ID, None) is True
    assert sessions(usage)[0]["label"] == f"Unnamed device · {AGENT}"


def test_delete_during_a_live_session_writes_nothing_back(
    usage: Usage, clock: Clock, path: Path
) -> None:
    usage.visit(ID, AGENT, None)
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.work_once()
    assert usage.rename(ID, "Car") is True
    assert usage.delete(ID) is True
    assert sessions(usage) == []
    clock.now += 5
    usage.heard(ID, "owr", "Default", AGENT, None)  # the page is still open
    assert sessions(usage)[0]["label"] != "Car"
    usage.work_once()
    assert query(path, "SELECT count(*) FROM listens WHERE device = ?", ID) == [(0,)]
    assert query(path, "SELECT count(*) FROM devices") == [(0,)]
    assert query(path, "SELECT device, started_at FROM listens") == [(None, clock.now)]
    assert usage.delete(ID) is False


def test_address_off_with_an_open_session_and_a_pending_visit_leaves_no_address(
    usage: Usage, clock: Clock, path: Path
) -> None:
    assert usage.set_addresses(True) == UsageSettings(365, True, True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.heard(ID, "owr", "Default", AGENT, ADDRESS)
    usage.work_once()
    assert set(addresses(path)) == {ADDRESS}
    assert sessions(usage)[0]["address"] == ADDRESS
    usage.visit(ID, AGENT, ADDRESS)  # pending at switch-off
    assert usage.set_addresses(False) == UsageSettings(365, False, True)
    clock.now += 5
    usage.heard(ID, "owr", "Default", AGENT, ADDRESS)
    usage.work_once()
    assert len(addresses(path)) == 3 and set(addresses(path)) == {None}
    assert meta(path, "store_addresses") == "0" and meta(path, "addresses_cleared") == "1"
    assert "address" not in sessions(usage)[0]
    assert usage.live()["addresses"] is False


def test_address_off_after_the_writer_detached_its_batch_strips_that_batch(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.heard(ID, "owr", "Default", AGENT, ADDRESS)
    original = Live.detach

    def detach_then_switch_off(
        self: Live,
    ) -> tuple[list[VisitRow], list[SessionRow], list[tuple[SessionRow, float]]]:
        batch = original(self)
        self.addresses = False
        return batch

    monkeypatch.setattr(Live, "detach", detach_then_switch_off)
    usage.work_once()
    assert len(addresses(path)) == 2 and set(addresses(path)) == {None}


def test_address_off_during_a_write_is_cleared_before_the_answer(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    entered, go = threading.Event(), threading.Event()

    def slow_write(*args: object) -> usagedb.Written:
        entered.set()
        go.wait(5)
        return REAL_WRITE(*args)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(usagedb, "write_batch", slow_write)
    flusher = threading.Thread(target=usage.work_once)
    flusher.start()
    assert entered.wait(5)
    answers: list[UsageSettings] = []
    switch = threading.Thread(target=lambda: answers.append(usage.set_addresses(False)))
    switch.start()
    wait_for(lambda: usage.live()["addresses"] is False)
    go.set()
    flusher.join(5)
    switch.join(5)
    assert answers == [UsageSettings(365, False, True)]
    assert addresses(path) == [None]


def test_address_off_during_an_outage_is_not_saved_yet_and_is_cleared_first_on_recovery(
    usage: Usage, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.work_once()
    monkeypatch.setattr(usagedb, "write_batch", broken_write)
    usage.visit(ID, AGENT, ADDRESS)
    usage.work_once()
    assert usage.live()["saved"] is False
    assert usage.settings().store_addresses is False  # off while the store is unavailable
    assert usage.set_addresses(False) == UsageSettings(365, False, False)
    calls: list[str] = []
    real_clear = usagedb.clear_addresses

    def spy_write(*args: object) -> usagedb.Written:
        calls.append("write")
        return REAL_WRITE(*args)  # pyright: ignore[reportArgumentType]

    def spy_clear(db: sqlite3.Connection) -> None:
        calls.append("clear")
        real_clear(db)

    monkeypatch.setattr(usagedb, "write_batch", spy_write)
    monkeypatch.setattr(usagedb, "clear_addresses", spy_clear)
    clock.now += RETRY_S
    usage.work_once()
    assert calls[:2] == ["clear", "write"]
    assert addresses(path) == [None, None]
    assert meta(path, "store_addresses") == "0"
    assert usage.settings() == UsageSettings(365, False, True)


def test_a_command_that_reopens_the_store_clears_addresses_first(
    usage: Usage, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.work_once()
    monkeypatch.setattr(usagedb, "write_batch", broken_write)
    usage.work_once()
    usage.set_addresses(False)
    calls: list[str] = []
    real_clear, real_rename = usagedb.clear_addresses, usagedb.rename_device

    def spy_clear(db: sqlite3.Connection) -> None:
        calls.append("clear")
        real_clear(db)

    def spy_rename(db: sqlite3.Connection, device: str, name: str | None) -> bool:
        calls.append("rename")
        return real_rename(db, device, name)

    monkeypatch.setattr(usagedb, "clear_addresses", spy_clear)
    monkeypatch.setattr(usagedb, "rename_device", spy_rename)
    clock.now += RETRY_S
    assert usage.rename(ID, "Car") is True
    assert calls == ["clear", "rename"]
    assert addresses(path) == [None]


def test_an_address_off_that_times_out_is_cleared_by_the_next_round(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.work_once()
    entered, go = threading.Event(), threading.Event()

    def slow_write(*args: object) -> usagedb.Written:
        entered.set()
        go.wait(5)
        return REAL_WRITE(*args)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(usagedb, "write_batch", slow_write)
    flusher = threading.Thread(target=usage.work_once)
    flusher.start()
    assert entered.wait(5)
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", 0.05)
    assert usage.set_addresses(False) == UsageSettings(365, False, False)
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", COMMAND_WAIT_S)
    go.set()
    flusher.join(5)
    assert addresses(path) == [ADDRESS]  # the writer was busy: not cleared yet
    usage.work_once()
    assert addresses(path) == [None]
    assert usage.settings() == UsageSettings(365, False, True)


def test_addresses_count_as_off_until_the_store_is_read(
    path: Path, clock: Clock, free: Free
) -> None:
    with started(make(path, clock, free)) as first:
        first.set_addresses(True)
    free.value = 0
    with started(make(path, clock, free)) as second:
        second.visit(ID, AGENT, ADDRESS)
        assert second.settings() == UsageSettings(365, False, False)
        free.value = None
        clock.now += RETRY_S
        second.work_once()
        assert second.settings() == UsageSettings(365, True, True)
    assert addresses(path) == [None]


def test_an_open_session_across_two_flushes_counts_once_in_the_summary(
    usage: Usage, clock: Clock
) -> None:
    usage.heard(None, "owr", "Default", AGENT, None)
    for step in range(30):
        clock.now += 10
        usage.heard(None, "owr", "Default", AGENT, None)
        if step in (9, 29):
            usage.work_once()
    today = cast(list[dict[str, object]], usage.summary("7d")["buckets"])[-1]
    assert today == {"label": "2026-10-24", "seconds": pytest.approx(300), "visits": 0}
    clock.now += 120  # idle: closed, stored and gone from memory
    usage.work_once()
    today = cast(list[dict[str, object]], usage.summary("7d")["buckets"])[-1]
    assert today["seconds"] == pytest.approx(300)


def test_a_failed_flush_adopts_no_rowid_and_overwrites_nothing(
    usage: Usage, clock: Clock, path: Path
) -> None:
    usage.work_once()
    start = clock.now
    # Fails at the end of the batch, after the session's INSERT ran.
    execute(
        path,
        "CREATE TRIGGER fail_insert AFTER INSERT ON meta BEGIN SELECT RAISE(ABORT, 'x'); END;"
        "CREATE TRIGGER fail_update AFTER UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'x'); END;",
    )
    usage.heard(None, "owr", "Default", AGENT, None)
    usage.work_once()
    assert usage.live()["saved"] is False
    execute(path, "DROP TRIGGER fail_insert; DROP TRIGGER fail_update;")
    clock.now += RETRY_S / 2
    usage.heard(None, "owr", "Default", AGENT, None)
    clock.now += RETRY_S / 2
    usage.heard(None, "owr", "Default", AGENT, None)
    usage.heard(None, "qmusic", "Default", AGENT, None)
    usage.work_once()
    clock.now += 10
    usage.heard(None, "owr", "Default", AGENT, None)
    clock.now += 5
    usage.heard(None, "qmusic", "Default", AGENT, None)
    usage.work_once()
    rows = query(path, "SELECT channel, started_at, last_seen FROM listens ORDER BY channel")
    assert rows == [
        ("owr", start, start + RETRY_S + 10),
        ("qmusic", start + RETRY_S, start + RETRY_S + 15),
    ]


def test_the_roll_up_follows_state_after_a_failed_flush(
    usage: Usage, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.heard(None, "owr", "Default", AGENT, None)
    usage.work_once()
    clock.now = DAY0 + 86_400 + 100
    monkeypatch.setattr(usagedb, "write_batch", broken_write)
    usage.work_once()
    assert query(path, "SELECT count(*) FROM daily WHERE day = '2026-10-24'") == [(0,)]
    assert meta(path, "last_rollup_day") == "2026-10-23"
    monkeypatch.setattr(usagedb, "write_batch", REAL_WRITE)
    clock.now += RETRY_S
    usage.work_once()
    assert meta(path, "last_rollup_day") == "2026-10-24"
    assert query(
        path, "SELECT value FROM daily WHERE day = '2026-10-24' AND metric = 'seconds_channel'"
    ) == [(10.0,)]


def test_a_late_row_re_rolls_its_day(usage: Usage, clock: Clock, path: Path) -> None:
    usage.work_once()
    clock.now = DAY0 + 86_400 + 100
    usage.work_once()
    visits = "SELECT value FROM daily WHERE day = '2026-10-24' AND metric = 'visits'"
    assert query(path, visits) == [(0.0,)]
    clock.now = DAY0 + 50_000  # a visit buffered from the day already rolled up
    usage.visit(ID, AGENT, None)
    clock.now = DAY0 + 86_400 + 200
    usage.work_once()
    assert query(path, visits) == [(1.0,)]


def test_the_roll_up_is_logged_with_its_counts(
    usage: Usage, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    usage.visit(ID, AGENT, None)
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.work_once()
    clock.now = DAY0 + 86_400 + 100
    usage.work_once()
    logged = events(caplog, "usage_rolled_up")
    assert len(logged) == 1
    assert {k: v for k, v in logged[0].items() if k != "duration_ms"} == {
        "days": 1,
        "sessions": 1,
        "visits": 1,
        "deleted": 0,
        "outcome": "success",
    }


def rolled_up(caplog: pytest.LogCaptureFixture) -> list[tuple[int, object]]:
    return [
        (r.levelno, r.__dict__["fields"]["outcome"])
        for r in caplog.records
        if r.getMessage() == "usage_rolled_up"
    ]


def test_a_session_open_across_midnight_rolls_yesterday_up_at_most_twice(
    path: Path,
    clock: Clock,
    free: Free,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, "tunebox.usage")
    rolls: list[str] = []
    real_roll_up = usagedb.roll_up

    def counting(db: sqlite3.Connection, day: str, zone: object) -> tuple[int, int]:
        rolls.append(day)
        return real_roll_up(db, day, cast(Any, zone))

    monkeypatch.setattr(usagedb, "roll_up", counting)
    midnight = DAY0 + 86_400
    clock.now = midnight - 120
    with started(make(path, clock, free)) as usage:
        # Polls every 10 s from 23:58:03, a round every minute on the minute.
        for second in range(0, 7 * 60 + 1):
            clock.now = midnight - 120 + second
            if second % 10 == 3:
                usage.heard(None, "owr", "Default", AGENT, None)
            if second % 60 == 0:
                usage.work_once()
    # Rolled once as a new day, and once more for the poll that crossed
    # midnight after that roll.
    assert rolls.count("2026-10-24") <= 2
    assert [level for level, _ in rolled_up(caplog) if level >= logging.INFO] == [logging.INFO]
    assert query(
        path, "SELECT value FROM daily WHERE day = '2026-10-24' AND metric = 'seconds_channel'"
    ) == [(117.0,)]


def test_reads_answer_unavailable_while_the_store_is_unavailable(
    usage: Usage, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    usage.work_once()
    monkeypatch.setattr(usagedb, "write_batch", broken_write)
    usage.heard(None, "owr", "Default", AGENT, None)
    usage.work_once()
    clock.now += RETRY_S
    usage.heard(None, "owr", "Default", AGENT, None)
    usage.work_once()  # still failing: logged once
    for read in (
        lambda: usage.summary("7d"),
        usage.devices,
        lambda: usage.device(ID, None),
        lambda: usage.rename(ID, "Car"),
        lambda: usage.set_retention(30),
    ):
        with pytest.raises(UsageUnavailable):
            read()
    assert events(caplog, "usage_store_unavailable") == [{"reason": "write_failed"}]
    monkeypatch.setattr(usagedb, "write_batch", REAL_WRITE)
    clock.now += RETRY_S
    usage.work_once()
    assert len(events(caplog, "usage_store_recovered")) == 1
    assert usage.summary("7d")["zone"] == "UTC"


def test_live_answers_from_memory_while_the_store_is_unavailable(
    usage: Usage, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.work_once()
    monkeypatch.setattr(usagedb, "write_batch", broken_write)
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.work_once()
    answer = usage.live()
    assert answer["saved"] is False
    assert [s["channel"] for s in sessions(usage)] == ["owr"]


def test_a_failed_read_is_logged_once_and_answers_unavailable(
    usage: Usage, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    usage.work_once()

    def broken_reader(_: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(usagedb, "open_reader", broken_reader)
    for _ in range(2):
        with pytest.raises(UsageUnavailable):
            usage.devices()
    assert events(caplog, "usage_store_unavailable") == [{"reason": "read_failed"}]


def test_a_newer_schema_makes_usage_unavailable_and_leaves_the_file(
    path: Path, clock: Clock, free: Free, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    db = usagedb.open_writer(path)
    db.execute("UPDATE schema_version SET version = 99")
    db.close()
    before = path.read_bytes()
    newer = make(path, clock, free)
    newer.visit(ID, AGENT, None)
    newer.work_once()
    clock.now += RETRY_S
    newer.work_once()
    assert path.read_bytes() == before
    assert events(caplog, "usage_store_unavailable") == [{"reason": "schema_newer"}]
    assert newer.live()["saved"] is False
    with pytest.raises(UsageUnavailable):
        newer.summary("7d")


def test_disk_below_the_floor_buffers_instead_of_writing(
    path: Path, clock: Clock, free: Free
) -> None:
    free.value = 0
    with started(make(path, clock, free)) as low:
        low.work_once()
        assert not path.exists()  # not even opened
        free.value = None
        clock.now += RETRY_S
        low.work_once()
        free.value = 0
        low.visit(ID, AGENT, None)
        low.work_once()
        assert query(path, "SELECT count(*) FROM visits") == [(0,)]
        assert low.live()["saved"] is False
        free.value = None
        clock.now += RETRY_S
        low.work_once()
        assert query(path, "SELECT device FROM visits") == [(ID,)]
        assert low.live()["saved"] is True


def test_rows_dropped_from_memory_are_counted_for_today(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.work_once()
    monkeypatch.setattr(usagelive, "MAX_PENDING", 2)
    for _ in range(5):
        usage.visit(ID, AGENT, None)
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(2,)]
    assert query(path, "SELECT value FROM daily WHERE metric = 'dropped' AND key = 'pending'") == [
        (3.0,)
    ]


def test_a_failed_flush_counts_what_requeue_drops(
    usage: Usage, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.work_once()
    monkeypatch.setattr(usagelive, "MAX_PENDING", 3)
    for _ in range(3):
        usage.visit(ID, AGENT, None)
    entered, go = threading.Event(), threading.Event()

    def failing_write(*_: object) -> usagedb.Written:
        entered.set()
        go.wait(5)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(usagedb, "write_batch", failing_write)
    flusher = threading.Thread(target=usage.work_once)
    flusher.start()
    assert entered.wait(5)
    usage.visit(OTHER, AGENT, None)  # arrives during the failing flush
    go.set()
    flusher.join(5)
    monkeypatch.setattr(usagedb, "write_batch", REAL_WRITE)
    clock.now += RETRY_S
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(3,)]
    assert query(path, "SELECT value FROM daily WHERE metric = 'dropped' AND key = 'pending'") == [
        (1.0,)
    ]


def test_stop_closes_and_flushes_open_sessions(path: Path, clock: Clock, free: Free) -> None:
    writer = make(path, clock, free)
    writer.heard(None, "owr", "Default", AGENT, None)
    heard_at = clock.now
    clock.now += 30
    stop = threading.Event()
    writer.start(stop)
    stop.set()
    writer.stop_and_join(time.monotonic() + 10)
    assert query(path, "SELECT started_at, last_seen FROM listens") == [(heard_at, heard_at)]
    assert writer.live()["saved"] is False  # closed with the writer
    assert cast(dict[str, object], writer.live()["last_ended"])["channel"] == "owr"


def test_heard_returns_while_a_flush_is_blocked_in_sqlite(
    usage: Usage, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, go = threading.Event(), threading.Event()

    def blocked_write(*args: object) -> usagedb.Written:
        entered.set()
        go.wait(5)
        return REAL_WRITE(*args)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(usagedb, "write_batch", blocked_write)
    usage.visit(ID, AGENT, None)
    flusher = threading.Thread(target=usage.work_once)
    flusher.start()
    try:
        assert entered.wait(5)
        began = time.monotonic()
        usage.heard(ID, "owr", "Default", AGENT, None)
        usage.visit(ID, AGENT, None)
        assert len(sessions(usage)) == 1
        assert time.monotonic() - began < 0.5
    finally:
        go.set()
        flusher.join(5)


def test_heard_never_raises(
    usage: Usage, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError(ID)

    monkeypatch.setattr(Live, "heard", boom)
    monkeypatch.setattr(Live, "visit", boom)
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.visit(ID, AGENT, None)
    assert events(caplog, "usage_failed") == [{"kind": "RuntimeError"}]
    assert ID not in caplog.text


def test_commands_time_out_as_unavailable_and_never_run_late(
    path: Path, clock: Clock, free: Free, monkeypatch: pytest.MonkeyPatch
) -> None:
    with started(make(path, clock, free)) as first:
        first.visit(ID, AGENT, None)
        first.work_once()
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", 0.05)
    idle = make(path, clock, free)  # no writer running
    with pytest.raises(UsageUnavailable):
        idle.rename(ID, "Car")
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", COMMAND_WAIT_S)
    with started(idle):
        idle.work_once()
    assert query(path, "SELECT name FROM devices") == [(None,)]


def test_retention_is_validated_stored_and_read_back(path: Path, clock: Clock, free: Free) -> None:
    with started(make(path, clock, free)) as first:
        assert first.set_retention(30) == UsageSettings(30, False, True)
        for days in (29, 3_651):
            with pytest.raises(ValueError):
                first.set_retention(days)
    second = make(path, clock, free)
    assert second.settings() == UsageSettings(365, False, False)
    with started(second):
        second.work_once()
        assert second.settings() == UsageSettings(30, False, True)


def test_retention_is_gated_on_the_roll_up(usage: Usage, clock: Clock, path: Path) -> None:
    usage.work_once()
    old = clock.now - 400 * 86_400
    execute(
        path,
        f"INSERT INTO visits VALUES ({old}, '{ID}', '{AGENT}', NULL);"
        "UPDATE meta SET value = '2027-01-01' WHERE key = 'last_rollup_day';"
        "DELETE FROM meta WHERE key = 'last_retention_day';",
    )
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(1,)]
    execute(path, "UPDATE meta SET value = '2026-10-23' WHERE key = 'last_rollup_day'")
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(0,)]
    assert meta(path, "last_retention_day") == "2026-10-24"


def test_retention_runs_once_per_day(usage: Usage, clock: Clock, path: Path) -> None:
    usage.work_once()
    assert meta(path, "last_retention_day") == "2026-10-24"
    execute(path, f"INSERT INTO visits VALUES ({clock.now - 400 * 86_400}, '{ID}', 'a', NULL)")
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(1,)]


def test_the_summary_joins_finished_days_and_today(usage: Usage, clock: Clock) -> None:
    usage.visit(ID, AGENT, None)
    usage.heard(ID, "owr", "Default", AGENT, None)
    clock.now += 20
    usage.heard(ID, "owr", "Default", AGENT, None)
    usage.work_once()
    clock.now = DAY0 + 86_400 + 43_200  # 2026-10-25 12:00
    usage.heard(None, "qmusic", "Low", "Firefox 140, Android", None)
    usage.work_once()
    week = usage.summary("7d")
    buckets = cast(list[dict[str, object]], week["buckets"])
    assert buckets[0]["label"] == "2026-10-19"
    assert buckets[-2:] == [
        {"label": "2026-10-24", "seconds": 20.0, "visits": 1},
        {"label": "2026-10-25", "seconds": 10.0, "visits": 0},
    ]
    assert len(buckets) == 7
    assert week["channels"] == [["owr", 20.0], ["qmusic", 10.0]]
    assert week["streams"] == [["owr/Default", 20.0], ["qmusic/Low", 10.0]]
    assert week["agents"] == [[AGENT, 20.0], ["Firefox 140, Android", 10.0]]
    assert week["peak"] == {"value": 1, "day": "2026-10-25"}
    assert len(cast(list[object], usage.summary("30d")["buckets"])) == 30
    year = cast(list[dict[str, object]], usage.summary("12m")["buckets"])
    assert year[0]["label"] == "2025-11" and len(year) == 12
    assert year[-1] == {"label": "2026-10", "seconds": 30.0, "visits": 1}
    every = cast(list[dict[str, object]], usage.summary("all")["buckets"])
    assert every == [{"label": "2026-10", "seconds": 30.0, "visits": 1}]
    with pytest.raises(ValueError):
        usage.summary("1y")


def test_the_summary_of_an_empty_store(usage: Usage) -> None:
    usage.work_once()
    answer = usage.summary("all")
    assert answer["buckets"] == [{"label": "2026-10", "seconds": 0.0, "visits": 0}]
    assert answer["peak"] == {"value": 0, "day": None}


def test_the_device_page_shows_its_sessions(usage: Usage) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    usage.heard(ID, "owr", "Default", AGENT, ADDRESS)
    usage.work_once()
    page = usage.device(ID, None)
    assert page is not None
    rows = cast(list[dict[str, object]], page["sessions"])
    assert [(r["channel"], r["address"]) for r in rows] == [("owr", ADDRESS)]
    assert usage.device(OTHER, None) is None


def test_the_last_ended_session_is_loaded_at_start(path: Path, clock: Clock, free: Free) -> None:
    with started(make(path, clock, free)) as first:
        first.heard(None, "owr", "Default", AGENT, None)
        first.work_once()
    second = make(path, clock, free)
    assert second.live()["last_ended"] is None
    with started(second):
        second.work_once()
        ended = cast(dict[str, object], second.live()["last_ended"])
        assert ended["channel"] == "owr"


def test_a_changed_zone_is_logged_once_and_the_current_one_labels_days(
    path: Path, clock: Clock, free: Free, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, "tunebox.usage")
    with started(make(path, clock, free)) as first:
        first.work_once()
    assert events(caplog, "usage_zone_changed") == []

    def amsterdam() -> Usage:
        return Usage(
            path,
            zone=ZoneInfo("Europe/Amsterdam"),
            zone_name="Europe/Amsterdam",
            free=free,
            clock=clock,
            monotonic=clock,
            flush_s=1e9,
        )

    with started(amsterdam()) as second:
        second.work_once()
        assert second.summary("7d")["zone"] == "Europe/Amsterdam"
    with started(amsterdam()) as third:
        third.work_once()
    assert events(caplog, "usage_zone_changed") == [
        {"stored": "UTC", "current": "Europe/Amsterdam"}
    ]
    assert meta(path, "zone") == "Europe/Amsterdam"


def test_a_file_that_cannot_be_opened_is_retried(
    path: Path, clock: Clock, free: Free, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    path.mkdir()
    with started(make(path, clock, free)) as writer:
        writer.visit(ID, AGENT, None)
        writer.work_once()
        assert events(caplog, "usage_store_unavailable") == [{"reason": "open_failed"}]
        path.rmdir()
        writer.work_once()  # waits out RETRY_S
        assert not path.exists()
        clock.now += RETRY_S
        writer.work_once()
        assert query(path, "SELECT device FROM visits") == [(ID,)]
        assert len(events(caplog, "usage_store_recovered")) == 1


def test_a_command_that_fails_in_sqlite_answers_unavailable(usage: Usage, path: Path) -> None:
    usage.visit(ID, AGENT, None)
    usage.work_once()
    execute(
        path,
        "CREATE TRIGGER no_rename BEFORE UPDATE ON devices BEGIN SELECT RAISE(ABORT, 'x'); END;",
    )
    with pytest.raises(UsageUnavailable):
        usage.rename(ID, "Car")
    assert usage.live()["saved"] is False


def test_a_bug_in_a_round_is_logged_once_and_the_writer_goes_on(
    usage: Usage,
    clock: Clock,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    usage.work_once()
    real_roll_up = usagedb.roll_up

    def broken_roll_up(*_: object) -> tuple[int, int]:
        raise RuntimeError("x")

    monkeypatch.setattr(usagedb, "roll_up", broken_roll_up)
    clock.now = DAY0 + 86_400 + 100
    usage.work_once()
    usage.work_once()
    assert events(caplog, "usage_failed") == [{"kind": "RuntimeError"}]
    assert rolled_up(caplog) == [(logging.WARNING, "error")]
    monkeypatch.setattr(usagedb, "roll_up", real_roll_up)
    usage.visit(ID, AGENT, None)
    usage.work_once()
    assert query(path, "SELECT count(*) FROM visits") == [(1,)]
    assert meta(path, "last_rollup_day") == "2026-10-24"
    assert rolled_up(caplog) == [(logging.WARNING, "error"), (logging.INFO, "success")]


def test_a_clear_cut_short_is_finished_at_start(path: Path, clock: Clock, free: Free) -> None:
    with started(make(path, clock, free)) as first:
        first.set_addresses(True)
        first.visit(ID, AGENT, ADDRESS)
        first.work_once()
    execute(path, "UPDATE meta SET value = '0' WHERE key = 'addresses_cleared'")
    with started(make(path, clock, free)) as second:
        second.work_once()
        assert second.settings() == UsageSettings(365, False, True)
    assert addresses(path) == [None]
    assert meta(path, "store_addresses") == "0"


def test_work_once_right_after_start_runs_on_the_writer(
    path: Path,
    clock: Clock,
    free: Free,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "tunebox.usage")
    original = Usage.run_forever

    def late_run_forever(self: Usage, stop: threading.Event) -> None:
        time.sleep(0.05)  # start() has returned before the writer begins
        original(self, stop)

    monkeypatch.setattr(Usage, "run_forever", late_run_forever)
    with started(make(path, clock, free)) as writer:
        writer.visit(ID, AGENT, None)
        writer.work_once()
        writer.work_once()
        assert writer.live()["saved"] is True
        assert query(path, "SELECT device FROM visits") == [(ID,)]
    assert events(caplog, "usage_store_unavailable") == []
    assert events(caplog, "usage_failed") == []


class Boom(BaseException):
    pass


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_base_exception_in_a_command_reaches_the_caller(
    usage: Usage, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.work_once()

    def boom(*_: object) -> bool:
        raise Boom

    monkeypatch.setattr(usagedb, "rename_device", boom)
    answers: list[BaseException] = []

    def call() -> None:
        try:
            usage.rename(ID, "Car")
        except BaseException as exc:  # noqa: BLE001 - the answer under test
            answers.append(exc)

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    caller.join(COMMAND_WAIT_S + 5)
    assert not caller.is_alive()
    assert [type(a) for a in answers] == [Boom]


def test_a_command_running_at_the_deadline_is_waited_for(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.visit(ID, AGENT, None)
    usage.work_once()
    real_rename = usagedb.rename_device

    def slow_rename(db: sqlite3.Connection, device: str, name: str | None) -> bool:
        time.sleep(0.3)
        return real_rename(db, device, name)

    monkeypatch.setattr(usagedb, "rename_device", slow_rename)
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", 0.05)
    assert usage.rename(ID, "Car") is True
    assert query(path, "SELECT name FROM devices") == [("Car",)]


def test_a_command_not_started_at_the_deadline_is_cancelled(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.visit(ID, AGENT, None)
    usage.work_once()
    entered, go = threading.Event(), threading.Event()

    def slow_write(*args: object) -> usagedb.Written:
        entered.set()
        go.wait(5)
        return REAL_WRITE(*args)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(usagedb, "write_batch", slow_write)
    flusher = threading.Thread(target=usage.work_once)
    flusher.start()
    assert entered.wait(5)
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", 0.05)
    with pytest.raises(UsageUnavailable):
        usage.rename(ID, "Car")
    monkeypatch.setattr(usage_module, "COMMAND_WAIT_S", COMMAND_WAIT_S)
    go.set()
    flusher.join(5)
    usage.work_once()
    assert query(path, "SELECT name FROM devices") == [(None,)]


def test_a_batch_requeued_after_an_address_off_keeps_no_address(
    usage: Usage, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.set_addresses(True)
    usage.visit(ID, AGENT, ADDRESS)
    lives: list[Live] = []
    original = Live.detach

    def capturing_detach(
        self: Live,
    ) -> tuple[list[VisitRow], list[SessionRow], list[tuple[SessionRow, float]]]:
        lives.append(self)
        return original(self)

    def off_then_crash(*_: object) -> usagedb.Written:
        lives[-1].addresses = False  # the switch lands after the writer's check
        raise RuntimeError("x")

    monkeypatch.setattr(Live, "detach", capturing_detach)
    monkeypatch.setattr(usagedb, "write_batch", off_then_crash)
    usage.work_once()
    monkeypatch.setattr(usagedb, "write_batch", REAL_WRITE)
    usage.set_addresses(True)
    usage.work_once()
    assert query(path, "SELECT address FROM visits") == [(None,)]
