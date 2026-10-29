"""Pacing for panel actions that make tunebox reach outside on the owner's
behalf (stream and feed checks, cover searches): one at a time, a number a
minute, and a refusal that says how long to wait rather than a wait."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager


class Busy(Exception):
    """Another action of the same pacer runs (running), or the minute's are
    used up; retry_s is when one can run again. The text is the error name
    the panel shows."""

    def __init__(self, error: str, running: bool, retry_s: int) -> None:
        super().__init__(error)
        self.running = running
        self.retry_s = retry_s


class Pacer:
    def __init__(
        self,
        per_minute: int,
        running: str,
        used_up: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_minute = per_minute
        self._running = running
        self._used_up = used_up
        self._clock = clock
        # One lock over the running flag, the minute's actions and when the
        # running one must be done, so a refusal reads all three as one moment.
        self._lock = threading.Lock()
        self._busy = False
        self._times: deque[float] = deque()
        self._ends = 0.0

    @contextmanager
    def slot(self, bound_s: float) -> Generator[None]:
        """bound_s: the longest this action can hold the slot; one refused
        meanwhile is told to wait what is left of it."""
        with self._lock:
            now = self._clock()
            if self._busy:
                raise Busy(self._running, True, max(1, math.ceil(self._ends - now)))
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            if len(self._times) >= self._per_minute:
                wait = max(1, math.ceil(60 - (now - self._times[0])))
                raise Busy(self._used_up, False, wait)
            self._times.append(now)
            self._busy = True
            self._ends = now + bound_s
        try:
            yield
        finally:
            with self._lock:
                self._busy = False
