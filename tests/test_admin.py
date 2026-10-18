import base64
import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tunebox import crypto
from tunebox.accounts import Accounts
from tunebox.admin import ADMIN_HEADERS, KNOWN_COOKIE, SESSION_COOKIE, Admin, Request, Response
from tunebox.config import AdminConfig
from tunebox.settings import Settings, SettingsWriteError

ORIGIN = "https://radio.example"
PASSWORD = "a long enough password"
SECRET = b"12345678901234567890"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, tmp_path: Path, setup_page: bool = False, account: bool = True) -> None:
        self.clock = Clock()
        self.settings = Settings(tmp_path / "t.db")
        self.accounts = Accounts(self.settings, clock=self.clock)
        if account:
            self.accounts.create(PASSWORD, SECRET, ["aaaa-bbbb-cccc"], step=0)
        web = tmp_path / "web_admin"
        web.mkdir(exist_ok=True)
        (web / "index.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
        (web / "notes.md").write_text("x", encoding="utf-8")
        self.admin = Admin(
            self.accounts,
            AdminConfig(True, setup_page, ORIGIN),
            web,
            clock=self.clock,
            monotonic=self.clock,
        )
        self.cookies: dict[str, str] = {}

    def call(
        self,
        method: str,
        path: str,
        body: object = None,
        origin: str | None = ORIGIN,
        csrf: str | None = None,
        content_type: str = "application/json",
        client: str = "198.51.100.7",
    ) -> Response:
        headers: dict[str, str] = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = origin
        if csrf:
            headers["X-Tunebox-CSRF"] = csrf
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        raw = b"" if body is None else json.dumps(body).encode()
        response = self.admin.handle(Request(method, path, headers, raw, client))
        for name, value in response.headers:
            if name == "Set-Cookie":
                cookie_name, _, rest = value.partition("=")
                if "Max-Age=0" in value:
                    self.cookies.pop(cookie_name, None)
                else:
                    self.cookies[cookie_name] = rest.split(";")[0]
        return response

    def code(self, offset: int = 0) -> str:
        return crypto.totp(SECRET, int(self.clock.now // 30) + offset)

    def login(self) -> str:
        body = {"password": PASSWORD, "code": self.code()}
        response = self.call("POST", "/admin/api/login", body)
        assert response.status == 200, response.body
        return json_of(response)["csrf"]


def json_of(response: Response) -> dict[str, Any]:
    return json.loads(response.body)


def setup_token(records: list[logging.LogRecord]) -> str:
    for record in records:
        if record.getMessage() == "admin_setup_token":
            return record.__dict__["fields"]["token"]
    raise AssertionError("no setup token logged")


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    made = World(tmp_path)
    yield made
    made.settings.close()


def test_login_sets_strict_cookies_and_state_shows_the_session(world: World) -> None:
    response = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code()})
    assert response.status == 200
    cookies = [value for name, value in response.headers if name == "Set-Cookie"]
    session = next(c for c in cookies if c.startswith(SESSION_COOKIE))
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/admin"):
        assert flag in session
    assert any(c.startswith(KNOWN_COOKIE) for c in cookies)
    state = json_of(world.call("GET", "/admin/api/state"))
    assert state["session"]["client"] == "198.51.100.7"
    assert state["session"]["csrf"] == json_of(response)["csrf"]
    assert state["session"]["totp_suspected"] is False


def test_every_failure_looks_the_same_to_the_client(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        world.clock.now += 1.1
        wrong_password = world.call(
            "POST", "/admin/api/login", {"password": "nope nope nope", "code": world.code()}
        )
        world.clock.now += 1.1
        wrong_code = world.call(
            "POST", "/admin/api/login", {"password": PASSWORD, "code": "000000"}
        )
    assert wrong_password.status == wrong_code.status == 401
    assert wrong_password.body == wrong_code.body
    failures = [r for r in caplog.records if r.getMessage() == "admin_login_failed"]
    assert [r.__dict__["fields"]["reason"] for r in failures] == ["password", "totp"]
    assert all(r.__dict__["fields"]["client"] == "198.51.100.7" for r in failures)
    assert PASSWORD not in caplog.text
    assert "nope nope" not in caplog.text


def test_post_from_another_origin_is_refused(world: World) -> None:
    csrf = world.login()
    evil = world.call("POST", "/admin/api/logout", origin="https://evil.example", csrf=csrf)
    assert evil.status == 403
    assert world.call("POST", "/admin/api/logout", origin=None, csrf=csrf).status == 403
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is not None


def test_logout_needs_the_csrf_token(world: World) -> None:
    csrf = world.login()
    assert world.call("POST", "/admin/api/logout").status == 403
    assert world.call("POST", "/admin/api/logout", csrf="guess").status == 403
    assert world.call("POST", "/admin/api/logout", csrf=csrf).status == 204
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is None


def test_json_only(world: World) -> None:
    body = {"password": PASSWORD}
    assert world.call("POST", "/admin/api/login", body, content_type="text/plain").status == 415


def test_five_failures_lock_the_address(world: World) -> None:
    for _ in range(5):
        world.clock.now += 1.1
        failed = world.call("POST", "/admin/api/login", {"password": "x" * 12, "code": "1"})
        assert failed.status == 401
    world.clock.now += 1.1
    good = {"password": PASSWORD, "code": world.code()}
    assert world.call("POST", "/admin/api/login", good).status == 429
    world.clock.now += 1.1
    assert world.call("POST", "/admin/api/login", good, client="203.0.113.5").status == 200


def test_the_global_pace_has_a_lane_for_known_browsers(world: World) -> None:
    world.login()
    world.cookies.pop(SESSION_COOKIE)
    # Same second: a stranger is paced, the known browser has its own lane.
    stranger = world.admin.handle(
        Request(
            "POST",
            "/admin/api/login",
            {"Origin": ORIGIN, "Content-Type": "application/json"},
            json.dumps({"password": "x" * 12, "code": "1"}).encode(),
            "203.0.113.5",
        )
    )
    assert stranger.status == 429
    world.clock.now += 0.5
    known = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code(1)})
    assert known.status == 200


def test_a_forged_known_browser_cookie_gets_no_lane(world: World) -> None:
    world.login()
    world.cookies = {KNOWN_COOKIE: "k1.1.9999999999.forged"}
    world.clock.now += 0.5
    response = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code(1)})
    assert response.status == 429


def test_admin_answers_carry_the_admin_headers(world: World) -> None:
    for response in (world.call("GET", "/admin/"), world.call("GET", "/admin/api/state")):
        headers = dict(response.headers)
        for name, value in ADMIN_HEADERS.items():
            assert headers[name] == value
    assert "frame-ancestors 'none'" in ADMIN_HEADERS["Content-Security-Policy"]


def test_static_files_are_limited_to_known_types_inside_the_folder(world: World) -> None:
    assert world.call("GET", "/admin/").status == 200
    assert world.call("GET", "/admin/notes.md").status == 404
    assert world.call("GET", "/admin/../t.db").status == 404
    assert world.call("GET", "/admin/%2e%2e/t.db").status == 404
    assert world.call("GET", "/admin/missing.js").status == 404
    assert world.call("POST", "/admin/index.html").status == 405
    assert world.call("GET", "/admin").status == 301


def test_setup_page_flow(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    assert json_of(world.call("GET", "/admin/api/state"))["setup"] is True
    assert world.call("POST", "/admin/api/setup/start", {"token": "wrong"}).status == 401
    started = json_of(world.call("POST", "/admin/api/setup/start", {"token": token}))
    assert started["uri"].startswith("otpauth://totp/")
    secret = base64.b32decode(started["secret"] + "=" * (-len(started["secret"]) % 8))
    good = crypto.totp(secret, int(world.clock.now // 30))
    finish = "/admin/api/setup/finish"
    short = world.call("POST", finish, {"token": token, "password": "short", "code": good})
    assert json_of(short)["error"] == "password_too_short"
    bad = world.call("POST", finish, {"token": token, "password": PASSWORD, "code": "000000"})
    assert json_of(bad)["error"] == "code_wrong"
    done = world.call("POST", finish, {"token": token, "password": PASSWORD, "code": good})
    assert len(json_of(done)["recovery_codes"]) == 10
    assert world.accounts.exists()
    # The setup code is spent: it cannot log in.
    login = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": good})
    assert login.status == 401
    world.settings.close()


def test_setup_is_closed_once_an_account_exists(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    world.accounts.create(PASSWORD, SECRET, [], step=0)  # as the CLI would, meanwhile
    assert world.call("POST", "/admin/api/setup/start", {"token": token}).status == 401
    assert json_of(world.call("GET", "/admin/api/state"))["setup"] is False
    world.settings.close()


def test_the_setup_token_expires(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    world.clock.now += 901
    assert world.call("POST", "/admin/api/setup/start", {"token": token}).status == 401
    world.settings.close()


def test_no_setup_token_without_the_setup_page(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=False, account=False)
    assert "admin_setup_token" not in caplog.text
    assert world.call("POST", "/admin/api/setup/start", {"token": "x"}).status == 401
    world.settings.close()


def test_unknown_api_paths_and_bad_bodies(world: World) -> None:
    assert world.call("GET", "/admin/api/nothing").status == 404
    assert world.call("GET", "/admin/api/login").status == 404
    assert world.call("POST", "/admin/api/login", "not an object").status == 400
    assert world.call("POST", "/admin/api/login", {"password": 5, "code": "1"}).status == 400
    raw = Request(
        "POST",
        "/admin/api/login",
        {"Origin": ORIGIN, "Content-Type": "application/json"},
        b"{",
        "a",
    )
    assert world.admin.handle(raw).status == 400


def test_a_known_browser_grant_ends_with_an_account_reset(world: World) -> None:
    world.login()
    world.cookies.pop(SESSION_COOKIE)
    world.accounts.create(PASSWORD, SECRET, [], step=int(world.clock.now // 30), replace=True)
    world.clock.now += 0.5  # within the same second as the last attempt
    response = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code(1)})
    assert response.status == 429


def test_a_flooding_address_cannot_take_the_global_pace(world: World) -> None:
    flood = {"password": "x" * 12, "code": "1"}
    for _ in range(20):
        world.clock.now += 0.4
        world.call("POST", "/admin/api/login", flood, client="203.0.113.66")
    # Locked after five failures; past the last slot it took before that.
    world.clock.now += 1.1
    # Still flooding, but a locked address must not spend the shared slot.
    world.call("POST", "/admin/api/login", flood, client="203.0.113.66")
    world.clock.now += 0.1
    good = {"password": PASSWORD, "code": world.code()}
    assert world.call("POST", "/admin/api/login", good, client="198.51.100.20").status == 200


def test_non_ascii_tokens_are_refused_not_crashed(world: World, tmp_path: Path) -> None:
    world.login()
    assert world.call("POST", "/admin/api/logout", csrf="café").status == 403
    assert world.call("POST", "/admin/api/setup/start", {"token": "é"}).status == 401


def test_a_malformed_cookie_does_not_hide_the_session(world: World) -> None:
    world.login()
    session = world.cookies[SESSION_COOKIE]
    world.cookies = {"other": "a b c", SESSION_COOKIE: session}
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is not None


def test_logout_without_a_live_session_is_done_quietly(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.cookies = {SESSION_COOKIE: "expired-or-unknown"}
    with caplog.at_level(logging.WARNING):
        response = world.call("POST", "/admin/api/logout", csrf="anything")
    assert response.status == 204
    assert "admin_csrf_rejected" not in caplog.text
    assert SESSION_COOKIE not in world.cookies


def test_logins_that_fail_to_save_do_not_lock_the_owner_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> tuple[str, str]:
        raise SettingsWriteError("disk full")

    monkeypatch.setattr(Accounts, "_open", broken)
    for offset in range(6):
        world.clock.now += 31
        good = {"password": PASSWORD, "code": world.code(offset * 0)}
        assert world.call("POST", "/admin/api/login", good).status == 503
    monkeypatch.undo()
    world.clock.now += 31
    good = {"password": PASSWORD, "code": world.code()}
    assert world.call("POST", "/admin/api/login", good).status == 200


def test_a_session_stays_readable_when_writes_fail(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.login()
    world.clock.now += 120

    def unwritable(*_args: object) -> object:
        raise SettingsWriteError("read-only")

    monkeypatch.setattr(world.settings, "transaction", unwritable)
    state = world.call("GET", "/admin/api/state")
    assert state.status == 200
    assert json_of(state)["session"] is not None


def test_surrogates_are_refused_not_crashed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    assert world.call("POST", "/admin/api/setup/start", {"token": "\ud800"}).status == 401
    started = json_of(world.call("POST", "/admin/api/setup/start", {"token": token}))
    secret = base64.b32decode(started["secret"] + "=" * (-len(started["secret"]) % 8))
    code = crypto.totp(secret, int(world.clock.now // 30))
    body = {"token": token, "password": "\ud800" + "a" * 13, "code": code}
    assert world.call("POST", "/admin/api/setup/finish", body).status == 400
    world.settings.close()
