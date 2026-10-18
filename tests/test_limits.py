from tunebox.limits import FailureLimit, Pace, group


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_ipv6_is_grouped_per_64_and_ipv4_per_address() -> None:
    assert group("2001:db8:1:2:aaaa::1") == group("2001:db8:1:2:bbbb::9")
    assert group("2001:db8:1:2::1") != group("2001:db8:1:3::1")
    assert group("192.0.2.1") != group("192.0.2.2")
    assert group("::ffff:192.0.2.1") == group("192.0.2.1")
    assert group("not an address") == "not an address"


def test_five_failures_lock_the_key_and_success_does_not_count() -> None:
    clock = Clock()
    limit = FailureLimit(5, window_s=900, lock_s=900, clock=clock)
    release = limit.reserve("a")
    assert release is not None
    release()  # a success
    for _ in range(5):
        assert limit.reserve("a") is not None
    assert limit.reserve("a") is None
    assert limit.reserve("b") is not None
    clock.now += 901
    assert limit.reserve("a") is not None


def test_attempts_are_counted_before_they_are_checked() -> None:
    limit = FailureLimit(2, window_s=900, lock_s=900, clock=Clock())
    # Two reservations in flight at once: the third is refused although
    # neither has failed yet.
    assert limit.reserve("a") is not None
    assert limit.reserve("a") is not None
    assert limit.reserve("a") is None


def test_old_failures_leave_the_window() -> None:
    clock = Clock()
    limit = FailureLimit(2, window_s=60, lock_s=900, clock=clock)
    limit.reserve("a")
    clock.now += 61
    limit.reserve("a")
    assert limit.reserve("a") is not None


def test_the_key_map_is_capped() -> None:
    limit = FailureLimit(1, window_s=900, lock_s=900, clock=Clock(), max_keys=3)
    for key in "abcd":
        limit.reserve(key)
    assert len(limit) == 3


def test_pace_allows_one_per_interval() -> None:
    clock = Clock()
    pace = Pace(1.0, clock=clock)
    assert pace.allow()
    assert not pace.allow()
    clock.now += 1.0
    assert pace.allow()
