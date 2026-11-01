import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import make_track
from tests.test_admin import World, json_of
from tests.test_health_checks import ADTS, FakeChannel, station
from tunebox.netguard import Probe
from tunebox.store import CoverDecision


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    made = World(tmp_path, health=True)
    yield made
    made.close()


def wait_until_idle(world: World) -> None:
    deadline = time.monotonic() + 2
    while json_of(world.call("GET", "/admin/api/health/count"))["running"]:
        assert time.monotonic() < deadline, "the run did not end in time"
        threading.Event().wait(0.01)


@pytest.mark.parametrize("path", ["health", "health/count", "health/covers"])
def test_reads_need_a_session(world: World, path: str) -> None:
    assert world.call("GET", f"/admin/api/{path}").status == 401


def test_the_button_needs_a_session(world: World) -> None:
    assert world.call("POST", "/admin/api/health/check", {}, csrf="x").status == 401


def test_the_button_needs_the_token_and_the_origin(world: World) -> None:
    csrf = world.login()
    missing = world.call("POST", "/admin/api/health/check", {})
    assert missing.status == 403 and json_of(missing)["error"] == "bad_csrf"
    foreign = world.call(
        "POST", "/admin/api/health/check", {}, csrf=csrf, origin="https://evil.invalid"
    )
    assert foreign.status == 403 and json_of(foreign)["error"] == "bad_origin"


def test_check_all_now_then_too_soon(world: World) -> None:
    csrf = world.login()
    started = world.call("POST", "/admin/api/health/check", {}, csrf=csrf)
    assert started.status == 202 and json_of(started) == {"started": True}
    wait_until_idle(world)
    again = world.call("POST", "/admin/api/health/check", {}, csrf=csrf)
    assert again.status == 429 and json_of(again)["error"] == "health_too_soon"
    assert json_of(again)["retry_s"] == 300
    assert ("Retry-After", str(json_of(again)["retry_s"])) in again.headers


def test_a_second_press_while_running_is_refused(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def held(_url: str) -> Probe:
        entered.set()
        release.wait(5)
        return Probe("audio/aac", {}, ADTS)

    world = World(tmp_path, health=True, probe=held)
    world.health_channels["owr"] = FakeChannel(station("owr"))
    try:
        csrf = world.login()
        assert world.call("POST", "/admin/api/health/check", {}, csrf=csrf).status == 202
        assert entered.wait(2)
        count = json_of(world.call("GET", "/admin/api/health/count"))
        assert count["running"] is True and count["progress"] is not None
        second = world.call("POST", "/admin/api/health/check", {}, csrf=csrf)
        assert second.status == 409 and json_of(second)["error"] == "health_running"
    finally:
        release.set()
    wait_until_idle(world)
    page = json_of(world.call("GET", "/admin/api/health"))
    # The stream answered and the unreachable feed failed twice.
    assert [s["status"] for s in page["stations"]] == ["broken"]
    world.close()


def test_the_page_and_the_count(world: World) -> None:
    world.login()
    page = json_of(world.call("GET", "/admin/api/health"))
    assert page["stations"] == [] and "run" in page
    count = json_of(world.call("GET", "/admin/api/health/count"))
    assert count == {"broken": 0, "running": False, "progress": None, "stopped": False}


def test_an_unreadable_store_answers_unavailable(world: World) -> None:
    world.login()
    assert world.health is not None
    world.health._store.close()  # pyright: ignore[reportPrivateUsage]
    answer = world.call("GET", "/admin/api/health")
    assert answer.status == 503 and json_of(answer)["error"] == "health_unavailable"


def test_a_stopped_worker_answers_503(world: World) -> None:
    csrf = world.login()
    assert world.health is not None
    world.stop.set()
    world.health.stop_and_join(time.monotonic() + 2)
    page = world.call("GET", "/admin/api/health")
    assert page.status == 503 and json_of(page)["error"] == "health_stopped"
    pressed = world.call("POST", "/admin/api/health/check", {}, csrf=csrf)
    assert pressed.status == 503 and json_of(pressed)["error"] == "health_stopped"
    assert json_of(world.call("GET", "/admin/api/health/count"))["stopped"] is True


def test_no_cover_page_means_no_counts(world: World) -> None:
    world.login()
    assert json_of(world.call("GET", "/admin/api/health/covers")) == {"counts": None}


def test_the_songs_without_an_image_per_station(tmp_path: Path) -> None:
    world = World(tmp_path, health=True, covers=True)
    assert world.cover_store is not None
    now_ms = int(world.clock.now * 1000)
    track = make_track("A", "B", now_ms - 5)
    world.cover_store.record_heard("owr", [track])
    world.cover_store.put_cover(CoverDecision(track.key, "miss", None, "m", world.clock.now))
    world.login()
    counts = json_of(world.call("GET", "/admin/api/health/covers"))["counts"]
    assert counts == {"owr": 1}
    world.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "health"),
        ("GET", "health/count"),
        ("GET", "health/covers"),
        ("POST", "health/check"),
    ],
)
def test_no_health_means_not_found(tmp_path: Path, method: str, path: str) -> None:
    plain = World(tmp_path)
    csrf = plain.login()
    answer = plain.call(method, f"/admin/api/{path}", {} if method == "POST" else None, csrf=csrf)
    assert answer.status == 404 and json_of(answer)["error"] == "not_found"
    plain.close()
