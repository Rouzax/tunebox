import base64
import dataclasses
import json
import logging
import threading
import time
import tomllib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tunebox import crypto
from tunebox.accounts import AccountError, Accounts, Session, SessionEnded
from tunebox.admin import ADMIN_HEADERS, MAX_BODY, MAX_STATE_BODY, Admin, Request, Response
from tunebox.blobs import MAX_BLOB, Blobs
from tunebox.config import AdminConfig
from tunebox.pin import Pin
from tunebox.settings import Settings, SettingsUnavailable, SettingsWriteError

ORIGIN = "https://radio.example"
PASSWORD = "a long enough password"
SECRET = b"12345678901234567890"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(
        self,
        tmp_path: Path,
        setup_page: bool = False,
        account: bool = True,
        origin: str = ORIGIN,
    ) -> None:
        self.origin = origin
        self.clock = Clock()
        self.tmp = tmp_path
        self.blobs = Blobs(tmp_path / "blobs")
        self.settings = Settings(tmp_path / "t.db")
        self.accounts = Accounts(self.settings, clock=self.clock)
        if account:
            self.accounts.create(PASSWORD, SECRET, ["aaaa-bbbb-cccc"], step=0)
        web = tmp_path / "web_admin"
        web.mkdir(exist_ok=True)
        (web / "index.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
        (web / "notes.md").write_text("x", encoding="utf-8")
        self.applied: list[int] = []
        self.samples_asked = 0
        self.apply_result: int | None = None
        self.pin = Pin(self.settings, self.accounts.server_key, clock=self.clock)
        self.admin = Admin(
            self.accounts,
            AdminConfig(True, setup_page, origin),
            web,
            settings=self.settings,
            pin=self.pin,
            blobs=self.blobs,
            apply=self.apply,
            samples=self.samples,
            clock=self.clock,
            monotonic=self.clock,
            sleep=lambda _s: None,
        )
        self.cookies: dict[str, str] = {}

    def call(
        self,
        method: str,
        path: str,
        body: object = None,
        origin: str | None = "",
        csrf: str | None = None,
        content_type: str = "application/json",
        client: str = "198.51.100.7",
    ) -> Response:
        headers: list[tuple[str, str]] = [("Content-Type", content_type)]
        if origin == "":
            origin = self.origin
        if origin:
            headers.append(("Origin", origin))
        if csrf:
            headers.append(("X-Tunebox-CSRF", csrf))
        if self.cookies:
            headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items())))
        raw = b"" if body is None else json.dumps(body).encode()
        path, _, query = path.partition("?")
        response = self.admin.handle(Request(method, path, tuple(headers), raw, client, query))
        for name, value in response.headers:
            if name == "Set-Cookie":
                cookie_name, _, rest = value.partition("=")
                if "Max-Age=0" in value:
                    self.cookies.pop(cookie_name, None)
                else:
                    self.cookies[cookie_name] = rest.split(";")[0]
        return response

    def apply(self) -> int:
        self.applied.append(1)
        if self.apply_result is not None:
            return self.apply_result
        return self.settings.load()[1]

    def samples(self) -> list[str]:
        self.samples_asked += 1
        return ["FISHER - Stay"]

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


PLAIN = "http://tunebox.home.arpa:8083"


def test_plain_http_cookies_drop_the_secure_flag_and_prefix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, origin=PLAIN)
    started = next(r for r in caplog.records if r.getMessage() == "admin_plain_http")
    assert started.__dict__["fields"]["origin"] == PLAIN
    response = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code()})
    assert response.status == 200
    cookies = [value for name, value in response.headers if name == "Set-Cookie"]
    assert [c.split("=")[0] for c in cookies] == ["tunebox_admin", "tunebox_known"]
    for cookie in cookies:
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
    # A __Secure- cookie is not a session here: browsers never send one over http.
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is not None
    world.cookies = {"__Secure-tunebox_admin": world.cookies["tunebox_admin"]}
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is None
    world.settings.close()


def test_the_scheme_is_read_whatever_its_case(tmp_path: Path) -> None:
    world = World(tmp_path, origin="HTTP://tunebox.home.arpa:8083")
    assert world.admin.plain_http
    assert world.admin.session_cookie == "tunebox_admin"
    world.settings.close()


def test_a_known_browser_grant_from_plain_http_gets_no_lane_on_https(tmp_path: Path) -> None:
    world = World(tmp_path, origin=PLAIN)
    world.login()
    grant = world.cookies["tunebox_known"]
    https = Admin(
        world.accounts,
        AdminConfig(True, False, ORIGIN),
        tmp_path / "web_admin",
        settings=world.settings,
        pin=world.pin,
        blobs=world.blobs,
        apply=world.apply,
        samples=list,
        clock=world.clock,
        monotonic=world.clock,
    )
    stranger = Request(
        "POST",
        "/admin/api/login",
        (("Origin", ORIGIN), ("Content-Type", "application/json")),
        json.dumps({"password": "x" * 12, "code": "1"}).encode(),
        "203.0.113.5",
    )
    assert https.handle(stranger).status == 401
    world.clock.now += 0.5
    sniffed = Request(
        "POST",
        "/admin/api/login",
        (
            ("Origin", ORIGIN),
            ("Content-Type", "application/json"),
            ("Cookie", f"__Secure-tunebox_known={grant}"),
        ),
        json.dumps({"password": PASSWORD, "code": world.code(1)}).encode(),
        "198.51.100.7",
    )
    assert https.handle(sniffed).status == 429
    world.settings.close()


def test_an_https_origin_logs_no_plain_http_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path)
    assert "admin_plain_http" not in caplog.text
    world.settings.close()


def test_login_sets_strict_cookies_and_state_shows_the_session(world: World) -> None:
    response = world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": world.code()})
    assert response.status == 200
    cookies = [value for name, value in response.headers if name == "Set-Cookie"]
    session = next(c for c in cookies if c.startswith(world.admin.session_cookie))
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/admin"):
        assert flag in session
    assert any(c.startswith(world.admin.known_cookie) for c in cookies)
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
    world.cookies.pop(world.admin.session_cookie)
    # Same second: a stranger is paced, the known browser has its own lane.
    stranger = world.admin.handle(
        Request(
            "POST",
            "/admin/api/login",
            (("Origin", ORIGIN), ("Content-Type", "application/json")),
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
    world.cookies = {world.admin.known_cookie: "k1.1.9999999999.forged"}
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


def test_reloading_the_setup_page_keeps_the_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    with caplog.at_level(logging.WARNING):
        first = json_of(world.call("POST", "/admin/api/setup/start", {"token": token}))
        again = json_of(
            world.call("POST", "/admin/api/setup/start", {"token": token}, client="203.0.113.9")
        )
    assert first["secret"] == again["secret"]
    # Every fetch of the secret shows in the log, with who fetched it.
    started = [r for r in caplog.records if r.getMessage() == "admin_setup_started"]
    assert [r.__dict__["fields"]["client"] for r in started] == ["198.51.100.7", "203.0.113.9"]
    world.settings.close()


def test_two_setup_starts_at_once_get_one_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    made = crypto.new_totp_secret

    def slow() -> bytes:
        time.sleep(0.05)
        return made()

    monkeypatch.setattr(crypto, "new_totp_secret", slow)
    secrets: list[str] = []

    def start(client: str) -> None:
        answer = world.call("POST", "/admin/api/setup/start", {"token": token}, client=client)
        secrets.append(json_of(answer)["secret"])

    threads = [threading.Thread(target=start, args=(f"198.51.100.{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(secrets) == 2
    assert secrets[0] == secrets[1]
    world.settings.close()


def test_setup_token_log_line_gives_the_fragment_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    fields = next(
        r.__dict__["fields"] for r in caplog.records if r.getMessage() == "admin_setup_token"
    )
    assert fields["path"] == f"/admin/#setup={token}"
    world.settings.close()


def test_setup_finish_is_refused_once_an_account_exists(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        world = World(tmp_path, setup_page=True, account=False)
    token = setup_token(caplog.records)
    started = json_of(world.call("POST", "/admin/api/setup/start", {"token": token}))
    world.accounts.create(PASSWORD, SECRET, ["aaaa-bbbb-cccc"], step=0)
    secret = base64.b32decode(started["secret"] + "=" * (-len(started["secret"]) % 8))
    code = crypto.totp(secret, int(world.clock.now // 30))
    answer = world.call(
        "POST", "/admin/api/setup/finish", {"token": token, "password": PASSWORD, "code": code}
    )
    assert answer.status == 401
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
        (("Origin", ORIGIN), ("Content-Type", "application/json")),
        b"{",
        "a",
    )
    assert world.admin.handle(raw).status == 400


def test_a_known_browser_grant_ends_with_an_account_reset(world: World) -> None:
    world.login()
    world.cookies.pop(world.admin.session_cookie)
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
    session = world.cookies[world.admin.session_cookie]
    world.cookies = {"other": "a b c", world.admin.session_cookie: session}
    assert json_of(world.call("GET", "/admin/api/state"))["session"] is not None


def test_logout_without_a_live_session_is_done_quietly(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.cookies = {world.admin.session_cookie: "expired-or-unknown"}
    with caplog.at_level(logging.WARNING):
        response = world.call("POST", "/admin/api/logout", csrf="anything")
    assert response.status == 204
    assert "admin_csrf_rejected" not in caplog.text
    assert world.admin.session_cookie not in world.cookies


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


def station(cid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "group": None,
        "enabled": True,
        "doc": {
            "id": cid,
            "name": cid.upper(),
            "stream_url": "https://example.invalid/stream.aac",
            "source": {"type": "triton", "mount": "OWR_INTERNATIONAL"},
            **extra,
        },
    }


def state_of(*stations: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"defaults": {}, "player": {}, "groups": [], "channels": list(stations), **top}


def save(world: World, csrf: str, based_on: int, state: dict[str, Any]) -> Response:
    return world.call(
        "POST", "/admin/api/config", {"based_on": based_on, "state": state}, csrf=csrf
    )


def test_station_endpoints_need_a_session_and_the_token(world: World) -> None:
    body = {"based_on": 0, "state": state_of(station("a"))}
    for method, path in (
        ("GET", "/admin/api/config"),
        ("GET", "/admin/api/history"),
        ("GET", "/admin/api/history/1"),
        ("POST", "/admin/api/config"),
        ("POST", "/admin/api/history/1/restore"),
    ):
        answer = world.call(method, path, body if method == "POST" else None)
        assert answer.status == 401, path
        assert json_of(answer) == {"error": "login_required"}
    csrf = world.login()
    assert world.call("POST", "/admin/api/config", body).status == 403
    assert world.call("POST", "/admin/api/config", body, csrf="x").status == 403
    assert world.call("POST", "/admin/api/config", body, csrf=csrf, origin=None).status == 403
    assert world.settings.load()[1] == 0
    assert save(world, csrf, 0, state_of(station("a"))).status == 200


def test_config_round_trip_applies_and_records(world: World) -> None:
    csrf = world.login()
    loaded = json_of(world.call("GET", "/admin/api/config"))
    assert loaded["version"] == 0
    assert "mount" in loaded["schema"]["sources"]["triton"]
    assert loaded["schema"]["codecs"] == ["aac", "mp3"]
    assert "delay_s" in loaded["schema"]["defaults"]
    answer = world.call(
        "POST",
        "/admin/api/config",
        {"based_on": 0, "state": state_of(station("a"), station("b")), "target": "a"},
        csrf=csrf,
    )
    assert json_of(answer) == {"version": 1, "changed": True, "applied": True}
    assert world.applied == [1]
    loaded = json_of(world.call("GET", "/admin/api/config"))
    assert [c["id"] for c in loaded["state"]["channels"]] == ["a", "b"]
    history = json_of(world.call("GET", "/admin/api/history"))
    assert [(e["kind"], e["target"]) for e in history["entries"]] == [("edit", "a")]
    assert history["more"] is False


def test_history_pages_with_before(world: World) -> None:
    csrf = world.login()
    for version in range(3):
        save(world, csrf, version, state_of(*[station(f"s{i}") for i in range(version + 1)]))
    first = json_of(world.call("GET", "/admin/api/history"))["entries"]
    older = json_of(world.call("GET", f"/admin/api/history?before={first[0]['id']}"))
    assert [e["id"] for e in older["entries"]] == [e["id"] for e in first[1:]]


def test_export_is_the_stored_state_as_toml(world: World) -> None:
    csrf = world.login()
    off = station("late")
    off["enabled"] = False
    save(world, csrf, 0, state_of(station("owr"), off))
    answer = world.call("GET", "/admin/api/export")
    assert answer.status == 200
    assert answer.content_type == "application/toml; charset=utf-8"
    assert (
        dict(answer.headers)["Content-Disposition"]
        == 'attachment; filename="tunebox-1970-01-12.toml"'
    )
    text = answer.body.decode()
    assert text.startswith("# tunebox configuration, version 1\n")
    data = tomllib.loads(text)
    assert [c["id"] for c in data["channels"]] == ["owr", "late"]
    assert data["channels"][1]["enabled"] is False
    for name, value in ADMIN_HEADERS.items():
        assert dict(answer.headers)[name] == value


STATIONS_TOML = """
[[channels]]
id = "owr"
name = "OWR"
stream_url = "https://example.invalid/owr.aac"
source = { type = "triton", mount = "OWR_INTERNATIONAL" }

[[channels]]
id = "late"
name = "Late"
stream_url = "https://example.invalid/late.aac"
source = { type = "triton", mount = "OWR_INTERNATIONAL" }
"""

PLAIN_TOML = (
    '[server]\nport = 8083\n\n[admin]\nenabled = true\npublic_origin = "https://radio.example"\n'
    + STATIONS_TOML
)

TWO_SAME_IDS = STATIONS_TOML.replace('id = "late"', 'id = "owr"')

SLOW_RULE = STATIONS_TOML.replace(
    'name = "Late"', "name = \"Late\"\nrules = { played_by = '(a+)+$' }"
)


def test_export_then_import_changes_nothing(world: World) -> None:
    csrf = world.login()
    save(world, csrf, 0, state_of(station("owr")))
    text = world.call("GET", "/admin/api/export").body.decode()
    fresh(world, csrf)
    answer = world.call("POST", "/admin/api/import", {"toml": text, "based_on": 1}, csrf=csrf)
    assert json_of(answer)["changed"] is False
    assert len(world.settings.history(None)[0]) == 1


def test_import_stores_a_plain_file_as_history_kind_import(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    answer = world.call("POST", "/admin/api/import", {"toml": PLAIN_TOML, "based_on": 0}, csrf=csrf)
    assert json_of(answer)["changed"] is True
    entry = world.settings.history(None)[0][0]
    assert (entry.kind, entry.target) == ("import", "panel")
    assert [row.id for row in world.settings.load()[0].channels] == ["owr", "late"]


def test_import_needs_a_fresh_code(world: World) -> None:
    csrf = world.login()
    body = {"toml": STATIONS_TOML, "based_on": 0}
    answer = world.call("POST", "/admin/api/import", body, csrf=csrf)
    assert answer.status == 403
    assert json_of(answer) == {"error": "reauth_required"}
    assert world.settings.load()[1] == 0


def test_preview_shows_the_state_and_notes_without_storing(world: World) -> None:
    csrf = world.login()
    answer = world.call("POST", "/admin/api/import/preview", {"toml": PLAIN_TOML}, csrf=csrf)
    assert answer.status == 200
    data = json_of(answer)
    assert [c["id"] for c in data["state"]["channels"]] == ["owr", "late"]
    assert any("[server]" in note for note in data["notes"])
    assert any("[admin]" in note for note in data["notes"])
    assert world.settings.load()[1] == 0


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("[[channels]\n", "line"),
        ("surprise = 1\n", "surprise"),
        (TWO_SAME_IDS, "duplicate id"),
        (STATIONS_TOML.replace('name = "Late"', "name = 1979-05-27"), "channels[1].name"),
        (SLOW_RULE, "rules.played_by"),
    ],
)
def test_a_broken_import_is_refused_with_its_reason(world: World, text: str, names: str) -> None:
    csrf = world.login()
    fresh(world, csrf)
    preview = world.call("POST", "/admin/api/import/preview", {"toml": text}, csrf=csrf)
    stored = world.call("POST", "/admin/api/import", {"toml": text, "based_on": 0}, csrf=csrf)
    for answer in (preview, stored):
        assert answer.status == 400
        assert names in json_of(answer)["message"]
    assert world.settings.load()[1] == 0


def test_the_preview_does_not_lint_patterns_the_store_already_holds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()
    rules = {"played_by": r"\s*\(Played by ([^)]*)\)\s*$"}
    save(world, csrf, 0, state_of(station("owr", rules=rules)))
    text = world.call("GET", "/admin/api/export").body.decode()

    def stricter(_pattern: str) -> str:
        return "now refused"

    monkeypatch.setattr("tunebox.rulecheck.lint", stricter)
    answer = world.call("POST", "/admin/api/import/preview", {"toml": text}, csrf=csrf)
    assert answer.status == 200
    changed = text.replace("Played by", "Played with")
    answer = world.call("POST", "/admin/api/import/preview", {"toml": changed}, csrf=csrf)
    assert answer.status == 400
    assert json_of(answer)["message"] == "channels[0].rules.played_by: now refused"


def test_a_deeply_nested_import_is_refused_not_a_crash(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    deep = "x = " + "[" * 5000 + "]" * 5000
    for path, body in (
        ("/admin/api/import/preview", {"toml": deep}),
        ("/admin/api/import", {"toml": deep, "based_on": 0}),
    ):
        answer = world.call("POST", path, body, csrf=csrf)
        assert answer.status == 400
        assert "too deeply nested" in json_of(answer)["message"]


def test_a_deeply_nested_json_body_is_a_bad_request(world: World) -> None:
    csrf = world.login()
    request = Request(
        "POST",
        "/admin/api/config",
        (
            ("Origin", ORIGIN),
            ("Content-Type", "application/json"),
            ("X-Tunebox-CSRF", csrf),
            ("Cookie", "; ".join(f"{k}={v}" for k, v in world.cookies.items())),
        ),
        b"[" * 100_000,
        "198.51.100.7",
    )
    assert world.admin.handle(request).status == 400


def test_a_non_finite_number_is_refused(world: World) -> None:
    csrf = world.login()
    answer = world.call(
        "POST",
        "/admin/api/import/preview",
        {"toml": "[defaults]\ndelay_s = nan\n" + STATIONS_TOML},
        csrf=csrf,
    )
    assert answer.status == 400
    assert "defaults.delay_s" in json_of(answer)["message"]
    raw = '{"based_on": 0, "state": {"defaults": {"delay_s": NaN}, "channels": []}}'
    request = Request(
        "POST",
        "/admin/api/config",
        (
            ("Origin", ORIGIN),
            ("Content-Type", "application/json"),
            ("X-Tunebox-CSRF", csrf),
            ("Cookie", "; ".join(f"{k}={v}" for k, v in world.cookies.items())),
        ),
        raw.encode(),
        "198.51.100.7",
    )
    saved = world.admin.handle(request)
    assert saved.status == 400
    assert "defaults.delay_s" in json_of(saved)["message"]


def test_export_then_import_keeps_enabled_groups_and_an_empty_group(world: World) -> None:
    csrf = world.login()
    off = station("late")
    off["enabled"] = False
    off["group"] = "Night"
    on = station("owr")
    on["group"] = "Day"
    state = state_of(on, off, groups=["Day", "Empty", "Night"])
    assert save(world, csrf, 0, state).status == 200
    before = json_of(world.call("GET", "/admin/api/config"))["state"]
    text = world.call("GET", "/admin/api/export").body.decode()
    fresh(world, csrf)
    answer = world.call("POST", "/admin/api/import", {"toml": text, "based_on": 1}, csrf=csrf)
    assert json_of(answer)["changed"] is False
    assert json_of(world.call("GET", "/admin/api/config"))["state"] == before
    assert before["groups"] == ["Day", "Empty", "Night"]


def test_a_stale_import_is_refused(world: World) -> None:
    csrf = world.login()
    save(world, csrf, 0, state_of(station("a")))
    fresh(world, csrf)
    answer = world.call(
        "POST", "/admin/api/import", {"toml": STATIONS_TOML, "based_on": 0}, csrf=csrf
    )
    assert (answer.status, json_of(answer)) == (409, {"error": "stale", "version": 1})


@pytest.mark.parametrize("path", ["/admin/api/import", "/admin/api/import/preview"])
@pytest.mark.parametrize("body", [None, [], {"based_on": 0}, {"toml": 5, "based_on": 0}])
def test_a_malformed_import_body_is_a_bad_request(world: World, path: str, body: object) -> None:
    csrf = world.login()
    fresh(world, csrf)
    answer = world.call("POST", path, body, csrf=csrf)
    assert answer.status == 400
    assert json_of(answer) == {"error": "bad_request"}


def test_an_import_without_an_int_version_is_a_bad_request(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    for based_on in ("0", True, None):
        body = {"toml": STATIONS_TOML, "based_on": based_on}
        answer = world.call("POST", "/admin/api/import", body, csrf=csrf)
        assert (answer.status, json_of(answer)) == (400, {"error": "bad_request"})


@pytest.mark.parametrize("path", ["/admin/api/import", "/admin/api/import/preview"])
def test_import_needs_a_session(world: World, path: str) -> None:
    assert world.call("POST", path, {"toml": "", "based_on": 0}).status == 401


def test_export_needs_a_session(world: World) -> None:
    assert world.call("GET", "/admin/api/export").status == 401


@pytest.mark.parametrize(
    "body",
    [None, [], {"state": {}}, {"based_on": "0", "state": {}}, {"based_on": True, "state": {}}],
)
def test_a_save_without_a_version_is_a_bad_request(world: World, body: object) -> None:
    csrf = world.login()
    answer = world.call("POST", "/admin/api/config", body, csrf=csrf)
    assert answer.status == 400
    assert json_of(answer) == {"error": "bad_request"}


def test_a_stale_save_is_refused(world: World) -> None:
    csrf = world.login()
    save(world, csrf, 0, state_of(station("a")))
    answer = save(world, csrf, 0, state_of(station("b")))
    assert answer.status == 409
    assert json_of(answer) == {"error": "stale", "version": 1}
    stored = json_of(world.call("GET", "/admin/api/config"))["state"]
    assert [c["id"] for c in stored["channels"]] == ["a"]
    assert world.applied == [1]


def test_an_invalid_state_names_the_field(world: World) -> None:
    csrf = world.login()
    bad = station("a")
    bad["doc"]["stream_url"] = "ftp://x"
    answer = save(world, csrf, 0, state_of(bad))
    assert answer.status == 400
    assert json_of(answer)["error"] == "invalid"
    assert "channels[0].stream_url" in json_of(answer)["message"]
    shape = save(world, csrf, 0, {"channels": [5]})
    assert json_of(shape) == {"error": "invalid", "message": "channels[0]: expected an object"}
    assert world.applied == []


def test_a_dangling_default_is_refused(world: World) -> None:
    csrf = world.login()
    answer = save(world, csrf, 0, state_of(station("a"), player={"default_channel": "gone"}))
    assert answer.status == 400
    assert "default_channel" in json_of(answer)["message"]


def test_a_refused_pattern_names_the_field(world: World) -> None:
    csrf = world.login()
    rules = {"played_by": "(a+)+$"}
    answer = save(world, csrf, 0, state_of(station("a"), station("b", rules=rules)))
    assert answer.status == 400
    message = json_of(answer)["message"]
    assert "channels[1].rules.played_by" in message
    assert "nested repetition" in message


def test_a_slow_new_pattern_is_refused_naming_its_field(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()

    def slow(patterns: list[str], _samples: list[str]) -> tuple[int | None, str]:
        return patterns.index("^(x)$"), "too slow on long titles; simplify the pattern"

    monkeypatch.setattr("tunebox.rulecheck.check", slow)
    stations = [
        station("a", rules={"station_id": "^y$"}),
        station("b", rules={"played_by": "^(x)$"}),
    ]
    answer = save(world, csrf, 0, state_of(*stations))
    assert answer.status == 400
    assert json_of(answer)["message"].startswith("channels[1].rules.played_by: too slow")


def test_a_check_that_cannot_run_refuses_the_save(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()

    def unavailable(_patterns: list[str], _samples: list[str]) -> tuple[int | None, str]:
        return None, "could not run"

    monkeypatch.setattr("tunebox.rulecheck.check", unavailable)
    answer = save(world, csrf, 0, state_of(station("a", rules={"played_by": "^(x)$"})))
    assert json_of(answer) == {"error": "invalid", "message": "rules: could not run"}


def test_stored_patterns_are_not_run_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    csrf = world.login()
    rules = {"played_by": r"\s*\(Played by ([^)]*)\)\s*$"}
    assert save(world, csrf, 0, state_of(station("a", rules=rules))).status == 200
    runs: list[list[str]] = []

    def counting(patterns: list[str], _samples: list[str]) -> None:
        runs.append(patterns)

    monkeypatch.setattr("tunebox.rulecheck.check", counting)
    assert save(world, csrf, 1, state_of(station("a", rules=rules), station("b"))).status == 200
    assert runs == []


def test_recent_titles_are_gathered_only_for_a_new_pattern(world: World) -> None:
    csrf = world.login()
    assert save(world, csrf, 0, state_of(station("a"))).status == 200
    assert save(world, csrf, 1, state_of(station("a", name="Renamed"))).status == 200
    assert world.samples_asked == 0
    rules = {"played_by": r"\s*\(Played by ([^)]*)\)\s*$"}
    assert save(world, csrf, 2, state_of(station("a", rules=rules))).status == 200
    assert world.samples_asked == 1


def test_a_stale_save_never_runs_the_rule_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()
    save(world, csrf, 0, state_of(station("owr")))
    ran: list[int] = []

    def counting(_patterns: list[str], _samples: list[str]) -> None:
        ran.append(1)

    monkeypatch.setattr("tunebox.rulecheck.check", counting)
    with_rule = station("owr", rules={"played_by": "^(.+) - live$"})
    assert save(world, csrf, 0, state_of(with_rule)).status == 409
    assert ran == []


def test_an_invalid_pattern_is_named_by_validation(world: World) -> None:
    csrf = world.login()
    answer = save(world, csrf, 0, state_of(station("owr", rules={"played_by": "("})))
    assert answer.status == 400
    assert json_of(answer)["message"].startswith("channels[0].rules.played_by")


def test_restore_applies_the_old_state(world: World) -> None:
    csrf = world.login()
    save(world, csrf, 0, state_of(station("a")))
    save(world, csrf, 1, state_of(station("a"), station("b")))
    entries = json_of(world.call("GET", "/admin/api/history"))["entries"]
    detail = json_of(world.call("GET", f"/admin/api/history/{entries[0]['id']}"))
    assert detail["entry"]["id"] == entries[0]["id"]
    assert [c["id"] for c in detail["before"]["channels"]] == ["a"]
    assert [c["id"] for c in detail["after"]["channels"]] == ["a", "b"]
    restore = f"/admin/api/history/{entries[0]['id']}/restore"
    assert world.call("POST", restore, {"based_on": 1}, csrf=csrf).status == 409
    answer = world.call("POST", restore, {"based_on": 2}, csrf=csrf)
    assert json_of(answer) == {"version": 3, "changed": True, "applied": True}
    stored = json_of(world.call("GET", "/admin/api/config"))["state"]
    assert [c["id"] for c in stored["channels"]] == ["a"]
    assert world.applied == [1, 1, 1]
    history = json_of(world.call("GET", "/admin/api/history"))["entries"]
    assert [e["kind"] for e in history] == ["restore", "edit", "edit"]
    assert history[0]["target"] == f"history {entries[0]['id']}"


def test_unknown_history_entries(world: World) -> None:
    csrf = world.login()
    assert world.call("GET", "/admin/api/history/999").status == 404
    assert world.call("GET", "/admin/api/history/abc").status == 404
    assert world.call("GET", "/admin/api/history/1/x").status == 404
    restore = world.call("POST", "/admin/api/history/999/restore", {"based_on": 0}, csrf=csrf)
    assert restore.status == 404
    bad = world.call("POST", "/admin/api/history/999/restore", {}, csrf=csrf)
    assert bad.status == 400


def test_a_group_inside_a_station_document_is_refused(world: World) -> None:
    csrf = world.login()
    bad = station("a")
    bad["doc"]["group"] = "Pop"
    answer = save(world, csrf, 0, state_of(bad))
    assert answer.status == 400
    assert "channels[0].doc.group" in json_of(answer)["message"]


def fresh(world: World, csrf: str) -> None:
    """Re-authenticates with the next step's code (the login used this one)."""
    response = world.call("POST", "/admin/api/account/reauth", {"code": world.code(1)}, csrf=csrf)
    assert response.status == 204, response.body


@pytest.mark.parametrize(
    "path",
    [
        "account/reauth",
        "account/password",
        "account/totp/start",
        "account/totp/finish",
        "account/recovery",
        "account/sessions/0123456789abcdef/end",
    ],
)
def test_account_posts_need_a_session_and_the_token(world: World, path: str) -> None:
    assert world.call("POST", f"/admin/api/{path}", {}).status == 401
    world.login()
    assert world.call("POST", f"/admin/api/{path}", {}).status == 403


def test_account_lists_the_sessions_and_marks_this_one(world: World) -> None:
    world.login()
    world.clock.now += 30
    mine = world.cookies.copy()
    world.cookies.clear()
    world.login()
    world.cookies = mine
    body = json_of(world.call("GET", "/admin/api/account"))
    assert len(body["sessions"]) == 2
    assert [s["current"] for s in body["sessions"]] == [False, True]
    assert body["fresh"] is False
    assert all(len(s["id"]) == 16 for s in body["sessions"])


def test_sensitive_changes_need_a_fresh_code(world: World) -> None:
    csrf = world.login()
    response = world.call("POST", "/admin/api/account/recovery", {}, csrf=csrf)
    assert (response.status, json_of(response)["error"]) == (403, "reauth_required")
    fresh(world, csrf)
    assert json_of(world.call("GET", "/admin/api/account"))["fresh"] is True
    response = world.call("POST", "/admin/api/account/recovery", {}, csrf=csrf)
    assert response.status == 200
    assert len(json_of(response)["recovery_codes"]) == 10


def test_a_wrong_reauth_code_is_refused_logged_and_limited(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    csrf = world.login()
    reauth = "/admin/api/account/reauth"
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            response = world.call("POST", reauth, {"code": "000000"}, csrf=csrf)
            assert (response.status, json_of(response)["error"]) == (400, "code_wrong")
        limited = world.call("POST", reauth, {"code": world.code(1)}, csrf=csrf)
    assert limited.status == 429
    failed = [r for r in caplog.records if r.getMessage() == "admin_reauth_failed"]
    assert len(failed) == 5
    assert failed[0].__dict__["fields"] == {"client": "198.51.100.7", "reason": "totp"}
    # Never the ban scenarios' event: the owner's typo must not ban the house.
    assert not [r for r in caplog.records if r.getMessage() == "admin_login_failed"]


def test_a_password_change_keeps_this_browser_and_its_lane(world: World) -> None:
    csrf = world.login()
    old_known = world.cookies["__Secure-tunebox_known"]
    fresh(world, csrf)
    body = {"password": "another long password"}
    response = world.call("POST", "/admin/api/account/password", body, csrf=csrf)
    assert response.status == 204
    assert world.cookies["__Secure-tunebox_known"] != old_known
    assert world.call("GET", "/admin/api/account").status == 200
    headers = (("Cookie", f"__Secure-tunebox_known={old_known}"),)
    old = Request("POST", "/admin/api/login", headers, b"", "x")
    assert not world.admin._known(old)  # pyright: ignore[reportPrivateUsage]


def test_a_short_new_password_is_refused(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    response = world.call("POST", "/admin/api/account/password", {"password": "short"}, csrf=csrf)
    assert (response.status, json_of(response)["error"]) == (400, "password_too_short")


def test_a_new_authenticator_needs_its_own_code(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    started = json_of(world.call("POST", "/admin/api/account/totp/start", {}, csrf=csrf))
    secret = base64.b32decode(started["secret"] + "=" * (-len(started["secret"]) % 8))
    assert started["uri"].startswith("otpauth://totp/")
    finish = "/admin/api/account/totp/finish"
    wrong = world.call("POST", finish, {"code": world.code(1)}, csrf=csrf)
    assert (wrong.status, json_of(wrong)["error"]) == (400, "code_wrong")
    right = {"code": crypto.totp(secret, int(world.clock.now // 30))}
    assert world.call("POST", finish, right, csrf=csrf).status == 204
    again = world.call("POST", finish, right, csrf=csrf)
    assert (again.status, json_of(again)["error"]) == (400, "no_new_secret")


def test_finishing_without_starting_is_refused(world: World) -> None:
    csrf = world.login()
    fresh(world, csrf)
    response = world.call("POST", "/admin/api/account/totp/finish", {"code": "123456"}, csrf=csrf)
    assert (response.status, json_of(response)["error"]) == (400, "no_new_secret")


def test_ending_another_session(world: World) -> None:
    world.login()
    world.clock.now += 30
    mine = world.cookies.copy()
    world.cookies.clear()
    world.login()
    theirs = world.cookies.copy()
    world.cookies = mine
    csrf = json_of(world.call("GET", "/admin/api/state"))["session"]["csrf"]
    listed = json_of(world.call("GET", "/admin/api/account"))["sessions"]
    other = next(s["id"] for s in listed if not s["current"])
    own = next(s["id"] for s in listed if s["current"])
    response = world.call("POST", f"/admin/api/account/sessions/{own}/end", {}, csrf=csrf)
    assert (response.status, json_of(response)["error"]) == (400, "this_session")
    end_other = f"/admin/api/account/sessions/{other}/end"
    assert world.call("POST", end_other, {}, csrf=csrf).status == 204
    assert world.call("POST", end_other, {}, csrf=csrf).status == 404
    world.cookies = theirs
    assert world.call("GET", "/admin/api/account").status == 401


def test_account_events_never_carry_secrets(world: World, caplog: pytest.LogCaptureFixture) -> None:
    csrf = world.login()
    with caplog.at_level(logging.DEBUG):
        fresh(world, csrf)
        body = {"password": "another long password"}
        world.call("POST", "/admin/api/account/password", body, csrf=csrf)
        codes = json_of(world.call("POST", "/admin/api/account/recovery", {}, csrf=csrf))
    text = " ".join(json.dumps(r.__dict__.get("fields", {})) for r in caplog.records)
    assert "another long password" not in text
    assert codes["recovery_codes"][0] not in text
    changes = [r for r in caplog.records if r.getMessage() == "admin_change"]
    assert [r.__dict__["fields"]["kind"] for r in changes] == ["password", "recovery_codes"]


@pytest.mark.parametrize("before", ["99999999999999999999", "²", "-1", "1e3"])
def test_history_ids_are_plain_digits(world: World, before: str) -> None:
    world.login()
    assert world.call("GET", f"/admin/api/history?before={before}").status == 400
    assert world.call("GET", f"/admin/api/history/{before}").status == 404


def test_pin_endpoints_need_a_session_and_the_token(world: World) -> None:
    assert world.call("GET", "/admin/api/pin").status == 401
    assert world.call("POST", "/admin/api/pin", {"mode": "off"}).status == 401
    world.login()
    assert world.call("POST", "/admin/api/pin", {"mode": "off"}).status == 403


def test_set_the_pin_and_its_mode(world: World, caplog: pytest.LogCaptureFixture) -> None:
    csrf = world.login()
    assert json_of(world.call("GET", "/admin/api/pin")) == {
        "mode": "off",
        "set": False,
        "revision": "off.0",
    }
    with caplog.at_level(logging.INFO):
        response = world.call(
            "POST",
            "/admin/api/pin",
            {"mode": "stations", "pin": "123456", "based_on": "off.0"},
            csrf=csrf,
        )
    assert json_of(response) == {"mode": "stations", "set": True, "revision": "stations.1"}
    assert world.pin.status().mode == "stations"
    change = next(r for r in caplog.records if r.getMessage() == "admin_change")
    assert change.__dict__["fields"] == {
        "kind": "pin",
        "mode": "stations",
        "new_pin": True,
        "client": "198.51.100.7",
    }
    assert "123456" not in caplog.text
    site = world.call(
        "POST", "/admin/api/pin", {"mode": "site", "based_on": "stations.1"}, csrf=csrf
    )
    assert json_of(site) == {"mode": "site", "set": True, "revision": "site.1"}
    off = world.call("POST", "/admin/api/pin", {"mode": "off", "based_on": "site.1"}, csrf=csrf)
    assert json_of(off) == {"mode": "off", "set": True, "revision": "off.1"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"mode": "site", "based_on": "off.0"}, "invalid"),
        ({"mode": "site", "pin": "12345", "based_on": "off.0"}, "invalid"),
        ({"mode": "everything", "pin": "123456", "based_on": "off.0"}, "invalid"),
        ({"mode": True, "based_on": "off.0"}, "bad_request"),
        ({"pin": "123456", "based_on": "off.0"}, "bad_request"),
        ({"mode": "site", "pin": 123456, "based_on": "off.0"}, "bad_request"),
        ({"mode": "site", "pin": "123456", "based_on": 0}, "bad_request"),
    ],
)
def test_a_bad_pin_change_is_refused(world: World, body: dict[str, object], error: str) -> None:
    csrf = world.login()
    response = world.call("POST", "/admin/api/pin", body, csrf=csrf)
    assert (response.status, json_of(response)["error"]) == (400, error)
    assert world.pin.status().mode == "off"


def test_pin_changes_stay_out_of_the_history(world: World) -> None:
    csrf = world.login()
    world.call(
        "POST",
        "/admin/api/pin",
        {"mode": "site", "pin": "123456", "based_on": "off.0"},
        csrf=csrf,
    )
    assert json_of(world.call("GET", "/admin/api/history"))["entries"] == []


def test_a_locked_reauth_is_not_a_ban_signal(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    csrf = world.login()
    reauth = "/admin/api/account/reauth"
    for _ in range(5):
        world.call("POST", reauth, {"code": "000000"}, csrf=csrf)
    with caplog.at_level(logging.WARNING):
        assert world.call("POST", reauth, {"code": "000000"}, csrf=csrf).status == 429
    limited = [r for r in caplog.records if r.getMessage() == "admin_rate_limited"]
    assert limited[0].__dict__["fields"]["why"] == "reauth"


def test_a_repeated_header_counts_as_absent() -> None:
    request = Request("POST", "/admin/api/login", (("Origin", "a"), ("origin", "b")), b"", "x")
    assert request.header("Origin") is None
    single = Request("POST", "/admin/api/login", (("Origin", "a"),), b"", "x")
    assert single.header("origin") == "a"


def test_a_repeated_cookie_counts_as_absent(world: World) -> None:
    csrf = world.login()
    token = world.cookies["__Secure-tunebox_admin"]
    world.cookies.clear()
    headers = (
        ("Cookie", f"__Secure-tunebox_admin={token}; __Secure-tunebox_admin=tossed"),
        ("Origin", ORIGIN),
        ("Content-Type", "application/json"),
        ("X-Tunebox-CSRF", csrf),
    )
    answer = world.admin.handle(Request("GET", "/admin/api/config", headers, b"", "x"))
    assert answer.status == 401


def test_a_crashing_route_answers_500_with_the_admin_headers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_request: Request) -> Response:
        raise RuntimeError("broken")

    monkeypatch.setattr(world.admin, "_state", boom)
    answer = world.call("GET", "/admin/api/state")
    assert answer.status == 500
    assert json_of(answer) == {"error": "internal"}
    assert dict(answer.headers)["Cache-Control"] == "no-store"


def head_only(
    world: World,
    path: str,
    csrf: str | None,
    origin: str = ORIGIN,
    length: int = MAX_BODY + 1,
) -> Request:
    headers: list[tuple[str, str]] = [
        ("Origin", origin),
        ("Content-Type", "application/json"),
        ("Content-Length", str(length)),
    ]
    if csrf:
        headers.append(("X-Tunebox-CSRF", csrf))
    if world.cookies:
        headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in world.cookies.items())))
    return Request("POST", path, tuple(headers), b"", "198.51.100.7")


def limit_of(world: World, request: Request) -> tuple[int, int | None]:
    """The limit and the status of the refusal that comes with it."""
    limit, refusal = world.admin.body_limit(request)
    return limit, None if refusal is None else refusal.status


def test_a_large_body_needs_a_live_session_and_token(world: World) -> None:
    path = "/admin/api/config"
    assert limit_of(world, head_only(world, path, None)) == (MAX_BODY, 401)
    csrf = world.login()
    assert limit_of(world, head_only(world, path, None)) == (MAX_BODY, 403)
    assert limit_of(world, head_only(world, path, "wrong")) == (MAX_BODY, 403)
    assert limit_of(world, head_only(world, path, csrf, "https://evil.example")) == (MAX_BODY, 403)
    assert limit_of(world, head_only(world, path, csrf)) == (MAX_STATE_BODY, None)
    for name in ("import", "import/preview"):
        large = head_only(world, f"/admin/api/{name}", csrf)
        assert limit_of(world, large) == (MAX_STATE_BODY, None)
    # Only the routes that need it; the size refusal is the server's 413.
    assert limit_of(world, head_only(world, "/admin/api/pin", csrf)) == (MAX_BODY, None)


def test_a_refusal_carries_the_admin_headers(world: World) -> None:
    _limit, refusal = world.admin.body_limit(head_only(world, "/admin/api/config", None))
    assert refusal is not None
    assert dict(refusal.headers)["Cache-Control"] == "no-store"


def test_a_body_within_the_limit_is_judged_after_it_is_read(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.login()
    asked: list[str] = []
    monkeypatch.setattr(world.accounts, "session", asked.append)
    with caplog.at_level(logging.DEBUG):
        within = head_only(world, "/admin/api/config", "wrong", length=MAX_BODY)
        assert limit_of(world, within) == (MAX_BODY, None)
        unnamed = Request("POST", "/admin/api/config", (("Origin", ORIGIN),), b"", "x")
        assert limit_of(world, unnamed) == (MAX_BODY, None)
        get = Request("GET", "/admin/api/config", (), b"", "198.51.100.7")
        assert limit_of(world, get) == (MAX_BODY, None)
    assert "admin_csrf_rejected" not in caplog.text
    assert asked == []


def test_a_large_body_while_sessions_cannot_be_read_is_read_only(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()

    def unreadable(_token: str) -> None:
        raise SettingsUnavailable("down")

    monkeypatch.setattr(world.accounts, "session", unreadable)
    assert limit_of(world, head_only(world, "/admin/api/config", csrf)) == (MAX_BODY, 503)


def test_a_refused_oversize_save_is_logged_once(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.login()
    with caplog.at_level(logging.WARNING):
        assert limit_of(world, head_only(world, "/admin/api/config", "wrong")) == (MAX_BODY, 403)
    rejected = [r for r in caplog.records if r.getMessage() == "admin_csrf_rejected"]
    assert len(rejected) == 1


def test_saves_and_restores_log_the_client(world: World, caplog: pytest.LogCaptureFixture) -> None:
    csrf = world.login()
    world.apply_result = 0
    with caplog.at_level(logging.INFO):
        save(world, csrf, 0, state_of(station("owr")))
    for name in ("admin_change_not_live", "admin_change"):
        record = next(r for r in caplog.records if r.getMessage() == name)
        assert record.__dict__["fields"]["client"] == "198.51.100.7"


def test_saving_the_stored_state_again_changes_nothing(world: World) -> None:
    csrf = world.login()
    first = save(world, csrf, 0, state_of(station("owr")))
    assert json_of(first) == {"version": 1, "changed": True, "applied": True}
    again = save(world, csrf, 1, state_of(station("owr")))
    assert json_of(again) == {"version": 1, "changed": False, "applied": True}
    # The unchanged save asks for the apply again; apply itself is a no-op when live.
    assert world.applied == [1, 1]


def test_a_long_target_is_refused(world: World) -> None:
    csrf = world.login()
    body = {"based_on": 0, "state": state_of(station("owr")), "target": "x" * 201}
    assert world.call("POST", "/admin/api/config", body, csrf=csrf).status == 400


def test_a_wrong_password_and_a_wrong_code_take_the_same_floor(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    floors: list[float] = []
    monkeypatch.setattr(world.admin.gate, "floor", floors.append)
    world.call("POST", "/admin/api/login", {"password": "wrong password!", "code": "000000"})
    world.clock.now += 2  # past the shared pace
    world.call(
        "POST",
        "/admin/api/login",
        {"password": PASSWORD, "code": "000000"},
        client="198.51.100.8",
    )
    assert len(floors) == 2


def test_the_floor_follows_every_other_work_of_a_failed_login(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def floor(_started: float) -> None:
        order.append("floor")

    def suspected() -> bool:
        order.append("read")
        return False

    monkeypatch.setattr(world.admin.gate, "floor", floor)
    monkeypatch.setattr(world.accounts, "totp_suspected", suspected)
    world.call("POST", "/admin/api/login", {"password": PASSWORD, "code": "000000"})
    assert order == ["read", "floor"]


def test_a_password_change_whose_session_ended_asks_to_log_in(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()
    fresh(world, csrf)

    def ended(_session: object, _password: str) -> int:
        raise SessionEnded("the session ended")

    monkeypatch.setattr(world.accounts, "change_password", ended)
    answer = world.call(
        "POST", "/admin/api/account/password", {"password": "another long password"}, csrf=csrf
    )
    assert answer.status == 401
    assert json_of(answer) == {"error": "login_required"}


def test_recovery_codes_are_shown_even_when_the_generation_cannot_be_read_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()
    fresh(world, csrf)

    def unreadable() -> int:
        raise SettingsUnavailable("locked")

    monkeypatch.setattr(world.accounts, "generation", unreadable)
    answer = world.call("POST", "/admin/api/account/recovery", {}, csrf=csrf)
    assert answer.status == 200
    assert json_of(answer)["recovery_codes"]


def test_an_account_rule_is_a_400_not_a_500(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    csrf = world.login()
    fresh(world, csrf)

    def refuse(_session: object, _password: str) -> int:
        raise AccountError("some future rule")

    monkeypatch.setattr(world.accounts, "change_password", refuse)
    answer = world.call(
        "POST", "/admin/api/account/password", {"password": "another long password"}, csrf=csrf
    )
    assert answer.status == 400
    assert json_of(answer) == {"error": "invalid", "message": "some future rule"}


def test_state_names_the_limits(world: World) -> None:
    assert json_of(world.call("GET", "/admin/api/state"))["limits"] == {
        "password_min": 12,
        "pin_min": 6,
        "pin_max": 8,
    }


def test_a_save_that_cannot_go_live_says_so(world: World) -> None:
    csrf = world.login()
    world.apply_result = 0
    answer = save(world, csrf, 0, state_of(station("owr")))
    assert answer.status == 200
    assert json_of(answer) == {"version": 1, "changed": True, "applied": False}


def test_saving_again_retries_a_change_that_is_not_live(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    csrf = world.login()
    world.apply_result = 0
    first = save(world, csrf, 0, state_of(station("owr")))
    assert json_of(first) == {"version": 1, "changed": True, "applied": False}
    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = save(world, csrf, 1, state_of(station("owr")))
        assert json_of(again) == {"version": 1, "changed": False, "applied": False}
        world.apply_result = None
        live = save(world, csrf, 1, state_of(station("owr")))
    assert json_of(live) == {"version": 1, "changed": False, "applied": True}
    assert world.applied == [1, 1, 1]
    assert "admin_change" not in caplog.text


def test_a_pin_change_needs_the_revision_it_was_based_on(world: World) -> None:
    csrf = world.login()
    status = json_of(world.call("GET", "/admin/api/pin"))
    assert status == {"mode": "off", "set": False, "revision": "off.0"}
    body = {"mode": "stations", "pin": "123456", "based_on": "off.0"}
    assert world.call("POST", "/admin/api/pin", body, csrf=csrf).status == 200
    stale = {"mode": "off", "based_on": "off.0"}
    answer = world.call("POST", "/admin/api/pin", stale, csrf=csrf)
    assert answer.status == 409
    assert json_of(answer) == {"error": "stale", "revision": "stations.1"}
    assert world.pin.status().mode == "stations"
    assert world.call("POST", "/admin/api/pin", {"mode": "off"}, csrf=csrf).status == 400


def _unwritable(*_args: object) -> object:
    raise SettingsWriteError("read-only")


def test_an_unwritable_database_does_not_tell_a_right_password(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(world.settings, "transaction", _unwritable)
    tries = [
        ("wrong password!", "000000", "password"),
        (PASSWORD, "000000", "totp"),
        (PASSWORD, "zzzz-zzzz-zzzz", "recovery"),
    ]
    with caplog.at_level(logging.WARNING):
        for number, (password, code, _reason) in enumerate(tries):
            world.clock.now += 2  # past the shared pace
            body = {"password": password, "code": code}
            answer = world.call("POST", "/admin/api/login", body, client=f"198.51.100.{number}")
            assert (answer.status, json_of(answer)) == (401, {"error": "login_failed"})
    failed = [r for r in caplog.records if r.getMessage() == "admin_login_failed"]
    assert [r.__dict__["fields"]["reason"] for r in failed] == [t[2] for t in tries]


def test_an_unreadable_account_does_not_lock_the_owner_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> None:
        raise SettingsUnavailable("down")

    monkeypatch.setattr(world.accounts, "_admin", unreadable)
    for _ in range(6):
        world.clock.now += 2
        good = {"password": PASSWORD, "code": world.code()}
        assert world.call("POST", "/admin/api/login", good).status == 503
    monkeypatch.undo()
    world.clock.now += 2
    good = {"password": PASSWORD, "code": world.code()}
    assert world.call("POST", "/admin/api/login", good).status == 200


def test_an_unreadable_generation_does_not_lock_the_owner_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> int:
        raise SettingsUnavailable("down")

    with monkeypatch.context() as patch:
        patch.setattr(world.accounts, "generation", unreadable)
        for _ in range(6):
            world.clock.now += 2
            good = {"password": PASSWORD, "code": world.code()}
            assert world.call("POST", "/admin/api/login", good).status == 503
    world.clock.now += 2
    good = {"password": PASSWORD, "code": world.code()}
    assert world.call("POST", "/admin/api/login", good).status == 200


def test_an_unreadable_account_gives_the_reauth_slot_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()

    def unreadable() -> None:
        raise SettingsUnavailable("down")

    monkeypatch.setattr(world.accounts, "_admin", unreadable)
    for _ in range(6):
        answer = world.call("POST", "/admin/api/account/reauth", {"code": "000000"}, csrf=csrf)
        assert answer.status == 503
    monkeypatch.undo()
    answer = world.call("POST", "/admin/api/account/reauth", {"code": world.code(1)}, csrf=csrf)
    assert answer.status == 204


def test_a_login_that_cannot_be_answered_still_takes_the_floor(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    floors: list[float] = []
    monkeypatch.setattr(world.admin.gate, "floor", floors.append)

    def unreadable() -> None:
        raise SettingsUnavailable("down")

    with monkeypatch.context() as patch:
        patch.setattr(world.accounts, "_admin", unreadable)
        good = {"password": PASSWORD, "code": world.code()}
        assert world.call("POST", "/admin/api/login", good).status == 503
    assert len(floors) == 1
    with monkeypatch.context() as patch:
        patch.setattr(Accounts, "_open", _unwritable)
        world.clock.now += 2
        good = {"password": PASSWORD, "code": world.code()}
        assert world.call("POST", "/admin/api/login", good).status == 503
    assert len(floors) == 2


def test_a_session_route_reads_the_session_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.login()
    calls: list[str] = []
    real = world.accounts.session

    def counted(token: str) -> Session | None:
        calls.append(token)
        return real(token)

    monkeypatch.setattr(world.accounts, "session", counted)
    assert world.call("GET", "/admin/api/account").status == 200
    assert len(calls) == 1


def test_account_rules_in_totp_and_recovery_are_400_not_500(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = world.login()
    fresh(world, csrf)

    def refuse(*_args: object) -> int:
        raise AccountError("some future rule")

    monkeypatch.setattr(world.accounts, "replace_recovery_codes", refuse)
    answer = world.call("POST", "/admin/api/account/recovery", {}, csrf=csrf)
    assert answer.status == 400
    expected = {"error": "invalid", "message": "some future rule"}
    assert json_of(answer) == expected
    started = json_of(world.call("POST", "/admin/api/account/totp/start", {}, csrf=csrf))
    new_secret = base64.b32decode(started["secret"] + "=" * (-len(started["secret"]) % 8))
    code = crypto.totp(new_secret, int(world.clock.now // 30))
    monkeypatch.setattr(world.accounts, "replace_totp", refuse)
    answer = world.call("POST", "/admin/api/account/totp/finish", {"code": code}, csrf=csrf)
    assert answer.status == 400
    assert json_of(answer) == expected


def test_an_ended_session_spends_no_reauth_slot(world: World) -> None:
    csrf = world.login()
    assert world.call("POST", "/admin/api/logout", {}, csrf=csrf).status == 204
    for _ in range(6):
        stale = world.call("POST", "/admin/api/account/reauth", {"code": "000000"}, csrf=csrf)
        assert stale.status == 401
    world.clock.now += 30
    csrf = world.login()
    world.clock.now += 30
    good = {"code": world.code()}
    assert world.call("POST", "/admin/api/account/reauth", good, csrf=csrf).status == 204


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def upload(
    world: World, csrf: str | None, body: bytes, content_type: str = "image/png"
) -> Response:
    headers = [("Origin", ORIGIN), ("Content-Type", content_type)]
    if csrf:
        headers.append(("X-Tunebox-CSRF", csrf))
    if world.cookies:
        headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in world.cookies.items())))
    return world.admin.handle(
        Request("POST", "/admin/api/logos", tuple(headers), body, "198.51.100.7")
    )


def test_an_uploaded_logo_answers_its_reference(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    csrf = world.login()
    with caplog.at_level(logging.INFO):
        answer = upload(world, csrf, PNG)
    assert answer.status == 200
    ref = json_of(answer)["logo"]
    assert json_of(answer)["kind"] == "png"
    shown = world.call("GET", f"/admin/api/logos/{ref.removeprefix('blob:')}")
    assert shown.status == 200 and shown.body == PNG
    assert shown.content_type == "image/png"
    assert ("Content-Security-Policy", "sandbox") in shown.headers
    event = next(r for r in caplog.records if r.getMessage() == "admin_logo_uploaded")
    assert event.__dict__["fields"] == {"client": "198.51.100.7", "kind": "png", "bytes": len(PNG)}


def test_an_upload_that_is_not_an_image_is_refused(world: World) -> None:
    csrf = world.login()
    answer = upload(world, csrf, b"<html><script>alert(1)</script>", "image/png")
    assert answer.status == 400 and json_of(answer) == {"error": "not_an_image"}
    assert not (world.tmp / "blobs").exists()


def test_an_upload_needs_an_image_type(world: World) -> None:
    csrf = world.login()
    assert upload(world, csrf, PNG, "text/plain").status == 415
    assert upload(world, csrf, PNG, "application/json").status == 415
    other = world.call("POST", "/admin/api/config", {}, csrf=csrf, content_type="image/png")
    assert other.status == 415 and json_of(other) == {"error": "json_only"}


def test_an_upload_needs_a_session_and_its_token(world: World) -> None:
    csrf = world.login()
    assert upload(world, None, PNG).status == 403
    assert upload(world, "wrong", PNG).status == 403
    world.cookies.clear()
    assert upload(world, csrf, PNG).status == 401


@pytest.mark.parametrize("sha", ["../x", "A" * 64, "0" * 63, "1" * 64])
def test_a_blob_view_takes_only_a_stored_reference(world: World, sha: str) -> None:
    world.login()
    assert world.call("GET", f"/admin/api/logos/{sha}").status == 404


def test_a_blob_view_needs_a_session(world: World) -> None:
    assert world.call("GET", "/admin/api/logos/" + "1" * 64).status == 401


def test_a_save_naming_an_absent_blob_is_refused(world: World) -> None:
    csrf = world.login()
    gone = "blob:" + "1" * 64
    answer = save(world, csrf, 0, state_of(station("a", logo=gone)))
    assert answer.status == 400
    assert json_of(answer)["message"] == f"channels[0].logo: no uploaded logo {gone}"
    assert world.settings.load()[1] == 0


def test_a_save_naming_a_stored_blob_goes_through(world: World) -> None:
    csrf = world.login()
    ref = json_of(upload(world, csrf, PNG))["logo"]
    assert save(world, csrf, 0, state_of(station("a", logo=ref))).status == 200


def test_a_preview_names_an_absent_blob(world: World) -> None:
    csrf = world.login()
    gone = "blob:" + "1" * 64
    text = STATIONS_TOML.replace('name = "Late"', f'name = "Late"\nlogo = "{gone}"')
    answer = world.call("POST", "/admin/api/import/preview", {"toml": text}, csrf=csrf)
    assert answer.status == 400
    assert gone in json_of(answer)["message"]


def test_a_logo_upload_is_granted_its_own_limit_to_a_live_session(world: World) -> None:
    path = "/admin/api/logos"
    big = MAX_BLOB // 2

    def asked(csrf: str | None) -> tuple[int, int | None]:
        request = head_only(world, path, csrf, length=big)
        request = dataclasses.replace(
            request,
            headers=tuple(
                ("Content-Type", "image/png") if k == "Content-Type" else (k, v)
                for k, v in request.headers
            ),
        )
        return limit_of(world, request)

    assert asked(None) == (MAX_BODY, 401)
    csrf = world.login()
    assert asked("wrong") == (MAX_BODY, 403)
    assert asked(csrf) == (MAX_BLOB, None)


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.mark.parametrize("declared", ["image/svg+xml", "image/png"])
def test_an_svg_is_stored_and_served_as_svg_whatever_it_declared(
    world: World, declared: str
) -> None:
    csrf = world.login()
    answer = upload(world, csrf, SVG, declared)
    assert answer.status == 200 and json_of(answer)["kind"] == "svg"
    shown = world.call("GET", f"/admin/api/logos/{json_of(answer)['logo'].removeprefix('blob:')}")
    assert shown.body == SVG and shown.content_type == "image/svg+xml"
    assert ("Content-Security-Policy", "sandbox") in shown.headers


def test_an_upload_that_cannot_be_stored_is_read_only(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_self: Path, _data: bytes) -> int:
        raise OSError(28, "No space left on device")

    csrf = world.login()
    monkeypatch.setattr(Path, "write_bytes", full)
    with caplog.at_level(logging.WARNING):
        answer = upload(world, csrf, PNG)
    assert answer.status == 503 and json_of(answer) == {"error": "read_only"}
    event = next(r for r in caplog.records if r.getMessage() == "logo_store_failed")
    assert event.__dict__["fields"] == {"client": "198.51.100.7", "errno": 28}


def test_history_keeps_its_blobs(world: World) -> None:
    csrf = world.login()
    old = json_of(upload(world, csrf, PNG))["logo"]
    assert save(world, csrf, 0, state_of(station("owr", logo=old))).status == 200
    new = json_of(upload(world, csrf, PNG + b"new"))["logo"]
    assert save(world, csrf, 1, state_of(station("owr", logo=new))).status == 200
    assert old in world.settings.referenced_blobs()
    world.blobs.collect(keep=world.settings.referenced_blobs(), older_than_s=0)
    restored = world.call("POST", "/admin/api/history/2/restore", {"based_on": 2}, csrf=csrf)
    assert restored.status == 200
    assert json_of(restored)["changed"] is True
