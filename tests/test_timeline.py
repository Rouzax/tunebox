from tests.fakes import make_track
from tunebox.timeline import build

OLD = make_track("FISHER", "Stay", 1_000_000)
ID = make_track("", "E1700 16 Oct 2026 M19 VO", 1_100_000)
NEW = make_track("JAZZY", "My People", 1_200_000)
NEWEST = make_track("PEGASSI", "Yoyoyo", 1_400_000)


def test_before_the_delay_the_new_track_is_upcoming() -> None:
    timeline = build([NEW, OLD], now_ms=1_203_000, delay_s=7)
    assert timeline.current == OLD
    assert timeline.upcoming == NEW
    assert timeline.upcoming_in_s == 4
    assert timeline.history == ()


def test_after_the_delay_it_is_current() -> None:
    timeline = build([NEW, OLD], now_ms=1_207_000, delay_s=7)
    assert timeline.current == NEW
    assert timeline.upcoming is None
    assert timeline.history == (OLD,)


def test_upcoming_is_the_oldest_not_yet_heard() -> None:
    timeline = build([NEWEST, NEW, OLD], now_ms=1_100_000, delay_s=7)
    assert timeline.upcoming == NEW
    assert timeline.not_heard == (NEWEST, NEW)


def test_station_id_is_audible_but_not_current_and_not_history() -> None:
    timeline = build([NEW, ID, OLD], now_ms=1_150_000, delay_s=7)
    assert timeline.audible == ID
    assert timeline.current is None
    assert timeline.history == (OLD,)
    later = build([NEW, ID, OLD], now_ms=1_300_000, delay_s=7)
    assert later.history == (OLD,)


def test_nothing_heard_yet() -> None:
    timeline = build([NEW], now_ms=1_000_000, delay_s=7)
    assert timeline.audible is None
    assert timeline.current is None
    assert timeline.upcoming_in_s == 207
    assert build([], now_ms=0, delay_s=7).upcoming is None
