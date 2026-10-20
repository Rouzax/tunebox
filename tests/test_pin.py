from collections.abc import Generator
from pathlib import Path

import pytest

from tunebox.pin import GRANT_COOKIE, GRANT_S, Pin, PinError, read_cookie, valid_pin
from tunebox.settings import Settings

KEY = b"k" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.settings = Settings(tmp_path / "t.db")
        self.pin = self.make()

    def make(self) -> Pin:
        return Pin(
            self.settings,
            lambda: KEY,
            clock=self.clock,
            monotonic=self.clock,
        )


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    made = World(tmp_path)
    yield made
    made.settings.close()


def granted(world: World, pin: str = "123456", client: str = "198.51.100.7") -> str:
    attempt = world.pin.attempt(pin, client)
    assert attempt.ok and attempt.cookie
    return attempt.cookie.split(";")[0]


def test_off_by_default_and_open(world: World) -> None:
    assert world.pin.mode == "off"
    assert world.pin.status().mode == "off" and world.pin.status().set is False
    assert not world.pin.locks_site(None)
    assert not world.pin.hides_stations(None)
    assert world.pin.attempt("123456", "x").reason == "off"


@pytest.mark.parametrize("mode", ["site", "stations"])
def test_a_mode_needs_a_pin(world: World, mode: str) -> None:
    with pytest.raises(PinError):
        world.pin.change(mode, None)
    assert world.pin.mode == "off"


def test_an_unknown_mode_is_refused(world: World) -> None:
    with pytest.raises(PinError):
        world.pin.change("everything", "123456")


@pytest.mark.parametrize("bad", ["12345", "123456789", "12345a", "\uff11" * 6, " 123456", ""])
def test_a_pin_is_six_to_eight_ascii_digits(world: World, bad: str) -> None:
    assert not valid_pin(bad)
    with pytest.raises(PinError):
        world.pin.change("site", bad)


def test_site_mode_locks_the_site_until_the_pin(world: World) -> None:
    world.pin.change("site", "123456")
    assert world.pin.locks_site(None)
    assert not world.pin.hides_stations(None)
    cookie = granted(world)
    assert cookie.startswith(f"{GRANT_COOKIE}=")
    assert not world.pin.locks_site(cookie)
    assert not world.pin.locks_site(f"other=1; {cookie}; more=2")


def test_stations_mode_hides_stations_until_the_pin(world: World) -> None:
    world.pin.change("stations", "123456")
    assert not world.pin.locks_site(None)
    assert world.pin.hides_stations(None)
    assert not world.pin.hides_stations(granted(world))


def test_one_grant_serves_both_modes(world: World) -> None:
    world.pin.change("stations", "123456")
    cookie = granted(world)
    world.pin.change("site", None)
    assert world.pin.granted(cookie)
    assert not world.pin.locks_site(cookie)


def test_the_grant_cookie_attributes(world: World) -> None:
    world.pin.change("site", "123456")
    attempt = world.pin.attempt("123456", "x")
    assert attempt.cookie is not None
    attributes = [part.strip() for part in attempt.cookie.split(";")[1:]]
    assert attributes == ["Path=/", f"Max-Age={GRANT_S}", "HttpOnly", "Secure", "SameSite=Lax"]


def test_a_plain_http_request_gets_no_secure_flag(world: World) -> None:
    world.pin.change("site", "123456")
    cookie = world.pin.attempt("123456", "x", secure=False).cookie or ""
    assert "Secure" not in cookie


def test_a_wrong_pin_is_refused(world: World) -> None:
    world.pin.change("site", "123456")
    assert world.pin.attempt("654321", "x").reason == "wrong"
    assert world.pin.attempt("not digits", "x").reason == "wrong"


def test_ten_failures_lock_the_address_group(world: World) -> None:
    world.pin.change("site", "123456")
    for index in range(10):
        assert world.pin.attempt("000000", f"2001:db8::{index}").reason == "wrong"
    assert world.pin.attempt("123456", "2001:db8::99").reason == "locked"
    assert world.pin.attempt("123456", "198.51.100.7").ok
    world.clock.now += 901
    assert world.pin.attempt("123456", "2001:db8::99").ok


def test_a_flood_of_failures_paces_everyone_together(world: World) -> None:
    world.pin.change("site", "123456")
    for index in range(100):
        world.pin.attempt("000000", f"203.0.113.{index}")
    # Past the flood: one checked guess per 2 s across all addresses, the
    # rest refused at once (no thread waits), however many arrive together.
    assert world.pin.attempt("000000", "198.51.100.1").reason == "wrong"
    assert world.pin.attempt("123456", "198.51.100.2").reason == "busy"
    assert world.pin.attempt("123456", "198.51.100.3").reason == "busy"
    world.clock.now += 2
    assert world.pin.attempt("123456", "198.51.100.3").ok
    world.clock.now += 3601
    assert world.pin.attempt("000000", "198.51.100.4").reason == "wrong"
    assert world.pin.attempt("000000", "198.51.100.5").reason == "wrong"


def test_a_paced_attempt_does_not_count_against_the_address(world: World) -> None:
    world.pin.change("site", "123456")
    for index in range(100):
        world.pin.attempt("000000", f"203.0.113.{index}")
    world.pin.attempt("000000", "198.51.100.1")
    for _ in range(20):
        assert world.pin.attempt("123456", "198.51.100.9").reason == "busy"
    world.clock.now += 2
    assert world.pin.attempt("123456", "198.51.100.9").ok


def test_changing_the_pin_signs_every_device_out(world: World) -> None:
    world.pin.change("site", "123456")
    cookie = granted(world)
    world.pin.change("site", "7654321")
    assert world.pin.locks_site(cookie)
    assert world.pin.attempt("123456", "x").reason == "wrong"
    assert not world.pin.locks_site(granted(world, "7654321"))


def test_switching_off_and_on_signs_every_device_out(world: World) -> None:
    world.pin.change("site", "123456")
    cookie = granted(world)
    world.pin.change("off", None)
    assert not world.pin.locks_site(None)
    world.pin.change("site", None)
    assert world.pin.locks_site(cookie)


def test_moving_between_modes_keeps_the_devices(world: World) -> None:
    world.pin.change("site", "123456")
    cookie = granted(world)
    world.pin.change("stations", None)
    world.pin.change("site", None)
    assert not world.pin.locks_site(cookie)


def test_an_expired_or_forged_grant_is_refused(world: World) -> None:
    world.pin.change("site", "123456")
    cookie = granted(world)
    name, _, value = cookie.partition("=")
    payload, _, mac = value.rpartition(".")
    assert world.pin.locks_site(f"{name}={payload}.{mac[:-2]}xx")
    assert world.pin.locks_site(f"{name}=p1.abc.1.99999999999.{mac}")
    world.clock.now += GRANT_S + 1
    assert world.pin.locks_site(cookie)


def test_the_pin_survives_a_restart(world: World) -> None:
    world.pin.change("stations", "123456")
    cookie = granted(world)
    again = world.make()
    assert again.mode == "stations"
    assert again.granted(cookie)
    assert again.attempt("123456", "x").ok


def test_the_stored_pin_is_not_the_pin(world: World) -> None:
    world.pin.change("site", "123456")
    with world.settings.read() as db:
        row = db.execute("SELECT mode, digest, generation FROM listener_pin").fetchone()
    assert row[0] == "site" and "123456" not in row[1] and len(row[1]) == 64


def test_read_cookie_skips_malformed_pairs() -> None:
    header = "broken; a=1; tunebox_grant=x.y; =z"
    assert read_cookie(header, "tunebox_grant") == "x.y"
    assert read_cookie(header, "missing") is None
    assert read_cookie(None, "a") is None


def test_read_cookie_refuses_a_repeated_name() -> None:
    assert read_cookie("a=1; b=2", "a") == "1"
    assert read_cookie("a=1; a=2", "a") is None
