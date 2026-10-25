import time
from datetime import UTC
from zoneinfo import ZoneInfo

import pytest

from tunebox.usagecore import (
    POLL_S,
    Interval,
    aggregate,
    day_bounds,
    day_of,
    device_cookie,
    interval_end,
    short_agent,
    valid_device,
)

AMS = ZoneInfo("Europe/Amsterdam")
CAR = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.224 Safari/537.36"
)
ID = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (CAR, "Chrome 120, Linux"),
        (
            "Mozilla/5.0 (Android 14; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0",
            "Firefox 132, Android",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
            "Safari 17, iOS",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
            "Edge 130, Windows",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
            "Samsung Internet 26, Android",
        ),
        (
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) Chrome/120.0 Safari/537.36",
            "Chrome 120, ChromeOS",
        ),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) Firefox/131.0", "Firefox 131, macOS"),
        ("Chrome/1234.0 (X11; Linux x86_64)", "Chrome, Linux"),
        ("curl/8.5.0", "Browser, Other"),
        ("", "Browser, Other"),
        (None, "Browser, Other"),
        ("Chrome/120.0 " + "x" * 10_000 + " Linux", "Chrome 120, Other"),
    ],
)
def test_short_agent(header: str | None, expected: str) -> None:
    assert short_agent(header) == expected


@pytest.mark.parametrize(
    "header",
    ["Version/" + "1" * 600, "Version/" + "1" * 300 + "." * 300],
    ids=["digits", "digits-then-dots"],
)
def test_short_agent_does_not_backtrack_on_a_hostile_header(header: str) -> None:
    began = time.perf_counter()
    assert short_agent(header) == "Browser, Other"
    assert time.perf_counter() - began < 0.01


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (ID, True),
        (ID.upper(), False),
        (ID[:-1], False),
        (ID + "0", False),
        ("g" * 32, False),
        (None, False),
    ],
)
def test_valid_device(value: str | None, ok: bool) -> None:
    assert valid_device(value) is ok


def test_device_cookie_keeps_a_valid_id_and_refreshes_it() -> None:
    device, header = device_cookie(f"tunebox_device={ID}", secure=False)
    assert device == ID
    assert header == f"tunebox_device={ID}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"


@pytest.mark.parametrize(
    "cookie",
    [None, "", "tunebox_device=nothex", f"tunebox_device={ID}; tunebox_device={ID}"],
)
def test_device_cookie_mints_a_new_id_for_a_missing_bad_or_repeated_cookie(
    cookie: str | None,
) -> None:
    device, header = device_cookie(cookie, secure=True)
    assert valid_device(device) and device != ID
    assert header.startswith(f"tunebox_device={device}; ")
    assert "; Secure;" in header


def test_local_days_and_their_bounds_follow_the_zone() -> None:
    # 2026-10-25 is the night the Netherlands leaves summer time: 25 hours.
    start, end = day_bounds("2026-10-25", AMS)
    assert end - start == 25 * 3600
    assert day_of(start, AMS) == "2026-10-25"
    assert day_of(start - 1, AMS) == "2026-10-24"
    assert day_bounds("2026-10-24", UTC) == (1_792_800_000.0, 1_792_886_400.0)


def test_interval_end_gives_a_single_poll_one_poll_interval() -> None:
    assert interval_end(100.0, 100.0) == 100.0 + POLL_S
    assert interval_end(100.0, 500.0) == 500.0


def iv(start: float, end: float, channel: str = "owr", device: str | None = "d") -> Interval:
    return Interval(start, end, channel, "Default", device, "Chrome 120, Linux")


def test_aggregate_clips_to_the_day_and_sums_by_key() -> None:
    totals = aggregate([iv(-50, 50), iv(10, 40, "qmusic", None)], [5.0, 200.0, -1.0], 0, 100)
    assert totals.seconds_channel == {"owr": 50.0, "qmusic": 30.0}
    assert totals.seconds_stream == {"owr/Default": 50.0, "qmusic/Default": 30.0}
    assert totals.seconds_device == {"d": 50.0, "-": 30.0}
    assert totals.seconds_agent == {"Chrome 120, Linux": 80.0}
    assert totals.visits == 1


@pytest.mark.parametrize(
    ("intervals", "peak"),
    [
        ([iv(0, 10), iv(5, 15, device="e")], 2),  # overlapping
        ([iv(0, 10), iv(10, 20, device="e")], 1),  # touching: an end counts before a start
        ([iv(0, 30), iv(10, 20, device="e")], 2),  # nested
        ([iv(5, 15), iv(5, 15, device="e")], 2),  # two single polls at the same instant
        ([], 0),
    ],
)
def test_aggregate_peak(intervals: list[Interval], peak: int) -> None:
    assert aggregate(intervals, [], 0, 100).peak == peak


def test_aggregate_clips_a_device_s_widened_session_at_its_next_one() -> None:
    # A single poll widened to POLL_S, then a station change 5 s later.
    totals = aggregate([iv(0, POLL_S), iv(5, 60, "qmusic")], [], 0, 100)
    assert totals.peak == 1
    assert totals.seconds_channel == {"owr": 5.0, "qmusic": 55.0}
    assert totals.seconds_device == {"d": 60.0}


def test_aggregate_leaves_overlaps_of_different_or_unidentified_devices() -> None:
    assert aggregate([iv(0, 10), iv(5, 60, device="e")], [], 0, 100).peak == 2
    unidentified = aggregate([iv(0, 10, device=None), iv(5, 60, device=None)], [], 0, 100)
    assert unidentified.peak == 2
    assert unidentified.seconds_device == {"-": 65.0}
