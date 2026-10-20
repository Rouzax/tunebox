from collections.abc import Generator
from pathlib import Path

import pytest

from tunebox import crypto
from tunebox.accounts import (
    ABSOLUTE_S,
    IDLE_S,
    REAUTH_S,
    AccountError,
    Accounts,
    Session,
    session_id,
)
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


def login(accounts: Accounts, clock: Clock, offset: int = 0, address: str = "a") -> str:
    outcome = accounts.login(PASSWORD, code(clock, offset), address, "Firefox 132, Linux")
    assert outcome.ok and outcome.token
    return outcome.token


def live(accounts: Accounts, token: str) -> Session:
    session = accounts.session(token)
    assert session is not None
    return session


def test_a_new_session_is_not_fresh(accounts: Accounts, clock: Clock) -> None:
    session = live(accounts, login(accounts, clock))
    assert session.reauth_at is None
    assert not accounts.fresh(session)


def test_a_fresh_code_makes_the_session_fresh_for_five_minutes(
    accounts: Accounts, clock: Clock
) -> None:
    token = login(accounts, clock)
    clock.now += 30
    assert accounts.reauth(live(accounts, token), code(clock)) is None
    assert accounts.fresh(live(accounts, token))
    clock.now += REAUTH_S + 1
    assert not accounts.fresh(live(accounts, token))


def test_reauth_refuses_the_code_used_to_log_in(accounts: Accounts, clock: Clock) -> None:
    token = login(accounts, clock)
    assert accounts.reauth(live(accounts, token), code(clock)) == "totp"
    assert not accounts.fresh(live(accounts, token))


def test_wrong_reauth_codes_count_like_wrong_login_codes(accounts: Accounts, clock: Clock) -> None:
    token = login(accounts, clock)
    for _ in range(10):
        assert accounts.reauth(live(accounts, token), "000000") == "totp"
    assert accounts.totp_suspected()


def test_reauth_takes_a_recovery_code_once(accounts: Accounts, clock: Clock) -> None:
    token = login(accounts, clock)
    assert accounts.reauth(live(accounts, token), "AAAA-bbbb-cccc") is None
    assert accounts.fresh(live(accounts, token))
    other = login(accounts, clock, offset=1, address="b")
    assert accounts.reauth(live(accounts, other), "aaaa-bbbb-cccc") == "recovery"


def test_reauth_of_one_session_leaves_the_others_stale(accounts: Accounts, clock: Clock) -> None:
    first = login(accounts, clock)
    second = login(accounts, clock, offset=1, address="b")
    clock.now += 60
    assert accounts.reauth(live(accounts, first), code(clock)) is None
    assert accounts.fresh(live(accounts, first))
    assert not accounts.fresh(live(accounts, second))


def test_sessions_lists_the_live_ones_and_ends_one(accounts: Accounts, clock: Clock) -> None:
    first = login(accounts, clock, address="198.51.100.7")
    clock.now += 10
    second = login(accounts, clock, offset=1, address="198.51.100.8")
    listed = accounts.sessions()
    assert [s.address for s in listed] == ["198.51.100.8", "198.51.100.7"]
    assert listed[0].user_agent == "Firefox 132, Linux"
    assert listed[0].id == session_id(live(accounts, second).token_hash)
    assert len(listed[0].id) == 16
    assert accounts.end_session(listed[1].id)
    assert accounts.session(first) is None
    assert accounts.session(second) is not None
    assert not accounts.end_session(listed[1].id)


@pytest.mark.parametrize("bad", ["", "XYZ", "0" * 64, "%", "abc'--"])
def test_end_session_ignores_ids_that_are_not_ids(accounts: Accounts, bad: str) -> None:
    assert not accounts.end_session(bad)


def test_expired_sessions_are_pruned_at_the_next_login(accounts: Accounts, clock: Clock) -> None:
    login(accounts, clock)
    clock.now += IDLE_S + 1
    login(accounts, clock, address="b")
    with accounts._settings.read() as db:  # pyright: ignore[reportPrivateUsage]
        count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count == 1
