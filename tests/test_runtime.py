import dataclasses
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeHttp, FakeSource, NoJitter
from tunebox import runtime as runtime_module
from tunebox.blobs import Blobs
from tunebox.channel import Channel
from tunebox.config import ChannelConfig, ChannelRow, ConfigState, Resolved
from tunebox.covers import CoverService
from tunebox.logos import Logos
from tunebox.runtime import LIVE_FIELDS, POLLER_FIELDS, Runtime, needs_restart
from tunebox.settings import Settings
from tunebox.store import Store


def doc(channel_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": channel_id,
        "name": channel_id.upper(),
        "stream_url": "https://example.invalid/stream.aac",
        "source": {"type": "triton", "mount": "OWR_INTERNATIONAL"},
        **extra,
    }


def rows(*docs: dict[str, Any]) -> ConfigState:
    return ConfigState(channels=tuple(ChannelRow(d["id"], None, True, d) for d in docs))


class RecordingLogos(Logos):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory, FakeHttp(lambda _url: b""), Blobs(directory.parent / "blobs"))
        self.requested: list[str | None] = []

    def request(self, channel: ChannelConfig) -> None:
        self.requested.append(channel.logo)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(tmp_path / "t.db")
        self.store = Store(None)
        self.covers = CoverService(self.store, lambda _track: None)
        self.made: list[tuple[str, Channel | None]] = []
        self.unbuildable: set[str] = set()
        self.logos = RecordingLogos(tmp_path / "logos")
        self.runtime = Runtime(self.settings, self.make, self.logos)

    def make(self, config: ChannelConfig, carry: Channel | None) -> Channel:
        if config.id in self.unbuildable:
            raise ValueError("cannot build this source")
        self.made.append((config.id, carry))
        return Channel(config, FakeSource(), self.covers, self.store, rng=NoJitter(), carry=carry)

    def live_threads(self) -> list[threading.Thread]:
        return [entry.thread for entry in self.runtime._live.values()]  # pyright: ignore[reportPrivateUsage]

    def commit(self, state: ConfigState) -> int:
        _, version = self.settings.load()
        return self.settings.commit(state, version, "test")


@pytest.fixture
def harness(tmp_path: Path) -> Generator[Harness]:
    made = Harness(tmp_path)
    yield made
    made.runtime.stop_and_join(time.monotonic() + 5)
    made.settings.close()


def test_every_channel_field_is_classified() -> None:
    names = {f.name for f in dataclasses.fields(ChannelConfig)}
    assert names == POLLER_FIELDS | LIVE_FIELDS
    assert not POLLER_FIELDS & LIVE_FIELDS


def test_apply_starts_swaps_restarts_and_stops(harness: Harness) -> None:
    harness.commit(rows(doc("a"), doc("b")))
    assert harness.runtime.apply() == 1
    first = harness.runtime.snapshot
    assert list(first.channels) == ["a", "b"]
    a, b = first.channels["a"], first.channels["b"]

    # a: display change, swapped in place; b: poll change, restarted; c: new.
    harness.commit(rows(doc("a", name="Renamed"), doc("b", poll_interval_s=30), doc("c")))
    assert harness.runtime.apply() == 2
    second = harness.runtime.snapshot
    assert second.channels["a"] is a
    assert a.config.name == "Renamed"
    assert second.channels["b"] is not b
    assert ("b", b) in harness.made
    assert list(second.channels) == ["a", "b", "c"]

    harness.commit(rows(doc("c")))
    harness.runtime.apply()
    assert list(harness.runtime.snapshot.channels) == ["c"]


def test_reverting_after_a_failed_apply_restarts_the_retired_channel(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = rows(doc("a"), doc("b"))
    harness.commit(first)
    assert harness.runtime.apply() == 1
    real_start = threading.Thread.start

    def refuse(thread: threading.Thread) -> None:
        if thread.name == "channel-c":
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    harness.commit(rows(doc("a"), doc("b", poll_interval_s=30), doc("c")))
    assert harness.runtime.apply() == 1
    monkeypatch.setattr(threading.Thread, "start", real_start)
    harness.commit(first)
    assert harness.runtime.apply() == 3
    for entry in harness.runtime._live.values():  # pyright: ignore[reportPrivateUsage]
        assert not entry.stop.is_set()
        assert entry.thread.is_alive()
    for thread in threading.enumerate():
        if thread.name.startswith("channel-") and thread not in harness.live_threads():
            thread.join(5)
    names = [t.name for t in threading.enumerate() if t.name.startswith("channel-")]
    assert sorted(names) == ["channel-a", "channel-b"]
    assert set(harness.runtime.snapshot.channels) == {"a", "b"}


def test_apply_without_a_new_version_does_nothing(harness: Harness) -> None:
    assert harness.runtime.apply() == 0
    harness.commit(rows(doc("a")))
    harness.runtime.apply()
    made = len(harness.made)
    assert harness.runtime.apply() == 1
    assert len(harness.made) == made


def test_only_new_or_changed_logos_are_requested(harness: Harness) -> None:
    harness.commit(rows(doc("a", logo="https://example.invalid/a.png")))
    harness.runtime.apply()
    harness.commit(rows(doc("a", logo="https://example.invalid/a.png", name="X")))
    harness.runtime.apply()
    harness.commit(rows(doc("a", logo="https://example.invalid/b.png")))
    harness.runtime.apply()
    assert harness.logos.requested == [
        "https://example.invalid/a.png",
        "https://example.invalid/b.png",
    ]


def test_requests_during_apply_never_fail(harness: Harness) -> None:
    harness.commit(rows(*(doc(f"c{i}") for i in range(20))))
    harness.runtime.apply()
    failures: list[BaseException] = []
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            try:
                time.sleep(0)
                for channel in harness.runtime.snapshot.channels.values():
                    channel.info()
            except Exception as error:  # noqa: BLE001 - any failure fails the test
                failures.append(error)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for size in (5, 20, 1, 20):
        harness.commit(rows(*(doc(f"c{i}") for i in range(size))))
        harness.runtime.apply()
    done.set()
    for thread in threads:
        thread.join(5)
    assert failures == []


def test_needs_restart_only_for_poller_fields(harness: Harness) -> None:
    harness.commit(rows(doc("a")))
    harness.runtime.apply()
    config = harness.runtime.snapshot.channels["a"].config
    assert not needs_restart(config, dataclasses.replace(config, name="X", delay_s=9))
    assert needs_restart(config, dataclasses.replace(config, history_size=10))


def test_a_stored_state_that_no_longer_validates_keeps_what_runs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.commit(rows(doc("a")))
    harness.runtime.apply()
    # Written behind the validator's back, as a future schema change could.
    harness.settings._db.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE channels SET doc_json = '{}'"
    )
    harness.settings._db.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE config_version SET version = 2"
    )
    assert harness.runtime.apply() == 1
    assert list(harness.runtime.snapshot.channels) == ["a"]
    assert "apply_failed" in caplog.text


def test_stop_and_join_does_not_wait_out_the_idle_check(harness: Harness) -> None:
    harness.commit(rows(doc("a")))
    harness.runtime.apply()
    time.sleep(0.1)  # the poller is now in its idle wait
    started = time.monotonic()
    harness.runtime.stop_and_join(time.monotonic() + 5)
    assert time.monotonic() - started < 0.5


def test_a_failed_apply_keeps_the_running_channels(harness: Harness) -> None:
    harness.commit(rows(doc("owr")))
    assert harness.runtime.apply() == 1
    before = dict(harness.runtime.snapshot.channels)
    harness.made.clear()
    harness.unbuildable.add("bad")
    harness.commit(rows(doc("owr", poll_interval_s=30), doc("new"), doc("bad")))
    assert harness.runtime.apply() == 1
    assert dict(harness.runtime.snapshot.channels) == before
    assert [made for made, _carry in harness.made] == ["owr", "new"]
    assert harness.runtime.apply() == 1


def test_a_thread_that_cannot_start_keeps_the_running_channels(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.commit(rows(doc("a"), doc("b")))
    assert harness.runtime.apply() == 1
    before = dict(harness.runtime.snapshot.channels)
    real_start = threading.Thread.start
    started: list[threading.Thread] = []

    def refuse(thread: threading.Thread) -> None:
        if thread.name == "channel-c":
            raise RuntimeError("can't start new thread")
        real_start(thread)
        started.append(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    harness.commit(rows(doc("a"), doc("b", poll_interval_s=30), doc("c")))
    assert harness.runtime.apply() == 1
    assert dict(harness.runtime.snapshot.channels) == before
    assert [thread.name for thread in started] == ["channel-b"]
    for thread in started:
        thread.join(5)
        assert not thread.is_alive()
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert harness.runtime.apply() == 2
    assert set(harness.runtime.snapshot.channels) == {"a", "b", "c"}
    for thread in threading.enumerate():
        if thread.name.startswith("channel-") and thread not in harness.live_threads():
            thread.join(5)
    live = [t for t in threading.enumerate() if t.name.startswith("channel-")]
    assert sorted(t.name for t in live) == ["channel-a", "channel-b", "channel-c"]


def test_apply_without_a_new_version_skips_the_load(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.commit(rows(doc("a")))
    harness.runtime.apply()
    calls: list[int] = []
    real = runtime_module.validate_state

    def counted(state: ConfigState) -> Resolved:
        calls.append(1)
        return real(state)

    monkeypatch.setattr(runtime_module, "validate_state", counted)
    assert harness.runtime.apply() == 1
    assert calls == []


def test_apply_recovers_once_the_bad_source_is_fixed(harness: Harness) -> None:
    harness.commit(rows(doc("owr")))
    assert harness.runtime.apply() == 1
    harness.unbuildable.add("bad")
    harness.commit(rows(doc("owr"), doc("bad")))
    assert harness.runtime.apply() == 1
    harness.unbuildable.clear()
    assert harness.runtime.apply() == 2
    assert set(harness.runtime.snapshot.channels) == {"owr", "bad"}
    assert harness.runtime.snapshot.version == 2
