from collections.abc import Generator
from pathlib import Path

import pytest

from tunebox import crypto
from tunebox.accounts import ABSOLUTE_S, IDLE_S, AccountError, Accounts
from tunebox.settings import Settings

PASSWORD = "a long enough password"
SECRET = b"12345678901234567890"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def accounts(tmp_path: Path, clock: Clock) -> Generator[Accounts]:
    settings = Settings(tmp_path / "t.db")
    made = Accounts(settings, clock=clock)
    made.create(PASSWORD, SECRET, ["aaaa-bbbb-cccc"], step=0)
    yield made
    settings.close()


def code(clock: Clock, offset: int = 0) -> str:
    return crypto.totp(SECRET, int(clock.now // 30) + offset)


def test_a_good_login_opens_a_session(accounts: Accounts, clock: Clock) -> None:
    outcome = accounts.login(PASSWORD, code(clock), "198.51.100.7", "Firefox")
    assert outcome.ok
    assert outcome.token
    assert outcome.csrf
    session = accounts.session(outcome.token)
    assert session is not None
    assert session.csrf == outcome.csrf
    assert session.address == "198.51.100.7"


def test_a_wrong_password_or_code_fails_with_its_reason(accounts: Accounts, clock: Clock) -> None:
    assert accounts.login("wrong password!", code(clock), "a", "b").reason == "password"
    assert accounts.login(PASSWORD, "000000", "a", "b").reason == "totp"


def test_a_code_cannot_be_used_twice(accounts: Accounts, clock: Clock) -> None:
    first = code(clock)
    assert accounts.login(PASSWORD, first, "a", "b").ok
    assert accounts.login(PASSWORD, first, "a", "b").reason == "totp"
    # The previous step is also behind the last one used now.
    assert accounts.login(PASSWORD, code(clock, -1), "a", "b").reason == "totp"


def test_a_recovery_code_works_once(accounts: Accounts) -> None:
    assert accounts.login(PASSWORD, "AAAA-BBBB-CCCC", "a", "b").ok
    assert accounts.login(PASSWORD, "aaaa-bbbb-cccc", "a", "b").reason == "recovery"


def test_sessions_expire_when_idle_and_after_twelve_hours(accounts: Accounts, clock: Clock) -> None:
    token = accounts.login(PASSWORD, code(clock), "a", "b").token or ""
    clock.now += IDLE_S - 1
    assert accounts.session(token) is not None
    clock.now += IDLE_S + 1
    assert accounts.session(token) is None
    clock.now += 60
    token = accounts.login(PASSWORD, code(clock), "a", "b").token or ""
    for _ in range(ABSOLUTE_S // (IDLE_S - 60) + 1):
        clock.now += IDLE_S - 60
        accounts.session(token)
    assert accounts.session(token) is None


def test_logout_ends_the_session(accounts: Accounts, clock: Clock) -> None:
    token = accounts.login(PASSWORD, code(clock), "a", "b").token or ""
    accounts.logout(token)
    assert accounts.session(token) is None
    assert accounts.session("never issued") is None


def test_many_wrong_codes_back_off_but_a_recovery_code_still_works(
    accounts: Accounts, clock: Clock
) -> None:
    for _ in range(10):
        assert accounts.login(PASSWORD, "000000", "a", "b").reason == "totp"
    assert accounts.totp_suspected()
    assert accounts.login(PASSWORD, code(clock), "a", "b").reason == "backoff"
    clock.now += 61
    assert accounts.login(PASSWORD, code(clock), "a", "b").ok
    assert not accounts.totp_suspected()
    for _ in range(10):
        accounts.login(PASSWORD, "000000", "a", "b")
    assert accounts.login(PASSWORD, "aaaa-bbbb-cccc", "a", "b").ok


def test_wrong_codes_older_than_a_day_start_a_new_count(accounts: Accounts, clock: Clock) -> None:
    for _ in range(9):
        accounts.login(PASSWORD, "000000", "a", "b")
    clock.now += 86_401
    accounts.login(PASSWORD, "000000", "a", "b")
    assert not accounts.totp_suspected()


def test_create_refuses_a_second_account_unless_replacing(accounts: Accounts, clock: Clock) -> None:
    token = accounts.login(PASSWORD, code(clock), "a", "b").token or ""
    generation = accounts.generation()
    with pytest.raises(AccountError, match="exists"):
        accounts.create(PASSWORD, SECRET, [], step=0)
    accounts.create("another long password", SECRET, [], step=0, replace=True)
    assert accounts.session(token) is None
    assert accounts.generation() == generation + 1


def test_a_short_password_is_refused(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    with pytest.raises(AccountError, match="12"):
        Accounts(settings).create("short", SECRET, [], step=0)
    settings.close()


def test_without_an_account_login_fails_as_a_password_failure(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    accounts = Accounts(settings)
    assert not accounts.exists()
    assert accounts.generation() == 0
    assert not accounts.totp_suspected()
    assert accounts.login(PASSWORD, "123456", "a", "b").reason == "password"
    settings.close()


def test_the_server_key_is_made_once(accounts: Accounts, tmp_path: Path) -> None:
    key = accounts.server_key()
    assert len(key) == 32
    assert accounts.server_key() == key


def test_concurrent_wrong_codes_each_count(accounts: Accounts, clock: Clock) -> None:
    import threading

    barrier = threading.Barrier(2)

    def wrong() -> None:
        barrier.wait()
        accounts.login(PASSWORD, "000000", "a", "b")

    for _ in range(5):
        threads = [threading.Thread(target=wrong) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert accounts.totp_suspected()


def test_wrong_recovery_codes_count_too(accounts: Accounts) -> None:
    for _ in range(10):
        assert accounts.login(PASSWORD, "zzzz-zzzz-zzzz", "a", "b").reason == "recovery"
    assert accounts.totp_suspected()
