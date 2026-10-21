"""Rate limits for logins and setup: per-key failure counts with a lock,
and a global pace; Once thins out repeated warnings. Keys are address
groups, so one IPv6 host cannot rotate through its /64 to get fresh
counters."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable


def group(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def network(address: str) -> str:
    """The range a ban on this address covers, as CIDR text: the address
    itself for IPv4, its /64 for IPv6, the same unit group() counts in."""
    grouped = group(address)
    try:
        return str(ipaddress.ip_network(grouped if "/" in grouped else f"{grouped}/32"))
    except ValueError:
        return address


class _Entry:
    __slots__ = ("attempts", "locked_until")

    def __init__(self) -> None:
        self.attempts: deque[float] = deque()
        self.locked_until = 0.0


class FailureLimit:
    """Each attempt is counted when it is reserved, before it is checked,
    so concurrent attempts cannot all slip past the count; a success gives
    its reservation back."""

    def __init__(
        self,
        limit: int,
        window_s: float,
        lock_s: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window_s
        self._lock_s = lock_s
        self._clock = clock
        self._max = max_keys
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _drop_idle(self, now: float) -> None:
        """Drops the least recently used entries that hold nothing: no
        attempt in the window and no lock. Stops at the first that still
        holds something, so each call costs little."""
        while self._entries:
            key, oldest = next(iter(self._entries.items()))
            recent = oldest.attempts and oldest.attempts[-1] > now - self._window
            if recent or now < oldest.locked_until:
                return
            del self._entries[key]

    def reserve(self, key: str) -> Callable[[], None] | None:
        """A function that gives the attempt back on success, or None when
        the key is locked."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._drop_idle(now)
                entry = self._entries[key] = _Entry()
                while len(self._entries) > self._max:
                    self._entries.popitem(last=False)
            self._entries.move_to_end(key)
            while entry.attempts and entry.attempts[0] <= now - self._window:
                entry.attempts.popleft()
            if now < entry.locked_until:
                return None
            if len(entry.attempts) >= self._limit:
                entry.locked_until = now + self._lock_s
                return None
            entry.attempts.append(now)

        def release() -> None:
            with self._lock:
                if now in entry.attempts:
                    entry.attempts.remove(now)

        return release


class Pace:
    """At most one allowed call per interval, shared by every caller."""

    def __init__(self, interval_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._next = 0.0

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            if now < self._next:
                return False
            self._next = now + self._interval
            return True


class Once:
    """True for the first call per address group in each interval, for a
    warning anyone can trigger: the rest go to a quieter level."""

    def __init__(
        self,
        interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._max = max_keys
        self._lock = threading.Lock()
        self._until: OrderedDict[str, float] = OrderedDict()

    def first(self, address: str) -> bool:
        key = group(address)
        now = self._clock()
        with self._lock:
            if self._until.get(key, 0.0) > now:
                return False
            self._until[key] = now + self._interval
            self._until.move_to_end(key)
            while len(self._until) > self._max:
                self._until.popitem(last=False)
            return True
