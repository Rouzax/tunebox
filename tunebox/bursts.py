"""How much past audio each stream sends on connect.

A Triton mount answers a new listener with a burst of the last seconds of
audio before it carries on live, so a player starts that far behind live.
The player measures what it adds to that itself (time since it asked for
the stream minus the position it has played to), but cannot see the burst:
Chromium keeps only a couple of seconds of the downloaded audio as
media.buffered. tunebox measures it here, once a day per stream a player
uses, and adds it to the player's buffer."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator

from tunebox.config import StreamConfig
from tunebox.http import HttpError
from tunebox.logs import event

log = logging.getLogger(__name__)

# Long enough for the burst to arrive in full, which it does in the first
# second even on a slow link.
WINDOW_S = 3.0
MAX_AGE_S = 86_400.0
RETRY_S = 600.0

StreamOpen = Callable[[str], Iterator[bytes]]


class Bursts:
    """Request threads ask and read; one worker thread measures."""

    def __init__(
        self, open_stream: StreamOpen, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._open = open_stream
        self._clock = clock
        self._lock = threading.Lock()
        self._burst: dict[str, float] = {}
        # When each URL may be measured again; set when it is queued, so a
        # second request in the meantime queues nothing.
        self._next: dict[str, float] = {}
        self._queue: queue.Queue[StreamConfig] = queue.Queue()

    def burst_s(self, url: str) -> float:
        """The last measured burst, 0 until there is one."""
        with self._lock:
            return self._burst.get(url, 0.0)

    def request(self, stream: StreamConfig) -> None:
        """Queues a measurement unless the stream had one recently."""
        if not stream.kbps:
            return
        now = self._clock()
        with self._lock:
            if now < self._next.get(stream.url, now):
                return
            self._next[stream.url] = float("inf")
        self._queue.put(stream)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.work_once(timeout_s=0.5)

    def work_once(self, timeout_s: float = 0.0) -> bool:
        """Measures one queued stream; returns whether there was one."""
        try:
            stream = self._queue.get(timeout=timeout_s) if timeout_s else self._queue.get_nowait()
        except queue.Empty:
            return False
        burst = self.measure(stream)
        now = self._clock()
        with self._lock:
            if burst is None:
                self._next[stream.url] = now + RETRY_S
            else:
                self._burst[stream.url] = burst
                self._next[stream.url] = now + MAX_AGE_S
        return True

    def measure(self, stream: StreamConfig) -> float | None:
        """Reads the stream for WINDOW_S after its first byte. Bytes beyond
        what that time plays at the stream's rate came in the burst."""
        if not stream.kbps:
            return None
        rate = stream.kbps * 125
        received = 0
        first: float | None = None
        elapsed = 0.0
        chunks: Iterator[bytes] | None = None
        try:
            chunks = self._open(stream.url)
            for chunk in chunks:
                if first is None:
                    first = self._clock()
                received += len(chunk)
                elapsed = self._clock() - first
                if elapsed >= WINDOW_S:
                    break
        except HttpError as error:
            event(log, logging.WARNING, "burst_failed", stream=stream.url, error=str(error))
            return None
        finally:
            # A generator holds the connection open until it is closed.
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
        if first is None:
            event(log, logging.WARNING, "burst_failed", stream=stream.url, error="no data")
            return None
        burst = round(max(0.0, received / rate - elapsed), 1)
        event(log, logging.INFO, "burst_measured", stream=stream.url, burst_s=burst, bytes=received)
        return burst
