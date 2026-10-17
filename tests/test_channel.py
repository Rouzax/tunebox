import logging

import pytest

from tests.fakes import FakeSource, NoJitter, channel_config
from tunebox.catalogs import Found
from tunebox.channel import FIRST_FETCH, STEADY_FETCH, Channel
from tunebox.config import ChannelConfig
from tunebox.covers import CoverService
from tunebox.sources.base import SourceError
from tunebox.store import Store
from tunebox.tracks import RawEntry, Track

OLD = RawEntry("FISHER", "Stay", 1_000_000)
NEW = RawEntry("JAZZY", "My People", 1_200_000)
ID = RawEntry("", "E1700 16 Oct 2026 M19 VO", 1_100_000)
M_URL = "https://example.invalid/m.jpg"
S_URL = "https://example.invalid/s.jpg"


class Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class Finder:
    def __init__(self) -> None:
        self.covers: dict[str, str] = {}
        self.calls: list[str] = []

    def __call__(self, track: Track) -> Found | None:
        self.calls.append(track.title)
        url = self.covers.get(track.title)
        return Found(url, "album", "test") if url else None


def make(
    source: FakeSource,
    clock: Clock,
    config: ChannelConfig | None = None,
    store: Store | None = None,
) -> tuple[Channel, CoverService, Finder]:
    finder = Finder()
    store = store or Store(None)
    covers = CoverService(store, finder, clock=lambda: clock.ms / 1000)
    channel = Channel(
        config or channel_config(), source, covers, store, clock_ms=clock, rng=NoJitter()
    )
    return channel, covers, finder


def drain(covers: CoverService) -> None:
    while covers.run_once():
        pass


def test_first_poll_fetches_more_than_later_polls() -> None:
    source = FakeSource()
    channel, _, _ = make(source, Clock(1_300_000))
    assert channel.poll_once() == 12.0
    channel.poll_once()
    assert source.counts == [FIRST_FETCH, STEADY_FETCH]


def test_merge_dedupes_sorts_and_caps() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD], [NEW], [RawEntry("PEGASSI", "Yoyoyo", 1_400_000)]]
    channel, _, _ = make(source, Clock(2_000_000), channel_config(history_size=2))
    for _ in range(3):
        channel.poll_once()
    body, _ = channel.now_view()
    assert body["current"]["title"] == "Yoyoyo"
    assert [t["title"] for t in body["history"]] == ["My People"]


def test_now_view_applies_the_delay() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    channel, _, _ = make(source, Clock(1_203_000))
    channel.poll_once()
    body, _ = channel.now_view()
    assert body["current"]["title"] == "Stay"
    assert body["upcoming"]["track"]["title"] == "My People"
    assert body["upcoming"]["in_s"] == 4
    assert body["history"] == []
    assert body["stale_since"] is None
    keys = {"artist", "artists", "title", "played_by", "started_at", "cover", "cover_kind"}
    assert set(body["current"]) == keys


def test_station_id_shows_as_no_current_and_null_upcoming_track() -> None:
    source = FakeSource()
    source.responses = [[NEW, ID, OLD]]
    clock = Clock(1_150_000)
    channel, _, _ = make(source, clock)
    channel.poll_once()
    body, _ = channel.now_view()
    assert body["current"] is None
    assert body["upcoming"]["track"]["title"] == "My People"
    clock.ms = 1_105_000
    body, _ = channel.now_view()
    assert body["upcoming"]["track"] is None


def test_etag_ignores_the_countdown_but_not_a_change() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    clock = Clock(1_201_000)
    channel, covers, finder = make(source, clock)
    channel.poll_once()
    _, first = channel.now_view()
    clock.ms = 1_204_000
    _, same = channel.now_view()
    assert same == first
    clock.ms = 1_208_000
    _, switched = channel.now_view()
    assert switched != first
    finder.covers["My People"] = M_URL
    drain(covers)
    _, covered = channel.now_view()
    assert covered != switched


def test_failures_back_off_mark_stale_and_recover(caplog: pytest.LogCaptureFixture) -> None:
    source = FakeSource()
    down = SourceError("down")
    source.responses = [[OLD], down, down, down, [OLD]]
    channel, _, _ = make(source, Clock(1_300_000))
    channel.poll_once()
    with caplog.at_level(logging.INFO, logger="tunebox.channel"):
        assert channel.poll_once() == 24.0
        assert channel.poll_once() == 48.0
        assert channel.poll_once() == 96.0
        assert channel.now_view()[0]["stale_since"] == 1_300_000
        assert channel.now_view()[0]["current"]["title"] == "Stay"
        channel.poll_once()
    events = [r.getMessage() for r in caplog.records]
    assert events.count("source_stale") == 1
    assert events.count("source_recovered") == 1
    assert channel.now_view()[0]["stale_since"] is None


def test_backoff_is_capped() -> None:
    source = FakeSource()
    source.responses = [SourceError("down")] * 6
    channel, _, _ = make(source, Clock(0))
    delays = [channel.poll_once() for _ in range(6)]
    assert delays[-1] == 120.0


def test_empty_answer_keeps_what_the_channel_had() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD], []]
    channel, _, _ = make(source, Clock(1_300_000))
    channel.poll_once()
    channel.poll_once()
    body, _ = channel.now_view()
    assert body["current"]["title"] == "My People"
    assert body["stale_since"] is None


def test_untimed_entries_are_not_added_twice() -> None:
    source = FakeSource()
    untimed = RawEntry("FISHER", "Stay", None)
    source.responses = [[untimed], [untimed], [RawEntry("JAZZY", "My People", None)]]
    clock = Clock(1_000_000)
    channel, _, _ = make(source, clock, channel_config(delay_s=0))
    for step in range(3):
        clock.ms = 1_000_000 + step * 10_000
        channel.poll_once()
    body, _ = channel.now_view()
    assert body["current"]["title"] == "My People"
    assert [t["title"] for t in body["history"]] == ["Stay"]


def test_cover_lookups_put_now_first_and_cap_history() -> None:
    source = FakeSource()
    entries = [RawEntry(f"ARTIST {i}", f"Song {i}", 1_000_000 + i * 1000) for i in range(40)]
    source.responses = [list(reversed(entries))]
    channel, covers, finder = make(source, Clock(1_000_000 + 39 * 1000 + 3000))
    channel.poll_once()
    drain(covers)
    # Not yet heard (36-39) first, newest first; then audible (35) and PawTunes'
    # audible (14); then only the newest 20 history entries (34 down to 15).
    assert finder.calls[:4] == ["Song 39", "Song 38", "Song 37", "Song 36"]
    assert len(finder.calls) == 4 + 1 + 1 + 20
    assert "Song 0" not in finder.calls


def test_recent_list_survives_a_restart() -> None:
    store = Store(None)
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    channel, _, _ = make(source, Clock(1_300_000), store=store)
    channel.poll_once()
    restarted, _, _ = make(FakeSource(), Clock(1_300_000), store=store)
    assert restarted.now_view()[0]["current"]["title"] == "My People"


def test_pawtunes_view_uses_its_own_delay_and_full_title() -> None:
    source = FakeSource()
    played = RawEntry("NOIZU", "Moon Groove (Played by Noizu Library TML 22)", 1_200_000)
    source.responses = [[played, OLD]]
    clock = Clock(1_210_000)
    channel, _, _ = make(source, clock)
    channel.poll_once()
    assert channel.now_view()[0]["current"]["title"] == "Moon Groove"
    assert channel.pawtunes_view()["title"] == "Stay"
    clock.ms = 1_230_000
    body = channel.pawtunes_view()
    assert body["title"] == "Moon Groove (Played by Noizu Library TML 22)"
    assert body["artist"] == "NOIZU"
    assert [h["title"] for h in body["history"]] == [body["title"], "Stay"]
    assert body["history"][0]["played_at"] == 1_200_000


def test_pawtunes_artwork_stays_omitted_once_omitted() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    channel, covers, finder = make(source, Clock(1_300_000))
    channel.poll_once()
    assert "artwork" not in channel.pawtunes_view()
    finder.covers["My People"] = M_URL
    finder.covers["Stay"] = S_URL
    drain(covers)
    body = channel.pawtunes_view()
    assert "artwork" not in body
    assert body["history"][1]["artwork"] == S_URL


def test_pawtunes_artwork_stays_once_sent() -> None:
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    clock = Clock(1_100_000)
    channel, covers, finder = make(source, clock)
    finder.covers["My People"] = M_URL
    channel.poll_once()
    drain(covers)
    clock.ms = 1_300_000
    assert channel.pawtunes_view()["artwork"] == M_URL
    assert channel.pawtunes_view()["artwork"] == M_URL


def test_pawtunes_station_id_and_empty_channel() -> None:
    empty, _, _ = make(FakeSource(), Clock(0))
    assert empty.pawtunes_view() == {"artist": "", "title": "One World Radio", "history": []}
    source = FakeSource()
    source.responses = [[ID, OLD]]
    channel, _, _ = make(source, Clock(1_200_000))
    channel.poll_once()
    body = channel.pawtunes_view()
    assert body["artist"] == ""
    assert body["title"] == "E1700 16 Oct 2026 M19 VO"
    assert [h["title"] for h in body["history"]] == ["Stay"]


def test_pawtunes_history_is_capped_at_twenty() -> None:
    source = FakeSource()
    entries = [RawEntry(f"A{i}", f"S{i}", 1_000_000 + i * 1000) for i in reversed(range(30))]
    source.responses = [entries]
    channel, _, _ = make(source, Clock(2_000_000))
    channel.poll_once()
    assert len(channel.pawtunes_view()["history"]) == 20


def test_info_and_status() -> None:
    channel, _, _ = make(FakeSource(), Clock(5_000))
    assert channel.info() == {
        "id": "owr",
        "name": "One World Radio",
        "stream_url": "https://example.invalid/stream.aac",
        "default_image": None,
    }
    assert channel.status() == {"last_poll_ok": None, "stale_since": None}
    channel.poll_once()
    assert channel.status()["last_poll_ok"] == 5_000


def test_player_buffer_turns_the_newest_track_into_upcoming() -> None:
    # The feed lists a track about 20 s after it started, so with the server
    # delay alone the newest track is already heard by the time it is seen.
    source = FakeSource()
    source.responses = [[NEW, OLD]]
    channel, _, _ = make(source, Clock(1_222_000))
    channel.poll_once()
    assert channel.now_view()[0]["upcoming"] is None
    body, _ = channel.now_view(buffer_s=30)
    assert body["current"]["title"] == "Stay"
    assert body["upcoming"]["track"]["title"] == "My People"
    assert body["upcoming"]["in_s"] == 15
