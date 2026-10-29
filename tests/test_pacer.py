import math

import pytest

from tunebox.pacer import Busy, Pacer


def paced(now: list[float], per_minute: int = 3) -> Pacer:
    return Pacer(per_minute, "search_running", "too_many_searches", clock=lambda: now[0])


def test_the_minute_runs_out_and_comes_back() -> None:
    now = [100.0]
    pacer = paced(now)
    for _ in range(3):
        with pacer.slot(5.0):
            now[0] += 1
    with pytest.raises(Busy) as used_up, pacer.slot(5.0):
        pass
    assert str(used_up.value) == "too_many_searches"
    assert (used_up.value.running, used_up.value.retry_s) == (False, 57)
    now[0] += 57
    with pacer.slot(5.0):
        pass


def test_one_at_a_time_with_the_wait_left() -> None:
    now = [100.0]
    pacer = paced(now)
    with pacer.slot(18.0):
        now[0] += 2.5
        with pytest.raises(Busy) as running, pacer.slot(18.0):
            pass
    assert str(running.value) == "search_running"
    assert (running.value.running, running.value.retry_s) == (True, math.ceil(18.0 - 2.5))
    # Free again afterwards, also after a refused one.
    with pacer.slot(18.0):
        pass


def test_a_crash_inside_the_slot_frees_it() -> None:
    pacer = paced([0.0])
    with pytest.raises(RuntimeError), pacer.slot(1.0):
        raise RuntimeError("boom")
    with pacer.slot(1.0):
        pass
