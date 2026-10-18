import http.client
import json
import socket
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import FakeSource, NoJitter, channel_config
from tunebox.bursts import Bursts
from tunebox.channel import Channel
from tunebox.config import PlayerConfig, StreamConfig
from tunebox.covers import CoverService
from tunebox.logos import Logos
from tunebox.server import CSP, TuneboxServer, etag_matches
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
    logos = Logos(logo_dir, lambda _url: b"")
    httpd = TuneboxServer(
        ("127.0.0.1", 0),
        {"owr": channel},
        web,
        logos=logos,
        player=PlayerConfig(52.0, 5.0, None),
        bursts=FakeBursts(),
    )
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
    assert headers["cache-control"] == "max-age=300"
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
    channel = server.channels["owr"]
    seen: list[float] = []
    original = channel.now_view

    def spy(buffer_s: float = 0) -> tuple[dict[str, object], str]:
        seen.append(buffer_s)
        return original(buffer_s)

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
    channel = server.channels["owr"]
    assert not channel.active()
    request(server, "/api/channels/owr/now")
    assert channel.active()


def test_the_pawtunes_endpoint_marks_the_channel_active(server: TuneboxServer) -> None:
    channel = server.channels["owr"]
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
    assert json.loads(body)[0]["logo"].startswith("/logos/owr.png?v=")


def test_csp_allows_the_streams_the_config_accepts() -> None:
    # stream_url may be http (kept for older configs); on a plain-http page
    # such a stream must not be refused by the policy itself.
    media = next(part for part in CSP.split("; ") if part.startswith("media-src"))
    assert media.split()[1:] == ["https:", "http:"]
