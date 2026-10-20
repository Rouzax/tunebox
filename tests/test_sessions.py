import time

from tunebox.sessions import LOGIN_FLOOR_S, Gate


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make(clock: Clock, plain_http: bool = False) -> Gate:
    return Gate(lambda: b"k" * 32, plain_http, clock=clock, monotonic=clock, sleep=clock.sleep)


def test_known_grants_name_the_generation_and_the_scheme() -> None:
    clock = Clock()
    gate = make(clock)
    grant = gate.known_grant(3)
    assert gate.known(grant, 3)
    assert not gate.known(grant, 4)
    assert not make(clock, plain_http=True).known(grant, 3)
    clock.now += 366 * 86_400
    assert not gate.known(grant, 3)
    assert not gate.known("forged.value", 3)
    assert not gate.known(None, 3)


def test_the_floor_pads_a_quick_answer_only() -> None:
    clock = Clock()
    gate = make(clock)
    started = clock.now
    clock.now += 0.1
    gate.floor(started)
    assert abs(clock.slept[0] - (LOGIN_FLOOR_S - 0.1)) < 1e-9
    started = clock.now
    clock.now += 1.0
    gate.floor(started)
    assert len(clock.slept) == 1


def test_two_hashing_slots() -> None:
    gate = make(Clock())
    with gate.hashing() as first, gate.hashing() as second, gate.hashing() as third:
        assert (first, second, third) == (True, True, False)
    with gate.hashing() as again:
        assert again


def test_waiting_authenticator_secrets_are_dropped_with_their_session() -> None:
    gate = make(Clock())
    gate.hold_totp("a", b"1")
    gate.hold_totp("b", b"2")
    gate.drop_totp(keep={"b"})
    assert gate.take_totp("a") is None
    assert gate.take_totp("b") == b"2"
    gate.forget_totp("b")
    gate.forget_totp("b")
    assert gate.take_totp("b") is None


def test_the_pace_has_a_lane_for_known_browsers() -> None:
    gate = make(Clock())
    assert gate.pace(known=False)
    assert not gate.pace(known=False)
    assert gate.pace(known=True)


def test_an_address_group_is_locked_after_five_failures() -> None:
    gate = make(Clock())
    for _ in range(5):
        assert gate.reserve("2001:db8:1:2::1") is not None
    assert gate.reserve("2001:db8:1:2::9") is None


def test_floor_uses_the_real_clock_by_default() -> None:
    gate = Gate(lambda: b"k" * 32, False)
    started = time.monotonic()
    gate.floor(started)
    assert time.monotonic() - started >= LOGIN_FLOOR_S
