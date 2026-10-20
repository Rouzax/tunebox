"""Request policy for the admin panel: how often a login may be tried, from
where and how fast, the scrypt slots, the known-browser lane, authenticator
secrets waiting for their first code, and the time floor that keeps a
login's answer from telling a right password from a wrong one. Nothing here
touches the database; the account and its sessions live in accounts.py."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Collection, Generator

from tunebox import crypto
from tunebox.limits import FailureLimit, Pace, group

KNOWN_S = 365 * 86_400
KNOWN_PURPOSE = "known-browser"
# Every login answer after the password check takes at least this long: a
# right password with a wrong code writes a counter, a wrong one does not.
LOGIN_FLOOR_S = 0.3
# scrypt takes about 33 MB and 0.1 s; more at once is a way to run tunebox
# out of memory.
HASHING_SLOTS = 2


class Gate:
    """Safe to share between threads."""

    def __init__(
        self,
        key: Callable[[], bytes],
        plain_http: bool,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = key
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        # A grant read on the network over plain http must not give a lane
        # once the panel moves to https.
        self._version = "h2" if plain_http else "k2"
        self._failures = FailureLimit(5, window_s=900, lock_s=900, clock=monotonic)
        self._pace = Pace(1.0, clock=monotonic)
        self._known_pace = Pace(1.0, clock=monotonic)
        self._hashing = threading.BoundedSemaphore(HASHING_SLOTS)
        self._lock = threading.Lock()
        # New authenticator secrets waiting for their first code, by session.
        self._totp: dict[str, bytes] = {}
        # The secret the setup page shows, until the account is made.
        self._setup: bytes | None = None

    def reserve(self, client: str) -> Callable[[], None] | None:
        return self._failures.reserve(group(client))

    def pace(self, known: bool) -> bool:
        return (self._known_pace if known else self._pace).allow()

    @contextlib.contextmanager
    def hashing(self) -> Generator[bool]:
        """True when a scrypt slot was free and is held until the block ends."""
        took = self._hashing.acquire(blocking=False)
        try:
            yield took
        finally:
            if took:
                self._hashing.release()

    def known_grant(self, generation: int) -> str:
        expiry = int(self._clock() + KNOWN_S)
        return crypto.sign(self._key(), f"{self._version}.{generation}.{expiry}", KNOWN_PURPOSE)

    def known(self, value: str | None, generation: int) -> bool:
        """A browser that logged in before, by a signed cookie naming the
        current account generation."""
        payload = crypto.unsign(self._key(), value, KNOWN_PURPOSE) if value else None
        if payload is None:
            return False
        try:
            version, given, expiry = payload.split(".")
            return (
                version == self._version
                and int(given) == generation
                and int(expiry) > self._clock()
            )
        except ValueError:
            return False

    def hold_totp(self, token_hash: str, secret: bytes) -> None:
        with self._lock:
            self._totp[token_hash] = secret

    def take_totp(self, token_hash: str) -> bytes | None:
        """The waiting secret; it stays until forgotten or dropped."""
        with self._lock:
            return self._totp.get(token_hash)

    def forget_totp(self, token_hash: str) -> None:
        with self._lock:
            self._totp.pop(token_hash, None)

    def drop_totp(self, keep: Collection[str]) -> None:
        with self._lock:
            self._totp = {k: v for k, v in self._totp.items() if k in keep}

    def setup_secret(self) -> bytes:
        """The setup secret, made on first ask: one per setup token, so a
        reload, or two starts at once, show the secret already scanned."""
        with self._lock:
            if self._setup is None:
                self._setup = crypto.new_totp_secret()
            return self._setup

    def setup_secret_shown(self) -> bytes | None:
        with self._lock:
            return self._setup

    def end_setup(self) -> None:
        with self._lock:
            self._setup = None

    def floor(self, started: float) -> None:
        left = started + LOGIN_FLOOR_S - self._monotonic()
        if left > 0:
            self._sleep(left)
