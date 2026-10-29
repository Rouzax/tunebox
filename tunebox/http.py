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
# Per socket operation, not per call.
TIMEOUT_S = 10.0

HttpGet = Callable[[str], bytes]


class HttpError(Exception):
    """status is set only when the host answered with an HTTP error status
    (from get() and from the guarded fetch); it is None for a transport
    failure, a refused address and an oversized body. A 404 or 410 tells a
    dead link from a host that is down. too_large is set only by the guarded
    fetch, for a body past the caller's max_bytes: the answer's fault, not
    the network's. get() has a fixed cap and does not set it. unreachable is
    set by the guard for a name that does not resolve and for an address it
    refuses as non-public: a caller that shows the error to someone outside
    must word the two alike, or the difference maps the home network."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        too_large: bool = False,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.too_large = too_large
        self.unreachable = unreachable


def get(url: str, timeout: float = TIMEOUT_S) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise HttpError(f"{url.split('?')[0]}: {error}", error.code) from error
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise HttpError(f"{url.split('?')[0]}: {error}") from error
    if len(body) > MAX_BYTES:
        raise HttpError(f"{url.split('?')[0]}: response larger than {MAX_BYTES} bytes")
    return body


def stream(url: str, timeout: float = TIMEOUT_S) -> Generator[bytes]:
    """Yields an endless response, such as an audio stream, as it arrives.
    The connection stays open until the generator is closed."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while chunk := response.read1(STREAM_CHUNK):
                yield chunk
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise HttpError(f"{url.split('?')[0]}: {error}") from error
