"""The listening PIN. In "site" mode it is a door in front of the whole
player for strangers who find its address; in "stations" mode the site is
open and only the stations marked as needing it stay hidden until a device
types it. A correct PIN gives the device a signed cookie, checked on every
request without the database. The PIN guards tunebox, not the audio: the
streams are the stations' own public addresses."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from tunebox import crypto
from tunebox.limits import FailureLimit, group
from tunebox.settings import Settings

MODES = ("off", "site", "stations")
GRANT_COOKIE = "tunebox_grant"
GRANT_S = 365 * 86_400
PIN_MIN = 6
PIN_MAX = 8
FAIL_LIMIT = 10
FAIL_WINDOW_S = 900
LOCK_S = 900
# Many addresses each guessing a few times: past this many failures in the
# window, every attempt waits, which caps guessing for everyone together.
FLOOD = 100
FLOOD_WINDOW_S = 3600
FLOOD_DELAY_S = 2.0
_GRANT_VERSION = "p1"


class PinError(Exception):
    pass


@dataclass(frozen=True)
class PinStatus:
    mode: str
    set: bool


@dataclass(frozen=True)
class Attempt:
    ok: bool
    # The whole Set-Cookie value for the grant, when ok.
    cookie: str | None = None
    reason: Literal["wrong", "locked", "off"] | None = None


@dataclass(frozen=True)
class _State:
    mode: str
    digest: str | None
    generation: int


def valid_pin(text: str) -> bool:
    return PIN_MIN <= len(text) <= PIN_MAX and text.isascii() and text.isdigit()


def read_cookie(header: str | None, name: str) -> str | None:
    """Read pair by pair: SimpleCookie drops every cookie when one cookie of
    another app on the host is malformed."""
    for pair in (header or "").split(";"):
        key, sep, value = pair.strip().partition("=")
        if sep and key == name:
            return value
    return None


class Pin:
    """Safe to share between threads: the state is one immutable value,
    replaced whole under a lock when it changes."""

    def __init__(
        self,
        settings: Settings,
        key: Callable[[], bytes],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._key = key
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._failures = FailureLimit(FAIL_LIMIT, FAIL_WINDOW_S, LOCK_S, clock=monotonic)
        self._recent: deque[float] = deque()
        self._state = self._load()

    @property
    def mode(self) -> str:
        return self._state.mode

    def status(self) -> PinStatus:
        state = self._state
        return PinStatus(state.mode, state.digest is not None)

    def granted(self, cookie_header: str | None) -> bool:
        state = self._state
        value = read_cookie(cookie_header, GRANT_COOKIE)
        payload = crypto.unsign(self._key(), value) if value else None
        if payload is None:
            return False
        try:
            version, _device, generation, expiry = payload.split(".")
            return (
                version == _GRANT_VERSION
                and int(generation) == state.generation
                and int(expiry) > self._clock()
            )
        except ValueError:
            return False

    def locks_site(self, cookie_header: str | None) -> bool:
        return self._state.mode == "site" and not self.granted(cookie_header)

    def hides_stations(self, cookie_header: str | None) -> bool:
        return self._state.mode == "stations" and not self.granted(cookie_header)

    def attempt(self, pin: str, client: str, secure: bool = True) -> Attempt:
        """secure: the request reached the proxy over https, so the browser
        may be told to send the grant over https only."""
        state = self._state
        if state.mode == "off":
            return Attempt(False, reason="off")
        release = self._failures.reserve(group(client))
        if release is None:
            return Attempt(False, reason="locked")
        if self._flooded():
            self._sleep(FLOOD_DELAY_S)
        given = self._digest(pin) if valid_pin(pin) else ""
        if state.digest is None or not hmac.compare_digest(given, state.digest):
            self._note_failure()
            return Attempt(False, reason="wrong")
        release()
        expiry = int(self._clock() + GRANT_S)
        # The device id names this grant; usage (stage 4) will count by it.
        payload = f"{_GRANT_VERSION}.{secrets.token_hex(8)}.{state.generation}.{expiry}"
        return Attempt(True, cookie=self._cookie(crypto.sign(self._key(), payload), secure))

    def change(self, mode: str, pin: str | None) -> PinStatus:
        """Sets the mode and the PIN (None keeps it). A new PIN, and leaving
        "off", sign out every device; moving between the two other modes
        keeps them."""
        if mode not in MODES:
            raise PinError(f"the mode is one of {', '.join(MODES)}")
        if pin is not None and not valid_pin(pin):
            raise PinError(f"the PIN is {PIN_MIN} to {PIN_MAX} digits")
        with self._lock:
            old = self._state
            digest = self._digest(pin) if pin is not None else old.digest
            if mode != "off" and digest is None:
                raise PinError("set a PIN before switching it on")
            bump = pin is not None or (mode != "off" and old.mode == "off")
            new = _State(mode, digest, old.generation + (1 if bump else 0))
            with self._settings.transaction() as db:
                db.execute(
                    "INSERT INTO listener_pin (id, mode, digest, generation) VALUES (1, ?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET mode = excluded.mode,"
                    " digest = excluded.digest, generation = excluded.generation",
                    (new.mode, new.digest, new.generation),
                )
            self._state = new
        return self.status()

    def _load(self) -> _State:
        with self._settings.read() as db:
            row = db.execute("SELECT mode, digest, generation FROM listener_pin").fetchone()
        if row is None:
            return _State("off", None, 0)
        mode, digest, generation = cast("tuple[str, str | None, int]", row)
        # A mode this tunebox does not know (a downgrade) locks the site:
        # failing open would show what the owner meant to keep private.
        return _State(mode if mode in MODES else "site", digest, generation)

    def _digest(self, pin: str) -> str:
        return hmac.new(self._key(), b"pin:" + pin.encode(), hashlib.sha256).hexdigest()

    def _cookie(self, value: str, secure: bool) -> str:
        flag = " Secure;" if secure else ""
        return f"{GRANT_COOKIE}={value}; Path=/; Max-Age={GRANT_S}; HttpOnly;{flag} SameSite=Lax"

    def _note_failure(self) -> None:
        with self._lock:
            self._recent.append(self._monotonic())

    def _flooded(self) -> bool:
        now = self._monotonic()
        with self._lock:
            while self._recent and self._recent[0] <= now - FLOOD_WINDOW_S:
                self._recent.popleft()
            # Capped: past the threshold only the count matters.
            while len(self._recent) > FLOOD:
                self._recent.popleft()
            return len(self._recent) >= FLOOD
