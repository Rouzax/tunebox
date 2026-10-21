import contextlib
import dataclasses
import http.client
import io
import ipaddress
import json
import logging
import socket
import struct
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from tests.fakes import FakeSource, NoJitter, channel_config
from tests.test_admin import PASSWORD, SECRET
from tunebox import crypto
from tunebox.accounts import Accounts
from tunebox.admin import MAX_BODY, MAX_STATE_BODY, Admin, Request, Response, Route
from tunebox.blobs import Blobs
from tunebox.bursts import Bursts
from tunebox.channel import Channel
from tunebox.config import AdminConfig, ChannelConfig, PlayerConfig, StreamConfig
from tunebox.covers import CoverService
from tunebox.logos import Logos
from tunebox.logs import configure
from tunebox.pin import Attempt, Pin
from tunebox.runtime import EMPTY, Snapshot
from tunebox.server import CSP, Handler, TuneboxServer, etag_matches
from tunebox.settings import Settings, SettingsUnavailable
from tunebox.store import Store
from tunebox.tracks import RawEntry


class FakeBursts(Bursts):
    def __init__(self) -> None:
        super().__init__(lambda _url: iter(()))
        self.requested: list[StreamConfig] = []

    def request(self, stream: StreamConfig) -> None:
        self.requested.append(stream)

    def burst_s(self, url: str) -> float:
        return 30.0


def serve(httpd: TuneboxServer) -> None:
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()


def make_pin(tmp_path: Path) -> Pin:
    """A PIN in mode off on its own settings database (closed by conftest)."""
    return Pin(Settings(tmp_path / "pin.db"), lambda: b"k" * 32)


def make_server(
    tmp_path: Path, pin: Pin | None = None, extra: tuple[ChannelConfig, ...] = ()
) -> TuneboxServer:
    pin = pin or make_pin(tmp_path)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (web / "pin.html").write_text("<!doctype html><title>PIN</title>", encoding="utf-8")
    (web / "status.html").write_text("<!doctype html><title>status</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (web / "app.js").write_text("export {};", encoding="utf-8")
    (web / "vendor").mkdir()
    (web / "vendor" / "lib-1.0.js").write_text("export {};", encoding="utf-8")
    (web / "vendor" / "lib-1.0.mjs").write_text("export {};", encoding="utf-8")
    (web / "font.woff2").write_bytes(b"wOF2")
    (web / "notes.md").write_text("# x", encoding="utf-8")
    source = FakeSource()
    source.responses = [[RawEntry("FISHER", "Stay", 1_000_000)]]
    store = Store(None)
    covers = CoverService(store, lambda _track: None)
    channel = Channel(
        channel_config(), source, covers, store, clock_ms=lambda: 2_000_000, rng=NoJitter()
    )
    channel.poll_once()
    logo_dir = tmp_path / "logos"
    logo_dir.mkdir()
    (logo_dir / "owr.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (logo_dir / "x.svg").write_bytes(b"<svg></svg>")
    (logo_dir / "small").mkdir()
    (logo_dir / "small" / "owr.png").write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    logos = Logos(logo_dir, lambda _url: b"", Blobs(tmp_path / "blobs"))
    channels: dict[str, Channel] = {"owr": channel}
    for config in extra:
        channels[config.id] = Channel(
            config, FakeSource(), covers, store, clock_ms=lambda: 2_000_000, rng=NoJitter()
        )
        (logo_dir / f"{config.id}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    snapshot = Snapshot(1, channels, PlayerConfig(52.0, 5.0, extra[0].id if extra else None))
    httpd = TuneboxServer(
        ("127.0.0.1", 0), lambda: snapshot, web, pin, logos=logos, bursts=FakeBursts()
    )
    serve(httpd)
    return httpd


@pytest.fixture
def server(tmp_path: Path) -> Generator[TuneboxServer]:
    httpd = make_server(tmp_path)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(
    httpd: TuneboxServer,
    path: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, data


def raw_post(
    httpd: TuneboxServer, path: str, headers: list[tuple[str, str]], body: bytes
) -> tuple[int, bytes]:
    """POSTs with the header lines exactly as given, so a repeated one reaches the server."""
    lines = [
        f"POST {path} HTTP/1.1",
        "Host: x",
        "Connection: close",
        f"Content-Length: {len(body)}",
    ]
    lines += [f"{name}: {value}" for name, value in headers]
    sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    sock.sendall("\r\n".join(lines).encode() + b"\r\n\r\n" + body)
    answer = b""
    while chunk := sock.recv(4096):
        answer += chunk
    sock.close()
    return int(answer.split(b" ", 2)[1]), answer


def test_channels_list(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/api/channels")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-cache"
    assert headers["x-tunebox-version"] == "1"
    assert json.loads(body)[0]["id"] == "owr"


def test_now_and_304(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/api/channels/owr/now")
    assert status == 200
    assert json.loads(body)["current"]["title"] == "Stay"
    etag = headers["etag"]
    status, _, body = request(server, "/api/channels/owr/now", headers={"If-None-Match": etag})
    assert status == 304
    assert body == b""


def test_weakened_etag_from_a_compressing_proxy_still_matches(server: TuneboxServer) -> None:
    _, headers, _ = request(server, "/api/channels/owr/now")
    weak = {"If-None-Match": "W/" + headers["etag"]}
    status, _, _ = request(server, "/api/channels/owr/now", headers=weak)
    assert status == 304


def test_pawtunes_and_health(server: TuneboxServer) -> None:
    status, _, body = request(server, "/api/pawtunes/owr")
    assert status == 200
    assert json.loads(body)["title"] == "Stay"
    status, _, body = request(server, "/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


@pytest.mark.parametrize(
    "path",
    [
        "/api/channels/nope/now",
        "/api/pawtunes/nope",
        "/api/other",
        "/missing.js",
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%00",
        "/api/channels/owr",
    ],
)
def test_not_found(server: TuneboxServer, path: str) -> None:
    status, headers, _ = request(server, path)
    assert status == 404
    assert headers["content-type"].startswith("application/json")


def test_static_index_and_head(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["cache-control"] == "no-cache"
    assert headers["content-security-policy"] == CSP
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert body.startswith(b"<!doctype html>")
    status, headers, body = request(server, "/", method="HEAD")
    assert status == 200
    assert int(headers["content-length"]) > 0
    assert body == b""


@pytest.mark.parametrize(
    ("path", "content_type", "cache"),
    [
        ("/app.js", "text/javascript; charset=utf-8", "no-cache"),
        ("/vendor/lib-1.0.js", "text/javascript; charset=utf-8", "max-age=31536000, immutable"),
        ("/font.woff2", "font/woff2", "no-cache"),
        # Not a vendor file once the path is resolved.
        ("/vendor/../app.js", "text/javascript; charset=utf-8", "no-cache"),
    ],
)
def test_static_types_and_caching(
    server: TuneboxServer, path: str, content_type: str, cache: str
) -> None:
    status, headers, _ = request(server, path)
    assert status == 200
    assert headers["content-type"] == content_type
    assert headers["cache-control"] == cache
    assert "content-security-policy" not in headers


def test_static_files_answer_304_to_their_etag(server: TuneboxServer) -> None:
    _, headers, _ = request(server, "/app.js")
    status, after, body = request(server, "/app.js", headers={"If-None-Match": headers["etag"]})
    assert status == 304
    assert body == b""
    assert after["x-content-type-options"] == "nosniff"


def test_files_of_unknown_type_are_not_served(server: TuneboxServer) -> None:
    status, _, _ = request(server, "/notes.md")
    assert status == 404


def test_player_settings(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/api/player")
    assert status == 200
    assert headers["cache-control"] == "max-age=300"
    assert json.loads(body) == {"latitude": 52.0, "longitude": 5.0, "default_channel": "owr"}


def test_json_answers_carry_nosniff(server: TuneboxServer) -> None:
    _, headers, _ = request(server, "/api/channels")
    assert headers["x-content-type-options"] == "nosniff"


def test_etag_matches() -> None:
    assert etag_matches('"a"', '"a"')
    assert etag_matches('"b", W/"a"', '"a"')
    assert etag_matches("*", '"a"')
    assert not etag_matches(None, '"a"')
    assert not etag_matches('"b"', '"a"')


def test_now_accepts_the_player_buffer(server: TuneboxServer) -> None:
    status, _, body = request(server, "/api/channels/owr/now?buffer_s=29.5")
    assert status == 200
    assert json.loads(body)["current"]["title"] == "Stay"


def test_now_adds_the_burst_of_the_stream_played(
    server: TuneboxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = server.current().channels["owr"]
    seen: list[float] = []
    original = channel.now_view

    def spy(
        buffer_s: float = 0, version: int = 0, stream: str | None = None, pin: str = "off.0"
    ) -> tuple[dict[str, object], str]:
        seen.append(buffer_s)
        return original(buffer_s, version, stream, pin)

    monkeypatch.setattr(channel, "now_view", spy)
    status, _, _ = request(server, "/api/channels/owr/now?buffer_s=2.5&stream=0")
    assert status == 200
    assert seen == [32.5]
    bursts = server.bursts
    assert isinstance(bursts, FakeBursts)
    assert bursts.requested == [channel.config.streams[0]]
    request(server, "/api/channels/owr/now?buffer_s=2.5")
    assert seen[-1] == 2.5


@pytest.mark.parametrize("value", ["x", "-1", "1", "0.5", ""])
def test_now_rejects_a_bad_stream(server: TuneboxServer, value: str) -> None:
    status, _, body = request(server, f"/api/channels/owr/now?stream={value}")
    assert status == 400
    assert json.loads(body) == {"error": "bad_stream"}


@pytest.mark.parametrize("value", ["abc", "-1", "121", "nan", "inf"])
def test_now_rejects_a_bad_buffer(server: TuneboxServer, value: str) -> None:
    status, _, body = request(server, f"/api/channels/owr/now?buffer_s={value}")
    assert status == 400
    assert json.loads(body) == {"error": "bad_buffer_s"}


def test_a_request_body_is_never_read_as_a_second_request(server: TuneboxServer) -> None:
    smuggled = b"GET /api/pawtunes/owr HTTP/1.1\r\nHost: x\r\n\r\n"
    head = f"GET /api/channels HTTP/1.1\r\nHost: x\r\nContent-Length: {len(smuggled)}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as conn:
        conn.sendall(head.encode() + smuggled)
        received = b""
        while chunk := conn.recv(65536):
            received += chunk
    assert received.count(b"HTTP/1.1 ") == 1
    assert b"Connection: close" in received


def test_asking_what_is_playing_marks_the_channel_active(server: TuneboxServer) -> None:
    channel = server.current().channels["owr"]
    assert not channel.active()
    request(server, "/api/channels/owr/now")
    assert channel.active()


def test_the_pawtunes_endpoint_marks_the_channel_active(server: TuneboxServer) -> None:
    channel = server.current().channels["owr"]
    request(server, "/api/pawtunes/owr")
    assert channel.active()


def test_logos_are_served_with_their_type(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/logos/owr.png?v=1")
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert body.startswith(b"\x89PNG")
    # The address carries the file's version, so a browser never asks twice.
    assert headers["cache-control"] == "max-age=31536000, immutable"
    status, headers, _ = request(server, "/logos/x.svg")
    assert headers["content-type"] == "image/svg+xml"
    # A logo cannot run script even when opened directly.
    assert headers["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("path", ["/logos/missing.png", "/logos/..%2Fweb%2Findex.html", "/logos/"])
def test_unknown_logos_are_not_found(server: TuneboxServer, path: str) -> None:
    status, _, _ = request(server, path)
    assert status == 404


def test_channel_list_gives_the_logo_path(server: TuneboxServer) -> None:
    _, _, body = request(server, "/api/channels")
    listed = json.loads(body)[0]
    assert listed["logo"].startswith("/logos/owr.png?v=")
    assert listed["logo_small"].startswith("/logos/small/owr.png?v=")


def test_small_logos_are_served(server: TuneboxServer) -> None:
    status, headers, body = request(server, "/logos/small/owr.png?v=1")
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert body.endswith(b"small")
    assert headers["cache-control"] == "max-age=31536000, immutable"
    assert request(server, "/logos/small/x.svg")[0] == 404
    assert request(server, "/logos/small/")[0] == 404


def test_csp_allows_the_streams_the_config_accepts() -> None:
    # stream_url may be http (kept for older configs); on a plain-http page
    # such a stream must not be refused by the policy itself.
    media = next(part for part in CSP.split("; ") if part.startswith("media-src"))
    assert media.split()[1:] == ["https:", "http:"]


def test_a_new_version_changes_the_now_etag(server: TuneboxServer) -> None:
    _, headers, body = request(server, "/api/channels/owr/now")
    assert json.loads(body)["version"] == 1
    old = server.current()
    newer = Snapshot(2, old.channels, old.player)
    server.current = lambda: newer
    status, _, body = request(
        server, "/api/channels/owr/now", headers={"If-None-Match": headers["etag"]}
    )
    assert status == 200
    assert json.loads(body)["version"] == 2


def test_stream_by_name_and_by_position(server: TuneboxServer) -> None:
    status, _, body = request(server, "/api/channels/owr/now?stream=Default")
    assert status == 200
    assert json.loads(body)["stream"] == "Default"
    status, _, body = request(server, "/api/channels/owr/now?stream=0")
    assert status == 200
    assert json.loads(body)["stream"] == "Default"
    status, _, _ = request(server, "/api/channels/owr/now?stream=Nope")
    assert status == 400


def test_stream_name_wins_over_position(server: TuneboxServer) -> None:
    channel = server.current().channels["owr"]
    two = (
        StreamConfig("1", "https://example.invalid/one.aac", None, None),
        StreamConfig("Other", "https://example.invalid/other.aac", None, None),
    )
    channel.replace_config(dataclasses.replace(channel.config, streams=two))
    _, _, body = request(server, "/api/channels/owr/now?stream=1")
    assert json.loads(body)["stream"] == "1"


def test_player_settings_without_channels(server: TuneboxServer) -> None:
    empty = Snapshot(1, {}, PlayerConfig())
    server.current = lambda: empty
    status, _, body = request(server, "/api/player")
    assert status == 200
    assert json.loads(body)["default_channel"] is None
    status, _, body = request(server, "/api/channels")
    assert status == 200
    assert json.loads(body) == []


ORIGIN = "https://radio.example"


@pytest.fixture
def admin_parts(tmp_path: Path) -> Generator[tuple[TuneboxServer, Accounts]]:
    settings = Settings(tmp_path / "t.db")
    web_admin = tmp_path / "web_admin"
    web_admin.mkdir()
    (web_admin / "index.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
    accounts = Accounts(settings)
    pin = Pin(settings, accounts.server_key)
    admin = Admin(
        accounts,
        AdminConfig(True, False, ORIGIN),
        web_admin,
        settings=settings,
        pin=pin,
        blobs=Blobs(tmp_path / "blobs"),
        free_space=lambda: None,
        apply=lambda: 0,
        samples=list,
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    httpd = TuneboxServer(
        ("127.0.0.1", 0),
        lambda: EMPTY,
        web,
        pin,
        admin=admin,
        trusted=(ipaddress.ip_network("127.0.0.0/8"),),
    )
    serve(httpd)
    yield httpd, accounts
    httpd.shutdown()
    httpd.server_close()
    settings.close()


@pytest.fixture
def admin_server(admin_parts: tuple[TuneboxServer, Accounts]) -> TuneboxServer:
    return admin_parts[0]


def post(
    httpd: TuneboxServer, path: str, body: bytes, headers: dict[str, str]
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


PLAIN = "http://tunebox.home.arpa:8083"


@pytest.fixture
def plain_server(tmp_path: Path) -> Generator[TuneboxServer]:
    """The panel on plain http, behind no trusted proxy: the test client
    connects from loopback, as a proxy on the same host would."""
    settings = Settings(tmp_path / "t.db")
    web_admin = tmp_path / "web_admin"
    web_admin.mkdir()
    (web_admin / "index.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
    accounts = Accounts(settings)
    pin = Pin(settings, accounts.server_key)
    admin = Admin(
        accounts,
        AdminConfig(True, False, PLAIN),
        web_admin,
        settings=settings,
        pin=pin,
        blobs=Blobs(tmp_path / "blobs"),
        free_space=lambda: None,
        apply=lambda: 0,
        samples=list,
    )
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, pin, admin=admin)
    serve(httpd)
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    settings.close()


def test_plain_http_serves_a_direct_client_on_the_network(plain_server: TuneboxServer) -> None:
    assert request(plain_server, "/admin/")[0] == 200
    assert request(plain_server, "/admin/api/state")[0] == 200


@pytest.mark.parametrize(
    "relay",
    [
        {"X-Forwarded-For": "198.51.100.7"},
        {"X-Forwarded-For": "192.168.1.20"},
        {"Forwarded": "for=198.51.100.7"},
        {"X-Real-IP": "198.51.100.7"},
        {"CF-Connecting-IP": "198.51.100.7"},
    ],
)
def test_plain_http_hides_the_panel_from_an_untrusted_proxy(
    plain_server: TuneboxServer,
    server: TuneboxServer,
    relay: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    off = request(server, "/admin/api/state")
    with caplog.at_level(logging.WARNING):
        for path in ("/admin", "/admin/", "/admin/api/state"):
            status, _, body = request(plain_server, path, headers=relay)
            assert (status, body) == (404, off[2])
        login = post(
            plain_server,
            "/admin/api/login",
            b"{}",
            {"Content-Type": "application/json", "Origin": PLAIN, **relay},
        )
    assert login[0] == 404
    refused = next(r for r in caplog.records if r.getMessage() == "admin_not_nearby")
    assert refused.__dict__["fields"]["peer"] == "127.0.0.1"


def test_plain_http_believes_a_trusted_proxy_about_the_client(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    accounts = Accounts(settings)
    pin = Pin(settings, accounts.server_key)
    admin = Admin(
        accounts,
        AdminConfig(True, False, PLAIN),
        tmp_path,
        settings=settings,
        pin=pin,
        blobs=Blobs(tmp_path / "blobs"),
        free_space=lambda: None,
        apply=lambda: 0,
        samples=list,
    )
    httpd = TuneboxServer(
        ("127.0.0.1", 0),
        lambda: EMPTY,
        tmp_path,
        pin,
        admin=admin,
        trusted=(ipaddress.ip_network("127.0.0.0/8"),),
    )
    serve(httpd)
    near = request(httpd, "/admin/api/state", headers={"X-Forwarded-For": "192.168.1.20"})
    far = request(httpd, "/admin/api/state", headers={"X-Forwarded-For": "198.51.100.7"})
    unread = request(httpd, "/admin/api/state", headers={"X-Forwarded-For": "203.0.113.9:51234"})
    unnamed = request(httpd, "/admin/api/state")
    httpd.shutdown()
    httpd.server_close()
    settings.close()
    assert (near[0], far[0], unread[0], unnamed[0]) == (200, 404, 404, 404)


def test_a_refused_address_is_logged_once_at_warning(
    plain_server: TuneboxServer, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        for address in ("198.51.100.7", "198.51.100.7", "203.0.113.5"):
            request(plain_server, "/admin/", headers={"Forwarded": f"for={address}"})
    levels = [r.levelno for r in caplog.records if r.getMessage() == "admin_not_nearby"]
    # Untrusted, so every request is the proxy's: one address, one warning.
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]


def test_https_serves_clients_anywhere(admin_server: TuneboxServer) -> None:
    relayed = {"X-Forwarded-For": "198.51.100.7"}
    assert request(admin_server, "/admin/api/state", headers=relayed)[0] == 200


def test_admin_is_404_when_disabled(server: TuneboxServer) -> None:
    for path in ("/admin", "/admin/", "/admin/api/state"):
        assert request(server, path)[0] == 404
    assert post(server, "/admin/api/login", b"{}", {"Content-Type": "application/json"})[0] == 404


def test_admin_requests_reach_the_admin_router(admin_server: TuneboxServer) -> None:
    status, headers, body = request(admin_server, "/admin/api/state")
    assert status == 200
    assert json.loads(body)["account"] is False
    assert headers["cache-control"] == "no-store"
    status, headers, _ = request(admin_server, "/admin/")
    assert status == 200
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    status, _, body = request(admin_server, "/admin/", method="HEAD")
    assert status == 200
    assert body == b""


def test_the_login_log_names_the_client_behind_a_trusted_proxy(
    admin_server: TuneboxServer, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "X-Forwarded-For": "198.51.100.7",
    }
    with caplog.at_level(logging.WARNING):
        status, _ = post(
            admin_server,
            "/admin/api/login",
            json.dumps({"password": "x" * 12, "code": "1"}).encode(),
            headers,
        )
    assert status == 401
    failed = next(r for r in caplog.records if r.getMessage() == "admin_login_failed")
    assert failed.__dict__["fields"]["client"] == "198.51.100.7"


def test_a_body_too_large_or_without_length_is_refused(admin_server: TuneboxServer) -> None:
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    assert post(admin_server, "/admin/api/login", b"x" * (MAX_BODY + 1), headers)[0] == 413
    # Only a live session may send a save's size; without one the answer is
    # the refusal the save would have had. Refused on the announced length,
    # before any of the body is read; sending it all would race the
    # server's hang-up.
    assert announce(admin_server, "/admin/api/config", MAX_BODY + 1, headers)[0] == 401
    assert announce(admin_server, "/admin/api/config", MAX_STATE_BODY + 1, {})[0] == 403
    connection = http.client.HTTPConnection("127.0.0.1", admin_server.server_address[1], timeout=5)
    connection.putrequest("POST", "/admin/api/login")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    connection.send(b"0\r\n\r\n")
    assert connection.getresponse().status == 411
    connection.close()


def test_a_crash_on_an_admin_request_withholds_the_exception_text(
    admin_server: TuneboxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    configure("info", out)

    def crash(_self: Admin, _request: Request) -> Response:
        raise RuntimeError("marker-admin-secret")

    monkeypatch.setattr(Admin, "handle", crash)
    assert request(admin_server, "/admin/api/state")[0] == 500
    assert "request_crashed" in out.getvalue()
    assert "RuntimeError (details withheld)" in out.getvalue()
    assert "marker-admin-secret" not in out.getvalue()


def test_a_crash_on_a_pin_request_withholds_the_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    configure("info", out)

    def crash(_self: Pin, _given: str, _client: str, secure: bool = False) -> Attempt:
        raise RuntimeError("marker-pin-secret")

    monkeypatch.setattr(Pin, "attempt", crash)
    httpd = make_server(tmp_path)
    headers = {"Content-Type": "application/json"}
    status, _, _ = request(httpd, "/api/pin", "POST", headers, b'{"pin": "123456"}')
    httpd.shutdown()
    httpd.server_close()
    assert status == 500
    assert "RuntimeError (details withheld)" in out.getvalue()
    assert "marker-pin-secret" not in out.getvalue()


def log_in(httpd: TuneboxServer, accounts: Accounts) -> tuple[str, str]:
    """Creates the account and logs in; the Cookie header value and the CSRF token."""
    accounts.create(PASSWORD, SECRET, ["aaaa-bbbb-cccc"], step=0)
    code = crypto.totp(SECRET, int(time.time() // 30))
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    body = json.dumps({"password": PASSWORD, "code": code}).encode()
    connection.request(
        "POST", "/admin/api/login", body, {"Origin": ORIGIN, "Content-Type": "application/json"}
    )
    response = connection.getresponse()
    csrf = json.loads(response.read())["csrf"]
    cookies = cast(list[str], response.headers.get_all("Set-Cookie"))
    cookie = next(c for c in cookies if c.startswith("__Secure-tunebox_admin=")).split(";")[0]
    connection.close()
    return cookie, csrf


def test_a_refused_large_body_is_not_read(admin_parts: tuple[TuneboxServer, Accounts]) -> None:
    big = b"{" + b" " * (MAX_BODY + 10) + b"}"
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    status, _ = post(admin_parts[0], "/admin/api/config", big, headers)
    assert status == 401


@pytest.mark.parametrize(
    "path", ["/admin/api/config", "/admin/api/import", "/admin/api/import/preview"]
)
def test_a_large_body_from_a_session_is_read(
    admin_parts: tuple[TuneboxServer, Accounts], path: str
) -> None:
    httpd, accounts = admin_parts
    cookie, csrf = log_in(httpd, accounts)
    big = json.dumps({"pad": " " * MAX_BODY}).encode()
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-Tunebox-CSRF": csrf,
    }
    status, _ = post(httpd, path, big, headers)
    # Read and judged, not refused for its size; import asks for a code first.
    assert status == (403 if path.endswith("/import") else 400)


def test_a_large_import_without_a_fresh_code_is_refused_before_its_body(
    admin_parts: tuple[TuneboxServer, Accounts],
) -> None:
    httpd, accounts = admin_parts
    cookie, csrf = log_in(httpd, accounts)
    big = json.dumps({"pad": " " * MAX_BODY}).encode()
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-Tunebox-CSRF": csrf,
    }
    status, body = post(httpd, "/admin/api/import", big, headers)
    assert (status, json.loads(body)) == (403, {"error": "reauth_required"})


def test_post_outside_admin_is_not_allowed(server: TuneboxServer) -> None:
    status, _ = post(server, "/api/channels", b"{}", {"Content-Type": "application/json"})
    assert status == 405


def test_post_outside_admin_is_refused_before_its_body(server: TuneboxServer) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.putrequest("POST", "/")
    connection.putheader("Content-Length", "10000000")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 405
    assert response.getheader("Connection") == "close"
    connection.close()


def test_every_logo_kind_is_sandboxed(server: TuneboxServer) -> None:
    _, headers, _ = request(server, "/logos/owr.png")
    assert headers["content-security-policy"] == "sandbox"


def test_forwarding_from_an_untrusted_peer_is_warned_once(
    server: TuneboxServer, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            request(server, "/api/channels", headers={"X-Forwarded-For": "203.0.113.9"})
    warned = [r for r in caplog.records if r.getMessage() == "untrusted_forwarding"]
    assert len(warned) == 1


def test_es_module_files_are_served_as_javascript(server: TuneboxServer) -> None:
    status, headers, _ = request(server, "/vendor/lib-1.0.mjs")
    assert status == 200
    assert headers["content-type"] == "text/javascript; charset=utf-8"


def test_every_forwarded_for_line_is_read(
    admin_server: TuneboxServer, caplog: pytest.LogCaptureFixture
) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", admin_server.server_address[1], timeout=5)
    body = json.dumps({"password": "x" * 12, "code": "1"}).encode()
    connection.putrequest("POST", "/admin/api/login")
    for name, value in (
        ("Content-Type", "application/json"),
        ("Origin", ORIGIN),
        ("Content-Length", str(len(body))),
        ("X-Forwarded-For", "203.0.113.250"),  # the client's own line
        ("X-Forwarded-For", "198.51.100.7"),  # the proxy's line
    ):
        connection.putheader(name, value)
    connection.endheaders(body)
    with caplog.at_level(logging.WARNING):
        assert connection.getresponse().status == 401
    connection.close()
    failed = next(r for r in caplog.records if r.getMessage() == "admin_login_failed")
    assert failed.__dict__["fields"]["client"] == "198.51.100.7"


class Recorder(Admin):
    def __init__(self, settings: Settings, web: Path, pin: Pin) -> None:
        accounts = Accounts(settings)
        super().__init__(
            accounts,
            AdminConfig(True, False, ORIGIN),
            web,
            settings=settings,
            pin=pin,
            blobs=Blobs(web.parent / "blobs"),
            free_space=lambda: None,
            apply=lambda: 0,
            samples=list,
        )
        self.seen: list[Request] = []

    def handle(self, request: Request) -> Response:
        self.seen.append(request)
        return Response(204)


def test_the_admin_gets_the_path_and_the_query_apart(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    pin = Pin(settings, Accounts(settings).server_key)
    admin = Recorder(settings, tmp_path, pin)
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, pin, admin=admin)
    serve(httpd)
    request(httpd, "/admin/api/history?before=12")
    httpd.shutdown()
    httpd.server_close()
    settings.close()
    assert [(r.path, r.query) for r in admin.seen] == [("/admin/api/history", "before=12")]


SECRET_STATION = dataclasses.replace(channel_config(), id="secret", name="Secret", needs_pin=True)
LOCKED = [
    "/api/channels",
    "/api/player",
    "/api/channels/owr/now",
    "/logos/owr.png",
    "/logos/small/owr.png",
]


class PinWorld:
    def __init__(self, tmp_path: Path, mode: str) -> None:
        self.settings = Settings(tmp_path / "pin.db")
        self.pin = Pin(self.settings, lambda: b"k" * 32)
        self.pin.change(mode, "123456")
        self.httpd = make_server(tmp_path, pin=self.pin, extra=(SECRET_STATION,))

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.settings.close()


@pytest.fixture
def site(tmp_path: Path) -> Generator[PinWorld]:
    made = PinWorld(tmp_path, "site")
    yield made
    made.close()


@pytest.fixture
def stations(tmp_path: Path) -> Generator[PinWorld]:
    made = PinWorld(tmp_path, "stations")
    yield made
    made.close()


def give_pin(httpd: TuneboxServer, pin: str = "123456") -> tuple[int, dict[str, str], bytes]:
    body = json.dumps({"pin": pin}).encode()
    return request(httpd, "/api/pin", "POST", {"Content-Type": "application/json"}, body)


def grant_of(httpd: TuneboxServer) -> str:
    status, headers, _ = give_pin(httpd)
    assert status == 204
    return headers["set-cookie"].split(";")[0]


def test_a_locked_site_shows_the_pin_page_for_every_page(site: PinWorld) -> None:
    for path in ["/", "/index.html", "/status.html"]:
        status, headers, body = request(site.httpd, path)
        assert status == 200, path
        assert b"<title>PIN</title>" in body, path
        assert headers["cache-control"] == "no-store", path
        assert "content-security-policy" in headers, path


def test_a_locked_site_refuses_the_api_and_the_logos(site: PinWorld) -> None:
    for path in LOCKED:
        status, headers, body = request(site.httpd, path)
        assert (status, json.loads(body)) == (401, {"error": "pin_required"}), path
        assert headers["cache-control"] == "no-store", path


def test_a_locked_site_keeps_the_code_and_pawtunes_open(site: PinWorld) -> None:
    for path in ["/app.js", "/pin.html", "/api/pawtunes/owr", "/healthz"]:
        assert request(site.httpd, path)[0] == 200, path


def test_the_right_pin_opens_the_site_privately(site: PinWorld) -> None:
    status, headers, _ = give_pin(site.httpd)
    assert status == 204
    assert headers["cache-control"] == "no-store"
    cookie = headers["set-cookie"].split(";")[0]
    assert cookie.startswith("tunebox_grant=")
    status, headers, body = request(site.httpd, "/", headers={"Cookie": cookie})
    assert status == 200 and b"<title>t</title>" in body
    for path in ["/", *LOCKED]:
        status, headers, _ = request(site.httpd, path, headers={"Cookie": cookie})
        assert status == 200, path
        assert headers["cache-control"].startswith("private"), path
        assert headers["vary"] == "Cookie", path


def test_a_wrong_pin_is_refused_and_logged(
    site: PinWorld, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        status, headers, body = give_pin(site.httpd, "654321")
    assert (status, json.loads(body)) == (401, {"error": "pin_wrong"})
    assert headers["cache-control"] == "no-store"
    failed = [r for r in caplog.records if r.getMessage() == "listener_pin_failed"]
    assert failed[0].__dict__["fields"] == {"client": "127.0.0.1", "reason": "wrong"}
    assert "654321" not in caplog.text


def test_ten_wrong_pins_lock_the_address(site: PinWorld) -> None:
    for _ in range(10):
        assert give_pin(site.httpd, "000000")[0] == 401
    status, _, body = give_pin(site.httpd)
    assert (status, json.loads(body)) == (429, {"error": "slow_down"})


def test_an_old_grant_is_refused_everywhere(site: PinWorld) -> None:
    cookie = grant_of(site.httpd)
    site.pin.change("site", "7654321")
    for path in LOCKED:
        assert request(site.httpd, path, headers={"Cookie": cookie})[0] == 401, path
    _, _, body = request(site.httpd, "/", headers={"Cookie": cookie})
    assert b"<title>PIN</title>" in body


@pytest.mark.parametrize(
    ("method", "body", "status"),
    [
        ("GET", None, 405),
        ("POST", b"{", 400),
        ("POST", b'{"pin": 123456}', 400),
        ("POST", b"x" * 300, 413),
    ],
)
def test_pin_requests_that_are_not_a_pin(
    site: PinWorld, method: str, body: bytes | None, status: int
) -> None:
    headers = {"Content-Type": "application/json"}
    assert request(site.httpd, "/api/pin", method, headers, body)[0] == status


def test_hidden_stations_are_left_out_until_the_pin(stations: PinWorld) -> None:
    status, headers, body = request(stations.httpd, "/api/channels")
    assert status == 200
    assert [c["id"] for c in json.loads(body)] == ["owr"]
    assert headers["x-tunebox-pin"] == "more"
    assert headers["cache-control"].startswith("private")
    for path in ["/api/channels/secret/now", "/logos/secret.png"]:
        assert request(stations.httpd, path)[0] == 404, path
    assert json.loads(request(stations.httpd, "/api/player")[2])["default_channel"] is None
    assert request(stations.httpd, "/")[0] == 200
    cookie = grant_of(stations.httpd)
    status, headers, body = request(stations.httpd, "/api/channels", headers={"Cookie": cookie})
    assert [c["id"] for c in json.loads(body)] == ["owr", "secret"]
    assert "x-tunebox-pin" not in headers
    for path in ["/api/channels/secret/now", "/logos/secret.png"]:
        assert request(stations.httpd, path, headers={"Cookie": cookie})[0] == 200, path
    player = json.loads(request(stations.httpd, "/api/player", headers={"Cookie": cookie})[2])
    assert player["default_channel"] == "secret"


def test_with_the_pin_off_nothing_is_hidden_or_private(stations: PinWorld) -> None:
    stations.pin.change("off", None)
    status, headers, body = request(stations.httpd, "/api/channels")
    assert [c["id"] for c in json.loads(body)] == ["owr", "secret"]
    assert headers["cache-control"] == "no-cache"
    assert "vary" not in headers and "x-tunebox-pin" not in headers
    status, headers, _ = give_pin(stations.httpd)
    assert status == 204
    assert "set-cookie" not in headers


def test_the_grant_is_secure_only_when_the_proxy_says_https(site: PinWorld) -> None:
    site.httpd.trusted = (ipaddress.ip_network("127.0.0.0/8"),)
    body = json.dumps({"pin": "123456"}).encode()
    for proto, secure in [("https", True), ("http", False), (None, False)]:
        headers = {"Content-Type": "application/json"}
        if proto:
            headers["X-Forwarded-Proto"] = proto
        _, answer, _ = request(site.httpd, "/api/pin", "POST", headers, body)
        assert ("Secure" in answer["set-cookie"]) is secure, proto
    site.httpd.trusted = ()
    https = {"Content-Type": "application/json", "X-Forwarded-Proto": "https"}
    _, answer, _ = request(site.httpd, "/api/pin", "POST", https, body)
    assert "Secure" not in answer["set-cookie"]


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_healthz_detail_only_for_a_direct_local_caller(server: TuneboxServer, header: str) -> None:
    _, _, body = request(server, "/healthz")
    assert "channels" in json.loads(body)
    _, _, body = request(server, "/healthz", headers={header: "for=203.0.113.9"})
    assert json.loads(body) == {"status": "ok"}


def test_a_paced_pin_attempt_is_429_and_no_failure(
    site: PinWorld, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(pin: str, client: str, secure: bool = True) -> Attempt:
        return Attempt(False, reason="busy")

    monkeypatch.setattr(site.pin, "attempt", busy)
    with caplog.at_level(logging.INFO):
        status, headers, body = give_pin(site.httpd)
    assert (status, json.loads(body)) == (429, {"error": "busy"})
    assert headers["cache-control"] == "no-store"
    assert headers["retry-after"] == "2"
    assert not [r for r in caplog.records if r.getMessage() == "listener_pin_failed"]


@pytest.mark.parametrize(
    "path", ["/index%2ehtml", "/%69ndex.html", "/status%2Ehtml", "/vendor/../index.html", "//"]
)
def test_a_locked_site_judges_the_file_it_would_serve(site: PinWorld, path: str) -> None:
    status, headers, body = request(site.httpd, path)
    assert status == 200, path
    assert b"<title>PIN</title>" in body, path
    assert headers["cache-control"] == "no-store", path


def test_a_cross_site_post_counts_as_nothing(
    site: PinWorld, caplog: pytest.LogCaptureFixture
) -> None:
    form = {"Content-Type": "text/plain", "Origin": "https://evil.example"}
    with caplog.at_level(logging.INFO):
        for _ in range(10):
            status, _, _ = request(site.httpd, "/api/pin", "POST", form, b'{"pin": "000000"}')
            assert status == 403
        cross = {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}
        assert request(site.httpd, "/api/pin", "POST", cross, b'{"pin": "000000"}')[0] == 403
    assert not [r for r in caplog.records if r.getMessage() == "listener_pin_failed"]
    # The household's own address is not locked out by it.
    assert give_pin(site.httpd)[0] == 204


def test_the_player_own_pin_post_passes(site: PinWorld) -> None:
    same = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
    status, _, _ = request(site.httpd, "/api/pin", "POST", same, b'{"pin": "123456"}')
    assert status == 204


@pytest.mark.parametrize("path", ["/vendor/../admin/", "/%61dmin/", "/web_admin/index.html"])
def test_the_admin_pages_never_come_from_the_player_files(
    admin_server: TuneboxServer, path: str
) -> None:
    status, _, body = request(admin_server, path)
    assert b"<title>admin</title>" not in body
    assert status == 404


def test_length_errors_on_admin_paths_carry_the_admin_headers(
    admin_server: TuneboxServer,
) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", admin_server.server_address[1], timeout=5)
    connection.putrequest("POST", "/admin/api/login")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 411
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Content-Security-Policy")
    connection.close()


def test_a_crash_inside_an_admin_request_answers_500_with_the_admin_headers(
    admin_server: TuneboxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = admin_server.admin
    assert admin is not None

    def boom(_request: Request) -> Response:
        raise RuntimeError("broken")

    monkeypatch.setitem(admin.routes, ("GET", "state"), Route(open=boom))
    status, headers, _ = request(admin_server, "/admin/api/state")
    assert status == 500
    assert headers["cache-control"] == "no-store"


def test_a_stalled_body_closes_the_connection_without_a_crash(
    server: TuneboxServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(Handler, "timeout", 0.5)
    with caplog.at_level(logging.DEBUG):
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
            sock.sendall(
                b"POST /api/pin HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                b'Content-Length: 50\r\n\r\n{"pin"'
            )
            assert sock.recv(4096) == b""  # closed with no answer, 500 or otherwise
        assert any(r.getMessage() == "request_body_incomplete" for r in caplog.records)
    assert not any(r.getMessage() == "request_crashed" for r in caplog.records)


def test_a_pin_change_changes_the_now_etag(tmp_path: Path) -> None:
    pin = make_pin(tmp_path)
    httpd = make_server(tmp_path, pin=pin)
    _, headers, body = request(httpd, "/api/channels/owr/now")
    assert json.loads(body)["pin"] == "off.0"
    pin.change("stations", "123456")
    status, _, body = request(
        httpd, "/api/channels/owr/now", headers={"If-None-Match": headers["etag"]}
    )
    assert status == 200 and json.loads(body)["pin"] == "stations.1"
    _, listed, _ = request(httpd, "/api/channels")
    assert listed["x-tunebox-pin-revision"] == "stations.1"
    httpd.shutdown()
    httpd.server_close()


def test_an_old_grant_after_a_new_pin_hides_the_stations_again(tmp_path: Path) -> None:
    pin = make_pin(tmp_path)
    hidden = dataclasses.replace(channel_config(), id="late", needs_pin=True)
    httpd = make_server(tmp_path, pin=pin, extra=(hidden,))
    pin.change("stations", "123456")
    grant = cast(str, pin.attempt("123456", "198.51.100.7").cookie).split(";")[0]
    assert request(httpd, "/api/channels/late/now", headers={"Cookie": grant})[0] == 200
    pin.change("stations", "654321")
    assert request(httpd, "/api/channels/late/now", headers={"Cookie": grant})[0] == 404
    assert request(httpd, "/logos/late.png", headers={"Cookie": grant})[0] == 404
    httpd.shutdown()
    httpd.server_close()


def test_a_304_while_a_pin_is_on_stays_private(tmp_path: Path) -> None:
    pin = make_pin(tmp_path)
    httpd = make_server(tmp_path, pin=pin)
    pin.change("stations", "123456")
    _, headers, _ = request(httpd, "/api/channels/owr/now")
    status, again, _ = request(
        httpd, "/api/channels/owr/now", headers={"If-None-Match": headers["etag"]}
    )
    assert status == 304
    assert again["vary"] == "Cookie"
    assert again["cache-control"].startswith("private")
    httpd.shutdown()
    httpd.server_close()


def test_a_busy_pin_answer_says_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pin = make_pin(tmp_path)
    httpd = make_server(tmp_path, pin=pin)

    def busy(pin: str, client: str, secure: bool = True) -> Attempt:
        return Attempt(False, reason="busy")

    monkeypatch.setattr(pin, "attempt", busy)
    status, headers, body = request(
        httpd, "/api/pin", "POST", {"Content-Type": "application/json"}, b'{"pin": "123456"}'
    )
    assert status == 429
    assert json.loads(body) == {"error": "busy"}
    assert headers["retry-after"] == "2"
    httpd.shutdown()
    httpd.server_close()


def test_a_repeated_cookie_line_counts_as_absent(tmp_path: Path) -> None:
    pin = make_pin(tmp_path)
    pin.change("site", "123456")
    httpd = make_server(tmp_path, pin=pin)
    grant = cast(str, pin.attempt("123456", "198.51.100.7").cookie).split(";")[0]
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    connection.putrequest("GET", "/api/channels")
    connection.putheader("Cookie", grant)
    connection.putheader("Cookie", "other=1")
    connection.endheaders()
    assert connection.getresponse().status == 401
    connection.close()
    assert request(httpd, "/api/channels", headers={"Cookie": grant})[0] == 200
    httpd.shutdown()
    httpd.server_close()


def announce(
    httpd: TuneboxServer, path: str, length: int, headers: dict[str, str]
) -> tuple[int, dict[str, str], dict[str, object]]:
    """A POST that names its length and sends no body: a refusal must come
    without the server reading one."""
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    connection.putrequest("POST", path)
    connection.putheader("Content-Length", str(length))
    for name, value in headers.items():
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    answer = json.loads(response.read() or b"null")
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, answer


OVERSIZE = MAX_BODY + 4096


def test_a_large_save_from_an_ended_session_asks_to_log_in(
    admin_parts: tuple[TuneboxServer, Accounts],
) -> None:
    httpd, accounts = admin_parts
    cookie, csrf = log_in(httpd, accounts)
    accounts.logout(cookie.partition("=")[2])
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-Tunebox-CSRF": csrf,
    }
    status, answered, body = announce(httpd, "/admin/api/config", OVERSIZE, headers)
    assert (status, body) == (401, {"error": "login_required"})
    assert answered["connection"] == "close"
    assert answered["cache-control"] == "no-store"


def test_a_large_save_with_a_stale_token_is_refused_and_logged_once(
    admin_parts: tuple[TuneboxServer, Accounts], caplog: pytest.LogCaptureFixture
) -> None:
    httpd, accounts = admin_parts
    cookie, _csrf = log_in(httpd, accounts)
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-Tunebox-CSRF": "stale",
    }
    with caplog.at_level(logging.WARNING):
        status, answered, body = announce(httpd, "/admin/api/config", OVERSIZE, headers)
    assert (status, body) == (403, {"error": "bad_csrf"})
    assert answered["connection"] == "close"
    rejected = [r for r in caplog.records if r.getMessage() == "admin_csrf_rejected"]
    assert len(rejected) == 1


def test_a_large_save_while_sessions_cannot_be_read_is_read_only(
    admin_parts: tuple[TuneboxServer, Accounts], monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, accounts = admin_parts
    cookie, csrf = log_in(httpd, accounts)

    def unreadable(_self: Accounts, _token: str) -> None:
        raise SettingsUnavailable("down")

    monkeypatch.setattr(Accounts, "session", unreadable)
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-Tunebox-CSRF": csrf,
    }
    status, answered, body = announce(httpd, "/admin/api/config", OVERSIZE, headers)
    assert (status, body) == (503, {"error": "read_only"})
    assert answered["connection"] == "close"


def test_a_large_post_from_off_the_network_is_hidden_before_any_session_read(
    plain_server: TuneboxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def session(_self: Accounts, token: str) -> None:
        asked.append(token)

    monkeypatch.setattr(Accounts, "session", session)
    headers = {
        "Origin": PLAIN,
        "Content-Type": "application/json",
        "Cookie": "tunebox_admin=abc",
        "X-Tunebox-CSRF": "x",
        "X-Forwarded-For": "198.51.100.7",
    }
    status, answered, body = announce(plain_server, "/admin/api/config", OVERSIZE, headers)
    assert (status, body) == (404, {"error": "not_found"})
    assert answered["connection"] == "close"
    assert asked == []


def exchange(httpd: TuneboxServer, raw: bytes) -> bytes:
    """Everything the server sends back on one connection until it hangs up."""
    with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as conn:
        conn.sendall(raw)
        received = b""
        while chunk := conn.recv(65536):
            received += chunk
    return received


@pytest.mark.parametrize("path", [b"/admin/api/config", b"/admin/api/login", b"/api/pin"])
# Superscript two: str.isdigit() takes it, int() does not. Past 4300 digits
# int() refuses ASCII digits too.
@pytest.mark.parametrize("length", [b"\xb2", b"1" * 4301], ids=["superscript", "4301-digits"])
def test_an_unusable_length_is_refused_not_crashed(
    admin_server: TuneboxServer, caplog: pytest.LogCaptureFixture, path: bytes, length: bytes
) -> None:
    raw = (
        b"POST " + path + b" HTTP/1.1\r\nHost: x\r\nOrigin: " + ORIGIN.encode() + b"\r\n"
        b"Content-Type: application/json\r\nContent-Length: " + length + b"\r\n\r\n{}"
    )
    with caplog.at_level(logging.DEBUG):
        received = exchange(admin_server, raw)
    assert received.startswith(b"HTTP/1.1 411 ")
    assert received.count(b"HTTP/1.1 ") == 1
    assert b"Connection: close" in received
    assert not any(r.getMessage() == "request_crashed" for r in caplog.records)


def test_a_crash_before_the_body_is_read_ends_the_connection(
    admin_server: TuneboxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_self: Admin, _request: Request) -> tuple[int, Response | None]:
        raise RuntimeError("broken")

    monkeypatch.setattr(Admin, "body_limit", crash)
    smuggled = b"GET /api/channels HTTP/1.1\r\nHost: x\r\n\r\n"
    raw = (
        b"POST /admin/api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
    )
    received = exchange(admin_server, raw)
    assert received.startswith(b"HTTP/1.1 500 ")
    assert received.count(b"HTTP/1.1 ") == 1
    assert b"Connection: close" in received


def test_a_dropped_connection_prints_no_traceback(
    server: TuneboxServer, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    sock.sendall(b"GET /api/channels HTTP/1.1\r\nHost: x\r\n\r\n")
    answer = sock.recv(65536)
    assert answer.startswith(b"HTTP/1.1 200")
    # Headers and body arrive as separate writes; a reset before the body
    # is out ends the request quietly mid-response instead.
    head, _, body = answer.partition(b"\r\n\r\n")
    length = next(
        int(line[15:]) for line in head.split(b"\r\n") if line.startswith(b"Content-Length: ")
    )
    while len(body) < length:
        body += sock.recv(65536)
    # Close with a reset while the server waits for the next request on
    # the kept-alive connection, as a browser does when it drops a socket.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
    for _ in range(50):
        if any(r.getMessage() == "connection_dropped" for r in caplog.records):
            break
        time.sleep(0.02)
    assert "Traceback" not in capfd.readouterr().err
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any(r.getMessage() == "connection_dropped" for r in caplog.records)


def test_two_content_length_lines_are_refused(server: TuneboxServer) -> None:
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    sock.sendall(
        b"POST /api/pin HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\nContent-Length: 40\r\n\r\n{}"
    )
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 411")
    assert b"Connection: close" in answer


def test_two_sec_fetch_site_lines_are_refused(server: TuneboxServer) -> None:
    status, _ = raw_post(
        server,
        "/api/pin",
        [
            ("Content-Type", "application/json"),
            ("Sec-Fetch-Site", "same-origin"),
            ("Sec-Fetch-Site", "cross-site"),
        ],
        b'{"pin": "123456"}',
    )
    assert status == 403


def test_a_trickled_body_is_cut_at_the_deadline(
    server: TuneboxServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("tunebox.server.BODY_DEADLINE_S", 0.5)
    caplog.set_level(logging.DEBUG)
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    sock.sendall(
        b"POST /api/pin HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: 40\r\n\r\n"
    )
    with contextlib.suppress(OSError):  # the server hangs up while bytes still trickle in
        for _ in range(8):
            sock.sendall(b" ")
            time.sleep(0.1)
    with contextlib.suppress(ConnectionResetError):  # unread bytes were left when it closed
        assert sock.recv(4096) == b""  # hung up, nothing answered
    sock.close()
    names = [r.getMessage() for r in caplog.records]
    assert "request_body_incomplete" in names
    assert "request_crashed" not in names
    assert "connection_crashed" not in names


def test_a_large_upload_needs_a_live_session(
    admin_parts: tuple[TuneboxServer, Accounts], tmp_path: Path
) -> None:
    httpd, _accounts = admin_parts
    headers = {"Origin": ORIGIN, "Content-Type": "image/png"}
    status, _ = post(httpd, "/admin/api/logos", b"\x89PNG\r\n\x1a\n" + b"\0" * 100_000, headers)
    assert status == 401
    assert not (tmp_path / "blobs").exists()


def test_an_upload_over_the_limit_is_refused_before_it_is_read(
    admin_parts: tuple[TuneboxServer, Accounts],
) -> None:
    httpd, _accounts = admin_parts
    raw = (
        b"POST /admin/api/logos HTTP/1.1\r\nHost: x\r\nOrigin: " + ORIGIN.encode() + b"\r\n"
        b"Content-Type: image/png\r\nContent-Length: 5000000\r\n\r\n"
    )
    received = exchange(httpd, raw)
    assert received.startswith(b"HTTP/1.1 401 ")


def test_a_pin_body_that_exhausts_the_parser_is_a_bad_request_not_a_crash(
    server: TuneboxServer, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_deep(_body: bytes) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("tunebox.server.json.loads", too_deep)
    with caplog.at_level(logging.DEBUG):
        status, _, _ = request(
            server, "/api/pin", "POST", {"Content-Type": "application/json"}, b'{"pin": "1"}'
        )
    assert status == 400
    assert not any(r.getMessage() == "request_crashed" for r in caplog.records)
