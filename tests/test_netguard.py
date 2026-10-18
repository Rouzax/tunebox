import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from tunebox import netguard
from tunebox.http import HttpError
from tunebox.netguard import Guard, public


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        port = cast("tuple[str, int]", self.server.server_address)[1]
        if self.path == "/redirect-ok":
            self._redirect(f"http://ok.test:{port}/body")
        elif self.path == "/redirect-inside":
            self._redirect("http://inside.test/body")
        elif self.path == "/redirect-ftp":
            self._redirect("ftp://ok.test/file")
        elif self.path == "/loop":
            self._redirect("/loop")
        elif self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/drip":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass  # the reader hung up, as it should
        else:
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"hello")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: V107
        pass


@pytest.fixture
def port() -> Generator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


NAMES = {"ok.test": "127.0.0.1", "inside.test": "10.0.0.1"}


def guard() -> Guard:
    # Loopback stands in for a public host; every other address is judged as in production.
    return Guard(
        resolver=lambda host, _port: [NAMES[host]],
        allow=lambda address: address == "127.0.0.1" or public(address),
        https_only=False,
    )


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("8.8.8.8", True),
        ("2001:4860:4860::8888", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
        ("224.0.0.1", False),
    ],
)
def test_only_public_addresses(address: str, allowed: bool) -> None:
    assert public(address) is allowed


def test_get_follows_a_redirect_to_an_allowed_host(port: int) -> None:
    assert guard().get(f"http://ok.test:{port}/redirect-ok") == b"hello"


def test_a_redirect_to_a_private_address_is_refused(port: int) -> None:
    with pytest.raises(HttpError, match="non-public"):
        guard().get(f"http://ok.test:{port}/redirect-inside")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://ok.test/x", "gopher://ok.test/"])
def test_other_schemes_are_refused(url: str) -> None:
    with pytest.raises(HttpError, match="refused"):
        guard().get(url)


def test_a_redirect_to_ftp_is_refused(port: int) -> None:
    with pytest.raises(HttpError, match="refused"):
        guard().get(f"http://ok.test:{port}/redirect-ftp")


def test_https_only_refuses_http(port: int) -> None:
    strict = Guard(resolver=lambda _host, _port: ["127.0.0.1"], allow=lambda _address: True)
    with pytest.raises(HttpError, match="refused"):
        strict.get(f"http://ok.test:{port}/")


def test_too_many_redirects(port: int) -> None:
    with pytest.raises(HttpError, match="redirects"):
        guard().get(f"http://ok.test:{port}/loop")


def test_an_error_status_is_an_error(port: int) -> None:
    with pytest.raises(HttpError, match="404"):
        guard().get(f"http://ok.test:{port}/missing")


def test_a_bad_port_is_refused() -> None:
    with pytest.raises(HttpError, match="refused"):
        guard().get("http://ok.test:99999/")


def test_a_drip_fed_answer_hits_the_deadline(port: int) -> None:
    started = time.monotonic()
    with pytest.raises(HttpError, match="longer than"):
        guard().get(f"http://ok.test:{port}/drip", deadline_s=0.5)
    assert time.monotonic() - started < 3


def test_a_large_answer_is_refused(port: int) -> None:
    with pytest.raises(HttpError, match="larger than"):
        guard().get(f"http://ok.test:{port}/body", max_bytes=3)


def test_stream_yields_until_closed(port: int) -> None:
    chunks = guard().stream(f"http://ok.test:{port}/drip")
    assert next(chunks) == b"x"
    chunks.close()


def test_a_hanging_name_lookup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netguard, "RESOLVE_S", 0.1)

    def hang(*_args: object, **_kwargs: object) -> list[Any]:
        time.sleep(2)
        return []

    monkeypatch.setattr(netguard.socket, "getaddrinfo", hang)
    with pytest.raises(HttpError, match="timed out"):
        netguard.resolve("slow.test", 443)


def test_a_failed_name_lookup_is_an_error() -> None:
    with pytest.raises(HttpError, match="lookup failed"):
        netguard.resolve("name.invalid", 443)


@pytest.mark.live
def test_https_through_the_guard() -> None:
    assert b"<html" in Guard().get("https://example.com/").lower()
