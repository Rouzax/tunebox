from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import channel_config
from tunebox.channel import Liveness
from tunebox.config import SourceConfig, StreamConfig
from tunebox.health import (
    LIVE_BROKEN_S,
    START_DELAY_S,
    STUCK_S,
    Judgement,
    Reason,
    fingerprint,
    judge,
    next_run,
    targets,
)
from tunebox.store import HealthRow

AMS = ZoneInfo("Europe/Amsterdam")
URL = "https://example.invalid/stream.aac"
IDLE = Liveness(False, None, None, False, None)


def at(text: str, zone: ZoneInfo = AMS) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=zone).timestamp()


def local(ts: float) -> str:
    return datetime.fromtimestamp(ts, AMS).strftime("%Y-%m-%d %H:%M")


def stream_row(
    ok: bool, at_ms: int = 1_000, target: str = URL, reason: str | None = None
) -> HealthRow:
    return HealthRow(
        "owr",
        "stream:Default",
        target,
        at_ms,
        ok,
        reason if reason or ok else "unreachable",
        "aac" if ok else None,
        None,
        None if ok else at_ms,
    )


def feed_row(ok: bool, at_ms: int = 1_000, reason: str | None = None) -> HealthRow:
    target = fingerprint(channel_config().source)
    return HealthRow(
        "owr",
        "feed",
        target,
        at_ms,
        ok,
        reason if reason or ok else "feed_error",
        None,
        None,
        None if ok else at_ms,
    )


def test_targets_name_every_stream_and_the_feed() -> None:
    config = channel_config()
    assert targets(config) == [("stream:Default", URL), ("feed", fingerprint(config.source))]


def test_the_fingerprint_follows_the_source_settings() -> None:
    a = fingerprint(SourceConfig("triton", {"mount": "A", "x": "1"}))
    assert a == fingerprint(SourceConfig("triton", {"x": "1", "mount": "A"}))
    assert a != fingerprint(SourceConfig("triton", {"mount": "B", "x": "1"}))
    assert len(a) == 16


def test_the_daily_run_is_at_half_past_four_local() -> None:
    start = at("2026-11-01 10:00")
    when, kind = next_run(at("2026-11-01 10:00"), start - 10, start - 5, start - 3600, AMS)
    assert (local(when), kind) == ("2026-11-02 04:30", "daily")
    early, _ = next_run(at("2026-11-02 03:00"), start - 10, start - 5, start, AMS)
    assert local(early) == "2026-11-02 04:30"


def test_a_daylight_saving_day_keeps_half_past_four() -> None:
    finished = at("2026-10-24 04:40")
    when, _ = next_run(at("2026-10-24 12:00"), finished, finished, finished, AMS)
    assert local(when) == "2026-10-25 04:30"
    spring = at("2027-03-27 12:00")
    when, _ = next_run(spring, spring, spring, spring, AMS)
    assert local(when) == "2027-03-28 04:30"


def test_start_up_runs_five_minutes_in_when_the_last_check_is_old() -> None:
    process = at("2026-11-01 10:00")
    when, kind = next_run(process, None, None, process, AMS)
    assert (when, kind) == (process + START_DELAY_S, "startup")
    old = process - 2 * 86_400
    when, kind = next_run(process, old, old, process, AMS)
    assert kind == "startup"


def test_a_restart_loop_does_not_check_every_time() -> None:
    process = at("2026-11-01 10:00")
    # The last run started 5 minutes ago and never finished (the process died).
    _, kind = next_run(process, process - 300, None, process, AMS)
    assert kind == "daily"


def test_an_interrupted_run_keeps_its_previous_finish() -> None:
    process = at("2026-11-01 10:00")
    _, kind = next_run(process, process - 7 * 3600, process - 3600 * 10, process, AMS)
    assert kind == "daily"


def test_a_utc_zone_works_too() -> None:
    now = datetime(2026, 11, 1, 5, 0, tzinfo=UTC).timestamp()
    when, _ = next_run(now, now, now, now, UTC)
    assert datetime.fromtimestamp(when, UTC).strftime("%d %H:%M") == "02 04:30"


def test_nothing_checked_is_ok_and_says_so() -> None:
    j = judge(channel_config(), {}, IDLE, 5_000)
    assert j.status == "ok"
    assert Reason("not_checked", "stream:Default") in j.reasons
    assert Reason("not_checked", "feed") in j.reasons


def test_every_stream_failing_is_broken() -> None:
    j = judge(
        channel_config(), {"stream:Default": stream_row(False), "feed": feed_row(True)}, IDLE, 5_000
    )
    assert j == Judgement("broken", (Reason("streams_failing", None),))


def test_a_changed_address_is_not_checked_yet_not_broken() -> None:
    rows = {
        "stream:Default": stream_row(False, target="https://example.invalid/old"),
        "feed": feed_row(True),
    }
    j = judge(channel_config(), rows, IDLE, 5_000)
    assert j.status == "ok" and Reason("not_checked", "stream:Default") in j.reasons


def test_one_of_two_streams_failing_is_a_warning() -> None:
    config = channel_config()
    two = replace(
        config,
        streams=(
            StreamConfig("Default", URL, None, None),
            StreamConfig("MP3", "https://example.invalid/m", None, None),
        ),
    )
    rows = {
        "stream:Default": stream_row(True),
        "stream:MP3": HealthRow(
            "owr", "stream:MP3", "https://example.invalid/m", 1, False, "timeout", None, None, 1
        ),
        "feed": feed_row(True),
    }
    assert judge(two, rows, IDLE, 5_000) == Judgement(
        "warning", (Reason("stream_failing", "stream:MP3"),)
    )


def test_a_failed_feed_check_after_a_good_poll_is_broken() -> None:
    rows = {"stream:Default": stream_row(True), "feed": feed_row(False, at_ms=2_000)}
    live = Liveness(True, 1_000, None, True, 1_000)
    assert judge(channel_config(), rows, live, 3_000).status == "broken"


def test_a_good_poll_after_a_failed_feed_check_is_only_a_warning() -> None:
    rows = {"stream:Default": stream_row(True), "feed": feed_row(False, at_ms=2_000)}
    live = Liveness(True, 2_500, None, True, 2_400)
    j = judge(channel_config(), rows, live, 3_000)
    assert j == Judgement("warning", (Reason("feed_check_failed_polls_ok", "feed"),))


def test_a_feed_without_songs_is_a_warning() -> None:
    rows = {"stream:Default": stream_row(True), "feed": feed_row(True, reason="no_songs")}
    assert judge(channel_config(), rows, IDLE, 3_000).status == "warning"


def test_a_plain_http_stream_is_a_warning() -> None:
    rows = {"stream:Default": stream_row(True, reason="plain_http"), "feed": feed_row(True)}
    assert judge(channel_config(), rows, IDLE, 3_000) == Judgement(
        "warning", (Reason("plain_http", "stream:Default"),)
    )


def test_a_poller_failing_ten_minutes_is_broken() -> None:
    rows = {"stream:Default": stream_row(True), "feed": feed_row(True)}
    now = 10_000_000
    young = Liveness(True, None, now - LIVE_BROKEN_S * 1000 + 1, True, None)
    old = Liveness(True, None, now - LIVE_BROKEN_S * 1000 - 1, True, None)
    assert judge(channel_config(), rows, young, now).status == "ok"
    assert judge(channel_config(), rows, old, now) == Judgement(
        "broken", (Reason("poll_failing", None),)
    )


def test_a_stuck_feed_is_a_warning_only_after_a_poll_since_waking() -> None:
    rows = {"stream:Default": stream_row(True), "feed": feed_row(True)}
    now = 100_000_000
    old_song = now - STUCK_S * 1000 - 1
    woke = Liveness(True, now, None, False, old_song)
    polled = Liveness(True, now, None, True, old_song)
    assert judge(channel_config(), rows, woke, now).status == "ok"
    assert judge(channel_config(), rows, polled, now) == Judgement(
        "warning", (Reason("stuck", None),)
    )
    assert (
        judge(channel_config(), rows, Liveness(False, now, None, True, old_song), now).status
        == "ok"
    )


@pytest.mark.parametrize("ok", [True, False])
def test_reasons_come_in_a_fixed_order(ok: bool) -> None:
    rows = {"stream:Default": stream_row(ok), "feed": feed_row(False)}
    j = judge(channel_config(), rows, IDLE, 5_000)
    assert [r.code for r in j.reasons] == (
        ["feed_failing"] if ok else ["streams_failing", "feed_failing"]
    )
