import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tunebox import http


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/missing":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/aac")
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"x" * 4096)
                self.wfile.flush()
        except OSError:
            pass  # the reader hung up, as it should

    def log_message(self, format: str, *args: Any) -> None:  # noqa: V107
        pass


@pytest.fixture
def base() -> Generator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_stream_yields_chunks_until_closed(base: str) -> None:
    chunks = http.stream(f"{base}/live")
    received = 0
    for chunk in chunks:
        received += len(chunk)
        if received >= 20_000:
            break
    chunks.close()
    assert received >= 20_000


def test_stream_turns_http_errors_into_http_error(base: str) -> None:
    with pytest.raises(http.HttpError, match="/missing"):
        next(http.stream(f"{base}/missing"))


def test_stream_turns_refused_connections_into_http_error() -> None:
    with pytest.raises(http.HttpError):
        next(http.stream("http://127.0.0.1:9/x", timeout=2))
