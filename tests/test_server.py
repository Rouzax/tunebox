import dataclasses
import http.client
import ipaddress
import json
import logging
import socket
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import FakeSource, NoJitter, channel_config
from tunebox.accounts import Accounts
from tunebox.admin import MAX_BODY, MAX_STATE_BODY, Admin, Request, Response
from tunebox.bursts import Bursts
from tunebox.channel import Channel
from tunebox.config import AdminConfig, PlayerConfig, StreamConfig
from tunebox.covers import CoverService
from tunebox.logos import Logos
from tunebox.runtime import EMPTY, Snapshot
from tunebox.server import CSP, TuneboxServer, etag_matches
from tunebox.settings import Settings
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


@pytest.fixture
def server(tmp_path: Path) -> Generator[TuneboxServer]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
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
    channel = Channel(
        channel_config(),
        source,
        CoverService(store, lambda _track: None),
        store,
        clock_ms=lambda: 2_000_000,
        rng=NoJitter(),
    )
    channel.poll_once()
    logo_dir = tmp_path / "logos"
    logo_dir.mkdir()
    (logo_dir / "owr.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (logo_dir / "x.svg").write_bytes(b"<svg></svg>")
    (logo_dir / "small").mkdir()
    (logo_dir / "small" / "owr.png").write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    logos = Logos(logo_dir, lambda _url: b"")
    snapshot = Snapshot(1, {"owr": channel}, PlayerConfig(52.0, 5.0, None))
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: snapshot, web, logos=logos, bursts=FakeBursts())
    # A short poll interval makes shutdown() quick.
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(
    httpd: TuneboxServer, path: str, method: str = "GET", headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, body


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
        buffer_s: float = 0, version: int = 0, stream: str | None = None
    ) -> tuple[dict[str, object], str]:
        seen.append(buffer_s)
        return original(buffer_s, version, stream)

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


def serve(httpd: TuneboxServer) -> None:
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()


@pytest.fixture
def admin_server(tmp_path: Path) -> Generator[TuneboxServer]:
    settings = Settings(tmp_path / "t.db")
    web_admin = tmp_path / "web_admin"
    web_admin.mkdir()
    (web_admin / "index.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
    admin = Admin(
        Accounts(settings), AdminConfig(True, False, ORIGIN), web_admin, settings=settings
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    httpd = TuneboxServer(
        ("127.0.0.1", 0),
        lambda: EMPTY,
        web,
        admin=admin,
        trusted=(ipaddress.ip_network("127.0.0.0/8"),),
    )
    serve(httpd)
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    settings.close()


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
    admin = Admin(
        Accounts(settings),
        AdminConfig(True, False, PLAIN),
        web_admin,
        settings=settings,
    )
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, admin=admin)
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
    admin = Admin(
        Accounts(settings),
        AdminConfig(True, False, PLAIN),
        tmp_path,
        settings=settings,
    )
    httpd = TuneboxServer(
        ("127.0.0.1", 0),
        lambda: EMPTY,
        tmp_path,
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
    # Only a save carries the whole station list; it is still read, then
    # refused for want of a session.
    assert post(admin_server, "/admin/api/config", b"x" * 100_000, headers)[0] == 401
    # Refused on the announced length, before any of the body is read;
    # sending it all would race the server's hang-up.
    connection = http.client.HTTPConnection("127.0.0.1", admin_server.server_address[1], timeout=5)
    connection.putrequest("POST", "/admin/api/config")
    connection.putheader("Content-Length", str(MAX_STATE_BODY + 1))
    connection.endheaders()
    assert connection.getresponse().status == 413
    connection.close()
    connection = http.client.HTTPConnection("127.0.0.1", admin_server.server_address[1], timeout=5)
    connection.putrequest("POST", "/admin/api/login")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    connection.send(b"0\r\n\r\n")
    assert connection.getresponse().status == 411
    connection.close()


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


def test_forwarded_header_without_trusted_proxies_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(tmp_path / "t.db")
    admin = Admin(Accounts(settings), AdminConfig(True, False, ORIGIN), tmp_path, settings=settings)
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, admin=admin)
    serve(httpd)
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            request(httpd, "/admin/api/state", headers={"X-Forwarded-For": "198.51.100.7"})
    httpd.shutdown()
    httpd.server_close()
    settings.close()
    assert caplog.text.count("trusted_proxies_unset") == 1


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
    def __init__(self, settings: Settings, web: Path) -> None:
        super().__init__(
            Accounts(settings), AdminConfig(True, False, ORIGIN), web, settings=settings
        )
        self.seen: list[Request] = []

    def handle(self, request: Request) -> Response:
        self.seen.append(request)
        return Response(204)


def test_the_admin_gets_the_path_and_the_query_apart(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    admin = Recorder(settings, tmp_path)
    httpd = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, admin=admin)
    serve(httpd)
    request(httpd, "/admin/api/history?before=12")
    httpd.shutdown()
    httpd.server_close()
    settings.close()
    assert [(r.path, r.query) for r in admin.seen] == [("/admin/api/history", "before=12")]
