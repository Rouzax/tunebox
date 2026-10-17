import threading
from collections.abc import Iterator

import pytest

from tunebox import http
from tunebox.bursts import MAX_AGE_S, RETRY_S, WINDOW_S, Bursts
from tunebox.config import StreamConfig
from tunebox.http import HttpError

AAC = StreamConfig("Standard", "https://example.invalid/aac", "aac", 128)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def live_stream(clock: FakeClock, burst_s: float, kbps: int, connect_s: float = 0.7):
    """A stream that sends burst_s seconds at once, then plays in real time."""
    rate = kbps * 125

    def open_stream(_url: str) -> Iterator[bytes]:
        clock.now += connect_s
        yield b"x" * int(burst_s * rate)
        while True:
            clock.now += 0.25
            yield b"x" * int(0.25 * rate)

    return open_stream


def test_measures_the_connect_burst() -> None:
    clock = FakeClock()
    bursts = Bursts(live_stream(clock, 32.0, 128), clock)
    assert bursts.measure(AAC) == pytest.approx(32.0, abs=0.3)


def test_a_stream_without_a_burst_measures_zero() -> None:
    clock = FakeClock()
    bursts = Bursts(live_stream(clock, 0.0, 128), clock)
    assert bursts.measure(AAC) == pytest.approx(0.0, abs=0.3)


def test_reading_stops_after_the_window() -> None:
    clock = FakeClock()
    bursts = Bursts(live_stream(clock, 10.0, 128), clock)
    bursts.measure(AAC)
    assert clock.now - 1000.0 <= 0.7 + WINDOW_S + 0.25


def test_a_stream_without_kbps_is_not_measured() -> None:
    def never(_url: str) -> Iterator[bytes]:
        raise AssertionError("opened")

    bursts = Bursts(never, FakeClock())
    assert bursts.measure(StreamConfig("x", "https://example.invalid/x", None, None)) is None


def test_a_failed_or_empty_stream_measures_nothing() -> None:
    def broken(_url: str) -> Iterator[bytes]:
        raise HttpError("down")

    def empty(_url: str) -> Iterator[bytes]:
        return iter(())

    assert Bursts(broken, FakeClock()).measure(AAC) is None
    assert Bursts(empty, FakeClock()).measure(AAC) is None


def test_the_stream_is_closed_after_reading() -> None:
    clock = FakeClock()
    closed: list[bool] = []

    def open_stream(_url: str) -> Iterator[bytes]:
        try:
            yield from live_stream(clock, 5.0, 128)(_url)
        finally:
            closed.append(True)

    Bursts(open_stream, clock).measure(AAC)
    assert closed == [True]


def test_unknown_streams_count_as_no_burst() -> None:
    bursts = Bursts(live_stream(FakeClock(), 5.0, 128), FakeClock())
    assert bursts.burst_s(AAC.url) == 0.0


def test_a_requested_stream_is_measured_by_the_worker() -> None:
    clock = FakeClock()
    bursts = Bursts(live_stream(clock, 20.0, 128), clock)
    bursts.request(AAC)
    assert bursts.work_once()
    assert bursts.burst_s(AAC.url) == pytest.approx(20.0, abs=0.3)
    assert not bursts.work_once()


def test_a_stream_is_measured_at_most_once_a_day() -> None:
    clock = FakeClock()
    opened: list[str] = []
    inner = live_stream(clock, 10.0, 128)

    def counting(url: str) -> Iterator[bytes]:
        opened.append(url)
        return inner(url)

    bursts = Bursts(counting, clock)
    bursts.request(AAC)
    bursts.request(AAC)
    bursts.work_once()
    bursts.request(AAC)
    assert not bursts.work_once()
    clock.now += MAX_AGE_S
    bursts.request(AAC)
    assert bursts.work_once()
    assert len(opened) == 2


def test_a_failed_measurement_keeps_the_last_value_and_retries_later() -> None:
    clock = FakeClock()
    fail = [False]
    inner = live_stream(clock, 12.0, 128)

    def flaky(url: str) -> Iterator[bytes]:
        if fail[0]:
            raise HttpError("down")
        return inner(url)

    bursts = Bursts(flaky, clock)
    bursts.request(AAC)
    bursts.work_once()
    fail[0] = True
    clock.now += MAX_AGE_S
    bursts.request(AAC)
    bursts.work_once()
    assert bursts.burst_s(AAC.url) == pytest.approx(12.0, abs=0.3)
    bursts.request(AAC)
    assert not bursts.work_once()
    clock.now += RETRY_S
    bursts.request(AAC)
    assert bursts.work_once()


def test_run_forever_stops() -> None:
    clock = FakeClock()
    bursts = Bursts(live_stream(clock, 3.0, 128), clock)
    stop = threading.Event()
    worker = threading.Thread(target=bursts.run_forever, args=(stop,), daemon=True)
    worker.start()
    bursts.request(AAC)
    for _ in range(200):
        if bursts.burst_s(AAC.url):
            break
        stop.wait(0.01)
    stop.set()
    worker.join(2)
    assert not worker.is_alive()
    assert bursts.burst_s(AAC.url) == pytest.approx(3.0, abs=0.3)


@pytest.mark.live
def test_live_triton_mounts_send_a_burst() -> None:
    base = "https://playerservices.streamtheworld.com/api/livestream-redirect/"
    bursts = Bursts(http.stream)
    aac = bursts.measure(StreamConfig("Standard", base + "OWR_INTERNATIONALAAC.aac", "aac", 128))
    mp3 = bursts.measure(StreamConfig("Compatible", base + "OWR_INTERNATIONAL.mp3", "mp3", 192))
    assert aac is not None and aac > 15
    assert mp3 is not None and mp3 > 5
