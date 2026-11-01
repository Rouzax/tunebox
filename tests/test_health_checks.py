import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import channel_config
from tunebox.channel import Liveness
from tunebox.config import ChannelConfig, SourceConfig, StreamConfig
from tunebox.health import (
    BUTTON_GAP_S,
    MAX_HEALTH_ITEMS,
    NETWORK_RETRY_S,
    HealthChecks,
    LiveChannel,
)
from tunebox.http import HttpError
from tunebox.netguard import Probe
from tunebox.sources.base import SourceError
from tunebox.store import HealthRow, Store, Written
from tunebox.tracks import RawEntry

ADTS = b"\xff\xf1\x50\x80" + b"\x00" * 60


class FakeChannel:
    def __init__(self, config: ChannelConfig, live: Liveness | None = None) -> None:
        self.config = config
        self.live = live or Liveness(False, None, None, False, None)

    def liveness(self) -> Liveness:
        return self.live


class Net:
    """Answers each URL from a table: a Probe, or an exception to raise.
    Every probe and feed fetch is recorded."""

    def __init__(self) -> None:
        self.streams: dict[str, Probe | Exception] = {}
        self.feeds: dict[str, list[RawEntry] | Exception] = {}
        self.asked: list[str] = []

    def probe(self, url: str) -> Probe:
        self.asked.append(url)
        answer = self.streams.get(url, Probe("audio/aac", {}, ADTS))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def feed(self, source: SourceConfig) -> list[RawEntry]:
        key = source.settings.get("mount", "")
        self.asked.append(f"feed:{key}")
        answer = self.feeds.get(key, [RawEntry("A", "B", 1)])
        if isinstance(answer, Exception):
            raise answer
        return answer


def station(cid: str, url: str | None = None) -> ChannelConfig:
    return replace(
        channel_config(),
        id=cid,
        name=cid.upper(),
        streams=(StreamConfig("Default", url or f"https://example.invalid/{cid}", None, None),),
        source=SourceConfig("triton", {"mount": cid}),
    )


def stations(*ids: str) -> dict[str, LiveChannel]:
    return {cid: FakeChannel(station(cid)) for cid in ids}


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(
    store: Store,
    net: Net,
    channels: Mapping[str, LiveChannel],
    clock: Clock | None = None,
    probe: Callable[[str], Probe] | None = None,
) -> HealthChecks:
    return HealthChecks(
        probe or net.probe,
        net.feed,
        store,
        lambda: channels,
        UTC,
        wall=clock or Clock(),
        pause_s=0.0,
    )


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == name]


def fields(record: logging.LogRecord) -> dict[str, Any]:
    return record.__dict__["fields"]


def until(condition: Callable[[], bool], limit_s: float = 5.0) -> None:
    deadline = time.monotonic() + limit_s
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        threading.Event().wait(0.01)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "tunebox.db")


def test_a_run_checks_every_stream_and_feed_and_stores_them(store: Store) -> None:
    checks = make(store, Net(), stations("a", "b"))
    result = checks.run_once("button", threading.Event())
    assert (result.outcome, result.items, result.failing, result.skipped) == ("ok", 4, 0, 0)
    rows = store.health_rows()
    assert rows is not None and len(rows) == 4
    assert all(r.ok for r in rows)
    assert [r.codec for r in rows if r.item.startswith("stream:")] == ["aac", "aac"]
    page = checks.page()
    assert page is not None
    assert [s["status"] for s in page["stations"]] == ["ok", "ok"]
    assert [i["item"] for i in page["stations"][0]["items"]] == ["stream:Default", "feed"]
    assert page["stations"][0]["items"][0]["codec"] == "aac"
    assert page["run"]["outcome"] == "ok"
    assert page["progress"] is None


def test_a_failure_is_tried_twice_before_it_counts(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    net = Net()
    asked: list[str] = []

    def once_404(url: str) -> Probe:
        asked.append(url)
        if url.endswith("/a") and asked.count(url) == 1:
            raise HttpError("x: HTTP 404", 404)
        return net.probe(url)

    checks = make(store, net, stations("a", "b"), probe=once_404)
    with caplog.at_level(logging.INFO):
        result = checks.run_once("button", threading.Event())
    assert result.outcome == "ok" and result.failing == 0
    assert asked.count("https://example.invalid/a") == 2
    rows = {(r.channel, r.item): r for r in store.health_rows() or []}
    assert rows["a", "stream:Default"].ok
    assert events(caplog, "health_failing") == []


def test_two_failures_store_failing_since_from_the_first_attempt_and_warn_once(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    net = Net()
    net.streams["https://example.invalid/a"] = HttpError("x: HTTP 404", 404)
    clock = Clock()
    checks = make(store, net, stations("a", "b", "c", "d"), clock)
    first = round(clock.now * 1000)
    with caplog.at_level(logging.INFO):
        assert checks.run_once("daily", threading.Event()).failing == 1
        clock.now += 86_400
        assert checks.run_once("daily", threading.Event()).failing == 1
    failing = events(caplog, "health_failing")
    assert len(failing) == 1
    assert failing[0].levelno == logging.WARNING
    assert fields(failing[0]) == {"channel": "a", "item": "stream:Default", "reason": "http_404"}
    rows = {(r.channel, r.item): r for r in store.health_rows() or []}
    row = rows["a", "stream:Default"]
    assert (row.ok, row.reason, row.failing_since) == (False, "http_404", first)
    assert row.checked_at == round(clock.now * 1000)


def test_a_recovery_logs_once(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    net = Net()
    net.streams["https://example.invalid/a"] = HttpError("x: HTTP 404", 404)
    clock = Clock()
    checks = make(store, net, stations("a", "b", "c", "d"), clock)
    checks.run_once("daily", threading.Event())
    del net.streams["https://example.invalid/a"]
    clock.now += 3600
    caplog.clear()
    with caplog.at_level(logging.INFO):
        checks.run_once("daily", threading.Event())
        checks.run_once("daily", threading.Event())
    recovered = events(caplog, "health_recovered")
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO
    assert fields(recovered[0]) == {"channel": "a", "item": "stream:Default", "failing_s": 3600}
    assert events(caplog, "health_failing") == []


def test_tunebox_offline_changes_nothing(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    net = Net()
    clock = Clock()
    checks = make(store, net, stations("a", "b"), clock)
    checks.run_once("daily", threading.Event())
    before = store.health_rows()
    down = HttpError("x: name lookup failed", unreachable=True)
    lost = SourceError("lookup")
    lost.__cause__ = down
    for cid in ("a", "b"):
        net.streams[f"https://example.invalid/{cid}"] = down
        net.feeds[cid] = lost
    clock.now += 86_400
    with caplog.at_level(logging.INFO):
        result = checks.run_once("daily", threading.Event())
    assert (result.outcome, result.items, result.failing) == ("network_down", 4, 4)
    assert store.health_rows() == before
    down_lines = events(caplog, "health_network_down")
    assert len(down_lines) == 1 and down_lines[0].levelno == logging.WARNING
    assert fields(down_lines[0]) == {"items": 4, "failing": 4}
    assert events(caplog, "health_failing") == []
    run = store.health_run()
    assert run.outcome == "network_down"
    assert run.last_finished_at == round(clock.now * 1000)


def test_the_network_rule_needs_more_than_three_items(store: Store) -> None:
    net = Net()
    down = HttpError("x: name lookup failed", unreachable=True)
    net.streams["https://example.invalid/a"] = down
    lost = SourceError("lookup")
    lost.__cause__ = down
    net.feeds["a"] = lost
    alone = make(store, net, stations("a"))
    assert alone.run_once("daily", threading.Event()).outcome == "network_down"
    assert store.health_rows() == []

    ten = make(store, net, stations(*"abcdefghij"))
    del net.feeds["a"]
    result = ten.run_once("daily", threading.Event())
    assert (result.outcome, result.items, result.failing) == ("ok", 20, 1)
    rows = {(r.channel, r.item): r for r in store.health_rows() or []}
    assert len(rows) == 20
    assert (rows["a", "stream:Default"].ok, rows["a", "stream:Default"].reason) == (
        False,
        "unreachable",
    )


def test_items_past_the_cap_are_not_probed() -> None:
    # In memory: hundreds of commits on disk would cost seconds.
    store = Store(None)
    net = Net()
    ids = [f"s{n:03d}" for n in range(MAX_HEALTH_ITEMS // 2 + 1)]
    checks = make(store, net, stations(*ids))
    result = checks.run_once("daily", threading.Event())
    assert len(net.asked) == MAX_HEALTH_ITEMS
    assert (result.outcome, result.items, result.skipped) == ("ok", MAX_HEALTH_ITEMS, 2)
    page = checks.page()
    assert page is not None
    last = next(s for s in page["stations"] if s["id"] == ids[-1])
    assert all(i["ok"] is None for i in last["items"])


def test_a_stop_between_items_ends_the_run_without_writing(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    net = Net()
    stop = threading.Event()

    def stopping(url: str) -> Probe:
        if url.endswith("/b"):
            stop.set()
        return net.probe(url)

    checks = make(store, net, stations("a", "b", "c"), probe=stopping)
    with caplog.at_level(logging.INFO):
        result = checks.run_once("daily", stop)
    assert result.outcome == "stopped"
    assert "https://example.invalid/c" not in net.asked
    assert store.health_rows() == []
    assert store.health_run().last_finished_at is None
    assert events(caplog, "health_check_done") == []
    assert checks.count()["progress"] is None


def test_a_stop_at_any_point_writes_nothing_more(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    preset = make(store, Net(), stations("a"))
    stop.set()
    assert preset.run_once("daily", stop).outcome == "stopped"
    assert store.health_run().last_started_at is None

    def stopping_on(net: Net, url: str, call: int) -> Callable[[str], Probe]:
        def probe(asked: str) -> Probe:
            if asked == url and net.asked.count(url) + 1 == call:
                stop.set()
            return net.probe(asked)

        return probe

    # During the second attempts.
    net = Net()
    net.streams["https://example.invalid/a"] = HttpError("x: HTTP 404", 404)
    net.feeds["a"] = SourceError("x")
    stop.clear()
    second = make(
        store, net, stations("a", "b"), probe=stopping_on(net, "https://example.invalid/a", 2)
    )
    assert second.run_once("daily", stop).outcome == "stopped"
    assert net.asked.count("feed:a") == 1

    # On the last check of a run that found the network down.
    net = Net()
    slow = HttpError("x: took too long")
    lost = SourceError("x")
    lost.__cause__ = slow
    net.streams["https://example.invalid/a"] = slow
    net.feeds["a"] = lost

    def feed_stopping(source: SourceConfig) -> list[RawEntry]:
        if net.asked.count("feed:a") == 1:
            stop.set()
        return net.feed(source)

    stop.clear()
    down = HealthChecks(
        net.probe, feed_stopping, store, lambda: stations("a"), UTC, wall=Clock(), pause_s=0.0
    )
    assert down.run_once("daily", stop).outcome == "stopped"
    assert store.health_run().outcome is None

    # On the last check of a good run, and during the last write.
    net = Net()

    def stop_on_b(source: SourceConfig) -> list[RawEntry]:
        if source.settings["mount"] == "b":
            stop.set()
        return net.feed(source)

    stop.clear()
    last = HealthChecks(
        net.probe, stop_on_b, store, lambda: stations("a", "b"), UTC, wall=Clock(), pause_s=0.0
    )
    assert last.run_once("daily", stop).outcome == "stopped"
    assert store.health_rows() == []
    assert store.health_run().last_finished_at is None

    write = store.health_write

    def stop_on_last(row: HealthRow) -> Written:
        if row.channel == "b" and row.item == "feed":
            stop.set()
        return write(row)

    stop.clear()
    writing = make(store, Net(), stations("a", "b"))
    monkeypatch.setattr(store, "health_write", stop_on_last)
    assert writing.run_once("daily", stop).outcome == "stopped"
    assert len(store.health_rows() or []) == 4
    assert store.health_run().last_finished_at is None


def test_a_crash_after_stop_records_nothing(store: Store) -> None:
    stop = threading.Event()

    def stopping() -> Mapping[str, LiveChannel]:
        stop.set()
        raise RuntimeError("closing")

    checks = HealthChecks(Net().probe, Net().feed, store, stopping, UTC, wall=Clock())
    with pytest.raises(RuntimeError):
        checks.run_once("daily", stop)
    assert store.health_run().outcome is None


def test_a_network_down_run_is_retried_three_times(store: Store) -> None:
    clock = Clock()
    checks = make(store, Net(), stations("a"), clock)
    store.health_run_started(round(clock.now * 1000))
    store.health_run_finished(round(clock.now * 1000), 1, 2, "network_down")
    plan = checks._plan  # pyright: ignore[reportPrivateUsage]
    for _ in range(3):
        assert plan("network_down") == (clock.now + NETWORK_RETRY_S, "retry")
    assert plan("network_down")[1] == "daily"
    plan("ok")
    assert plan("network_down")[1] == "retry"
    # A retry never comes after the daily run it would duplicate.
    clock.now = datetime(2027, 1, 15, 4, 10, tzinfo=UTC).timestamp()
    store.health_run_finished(round(clock.now * 1000), 1, 2, "network_down")
    assert plan("network_down")[1] == "daily"


def test_no_log_line_holds_the_url_or_server_text(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://user:secret@example.invalid/live?token=abc123"
    net = Net()
    net.streams[url] = HttpError(f"{url}: SSH-2.0-OpenSSH banner")
    channels = stations("b", "c", "d")
    channels["a"] = FakeChannel(station("a", url))
    checks = make(store, net, channels)
    with caplog.at_level(logging.DEBUG):
        checks.run_once("daily", threading.Event())
    assert len(events(caplog, "health_failing")) == 1
    seen = caplog.text + "".join(repr(r.__dict__.get("fields")) for r in caplog.records)
    for secret in ("secret", "abc123", "OpenSSH", "example.invalid"):
        assert secret not in seen


def test_a_crashing_check_is_one_item_not_the_run(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    net = Net()
    net.feeds["a"] = KeyError("entries")
    checks = make(store, net, stations("a", "b"))
    with caplog.at_level(logging.INFO):
        result = checks.run_once("daily", threading.Event())
    assert (result.outcome, result.failing) == ("ok", 1)
    assert net.asked.count("feed:a") == 2
    rows = {(r.channel, r.item): r for r in store.health_rows() or []}
    assert (rows["a", "feed"].ok, rows["a", "feed"].reason) == (False, "unreadable")
    assert all(r.ok for key, r in rows.items() if key != ("a", "feed"))
    crashed = events(caplog, "health_item_crashed")
    assert len(crashed) == 1 and crashed[0].levelno == logging.WARNING
    assert fields(crashed[0]) == {"channel": "a", "item": "feed", "error": "KeyError"}
    assert crashed[0].exc_info is None


def test_a_check_that_raises_is_stored_as_unreadable(store: Store) -> None:
    def broken(url: str) -> Probe:
        raise ValueError(url)

    checks = make(store, Net(), stations("a", "b"), probe=broken)
    assert checks.run_once("daily", threading.Event()).failing == 2
    rows = [r for r in store.health_rows() or [] if r.item == "stream:Default"]
    assert {(r.ok, r.reason) for r in rows} == {(False, "unreadable")}


def test_request_refuses_while_running_and_within_the_gap(store: Store) -> None:
    net = Net()
    release = threading.Event()
    entered = threading.Event()

    def blocking(url: str) -> Probe:
        entered.set()
        release.wait(5)
        return net.probe(url)

    clock = Clock()
    checks = make(store, net, stations("a"), clock, probe=blocking)
    stop = threading.Event()
    checks.start(stop)
    try:
        assert checks.alive()
        assert checks.request().outcome == "started"
        assert checks.request().outcome == "running"
        assert checks.count()["running"]
        assert entered.wait(5)
        progress = checks.count()["progress"]
        assert progress is not None
        assert (progress["trigger"], progress["total"]) == ("button", 2)
        release.set()
        until(lambda: not checks.count()["running"])
        asked = checks.request()
        assert asked.outcome == "too_soon"
        assert 0 < asked.retry_s <= BUTTON_GAP_S
        clock.now += BUTTON_GAP_S + 1
        assert checks.request().outcome == "started"
        until(lambda: not checks.count()["running"])
    finally:
        release.set()
        stop.set()
        checks.stop_and_join(time.monotonic() + 2)
    assert not checks.alive()
    assert checks.request().outcome == "stopped"


def test_request_says_stopped_without_a_worker(store: Store) -> None:
    checks = make(store, Net(), stations("a"))
    assert checks.request().outcome == "stopped"
    assert not checks.alive()
    checks.stop_and_join(time.monotonic())


def test_the_loop_survives_a_crash(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    net = Net()
    clock = Clock()
    channels = stations("a")
    calls: list[int] = []

    def flaky() -> Mapping[str, LiveChannel]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return channels

    checks = HealthChecks(
        net.probe, net.feed, store, flaky, UTC, wall=clock, pause_s=0.0, retry_wait_s=0.01
    )
    stop = threading.Event()
    with caplog.at_level(logging.INFO):
        checks.start(stop)
        try:
            assert checks.request().outcome == "started"
            until(lambda: bool(events(caplog, "health_check_crashed")))
            until(lambda: not checks.count()["running"])
            crash = events(caplog, "health_check_crashed")[0]
            assert crash.levelno == logging.ERROR and crash.exc_info is not None
            assert checks.alive()
            assert store.health_run().outcome == "error"
            clock.now += BUTTON_GAP_S + 1
            assert checks.request().outcome == "started"
            until(lambda: store.health_run().outcome == "ok")
        finally:
            stop.set()
            checks.stop_and_join(time.monotonic() + 2)
    done = [fields(r)["outcome"] for r in events(caplog, "health_check_done")]
    assert done == ["error", "ok"]


def test_page_marks_an_old_address_as_not_checked(store: Store) -> None:
    channels = stations("a")
    checks = make(store, Net(), channels)
    checks.run_once("daily", threading.Event())
    channels["a"] = FakeChannel(station("a", "https://example.invalid/new"))
    page = checks.page()
    assert page is not None
    [only] = page["stations"]
    assert only["status"] == "ok"
    assert only["reasons"] == [{"code": "not_checked", "item": "stream:Default"}]
    item = only["items"][0]
    assert (item["checked_at"], item["ok"], item["reason"], item["codec"]) == (
        None,
        None,
        None,
        None,
    )
    assert only["items"][1]["ok"] is True


def test_page_orders_broken_then_warning_then_ok(store: Store) -> None:
    net = Net()
    net.streams["https://example.invalid/c"] = HttpError("x: HTTP 404", 404)
    net.feeds["b"] = []
    live = Liveness(True, 5, 7, True, 3)
    channels = stations("a", "b", "c", "d")
    channels["d"] = FakeChannel(station("d"), live)
    checks = make(store, net, channels)
    checks.run_once("daily", threading.Event())
    page = checks.page()
    assert page is not None
    assert [(s["id"], s["status"]) for s in page["stations"]] == [
        ("c", "broken"),
        ("d", "broken"),
        ("b", "warning"),
        ("a", "ok"),
    ]
    by_id = {s["id"]: s for s in page["stations"]}
    assert by_id["d"]["live"] == {
        "active": True,
        "last_poll_ok": 5,
        "stale_since": 7,
        "last_change": 3,
    }
    assert by_id["c"]["name"] == "C"
    failing = by_id["c"]["items"][0]
    assert (failing["ok"], failing["reason"], failing["kind"], failing["name"]) == (
        False,
        "http_404",
        "stream",
        "Default",
    )
    assert failing["failing_since"] == failing["checked_at"]
    assert by_id["b"]["items"][1]["kind"] == "feed"
    assert checks.count() == {"broken": 2, "running": False, "progress": None}


def test_count_and_page_when_rows_are_unreadable(store: Store) -> None:
    checks = make(store, Net(), stations("a"))
    store._db.execute("DROP TABLE health_v1")  # pyright: ignore[reportPrivateUsage]
    assert checks.page() is None
    assert checks.count()["broken"] is None


def test_prune_follows_the_current_stations(store: Store) -> None:
    channels = stations("a", "b")
    checks = make(store, Net(), channels)
    checks.run_once("daily", threading.Event())
    del channels["b"]
    checks.run_once("daily", threading.Event())
    assert {r.channel for r in store.health_rows() or []} == {"a"}


def test_health_check_done_is_logged_with_its_trigger(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    net = Net()
    checks = make(store, net, stations("a", "b"))
    with caplog.at_level(logging.INFO):
        checks.run_once("button", threading.Event())
        down = HttpError("x: took too long")
        net.streams["https://example.invalid/a"] = down
        net.streams["https://example.invalid/b"] = down
        slow = SourceError("x")
        slow.__cause__ = down
        net.feeds["a"] = net.feeds["b"] = slow
        checks.run_once("retry", threading.Event())
    done = events(caplog, "health_check_done")
    assert [r.levelno for r in done] == [logging.INFO, logging.WARNING]
    first = fields(done[0])
    assert {k: first[k] for k in ("trigger", "items", "failing", "outcome")} == {
        "trigger": "button",
        "items": 4,
        "failing": 0,
        "outcome": "ok",
    }
    assert isinstance(first["duration_ms"], int)
    assert (first["skipped"], first["write_failures"]) == (0, 0)
    second = fields(done[1])
    assert (second["trigger"], second["outcome"]) == ("retry", "network_down")


def test_a_failed_write_still_logs_the_change(
    store: Store, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = Net()
    net.streams["https://example.invalid/a"] = HttpError("x: HTTP 404", 404)
    checks = make(store, net, stations("a", "b", "c", "d"))

    def failed(row: HealthRow) -> Written:
        return Written("write_failed", None)

    monkeypatch.setattr(store, "health_write", failed)
    with caplog.at_level(logging.INFO):
        result = checks.run_once("daily", threading.Event())
    assert (result.outcome, result.write_failures) == ("ok", 8)
    assert len(events(caplog, "health_failing")) == 1
    [done] = events(caplog, "health_check_done")
    assert done.levelno == logging.WARNING
