"""Outbound HTTP GET with one user agent and a timeout; whole answers have a size cap."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable, Generator

USER_AGENT = "tunebox/0.1 (+https://github.com/Rouzax/tunebox)"
# Feeds and search answers are a few kB; anything this large is not one.
MAX_BYTES = 2_000_000
STREAM_CHUNK = 16_384

HttpGet = Callable[[str], bytes]


class HttpError(Exception):
    """status is the HTTP status of a refused answer, None for a transport
    failure; a 404 or 410 tells a dead link from a host that is down."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def get(url: str, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise HttpError(f"{url.split('?')[0]}: {error}") from error
    if len(body) > MAX_BYTES:
        raise HttpError(f"{url.split('?')[0]}: response larger than {MAX_BYTES} bytes")
    return body


def stream(url: str, timeout: float = 10.0) -> Generator[bytes]:
    """Yields an endless response, such as an audio stream, as it arrives.
    The connection stays open until the generator is closed."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while chunk := response.read1(STREAM_CHUNK):
                yield chunk
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise HttpError(f"{url.split('?')[0]}: {error}") from error
