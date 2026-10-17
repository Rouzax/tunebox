import http.client
import json
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import FakeSource, NoJitter, channel_config
from tunebox.channel import Channel
from tunebox.covers import CoverService
from tunebox.server import TuneboxServer, etag_matches
from tunebox.store import Store
from tunebox.tracks import RawEntry


@pytest.fixture
def server(tmp_path: Path) -> Generator[TuneboxServer]:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
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
    httpd = TuneboxServer(("127.0.0.1", 0), {"owr": channel}, web)
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
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-cache"
    assert body.startswith(b"<!doctype html>")
    status, headers, body = request(server, "/", method="HEAD")
    assert status == 200
    assert int(headers["content-length"]) > 0
    assert body == b""


def test_etag_matches() -> None:
    assert etag_matches('"a"', '"a"')
    assert etag_matches('"b", W/"a"', '"a"')
    assert etag_matches("*", '"a"')
    assert not etag_matches(None, '"a"')
    assert not etag_matches('"b"', '"a"')
