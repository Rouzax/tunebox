from tunebox.limits import FailureLimit, Once, Pace, group


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


def test_once_is_true_for_the_first_of_each_group_per_interval() -> None:
    clock = Clock()
    once = Once(600, clock=clock, max_keys=2)
    assert once.first("198.51.100.7")
    assert not once.first("198.51.100.7")
    assert once.first("2001:db8::1")
    assert not once.first("2001:db8::2")  # the same /64
    clock.now += 601
    assert once.first("198.51.100.7")


def test_once_forgets_the_oldest_group_when_full() -> None:
    once = Once(600, clock=Clock(), max_keys=2)
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert once.first(address)
    assert once.first("10.0.0.1")


def test_idle_entries_are_dropped_when_new_keys_arrive() -> None:
    clock = Clock()
    limit = FailureLimit(5, window_s=60, lock_s=60, clock=clock)
    for n in range(100):
        assert limit.reserve(f"198.51.100.{n}") is not None
    clock.now += 61
    limit.reserve("203.0.113.1")
    assert len(limit) == 1


def test_a_locked_entry_is_kept_until_its_lock_ends() -> None:
    clock = Clock()
    limit = FailureLimit(1, window_s=60, lock_s=600, clock=clock)
    limit.reserve("a")
    assert limit.reserve("a") is None  # locked now
    clock.now += 61
    limit.reserve("b")
    assert limit.reserve("a") is None
