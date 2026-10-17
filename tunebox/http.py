"""Outbound HTTP GET with one user agent, a timeout and a size cap."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable

USER_AGENT = "tunebox/0.1 (+https://github.com/Rouzax/tunebox)"
# Feeds and search answers are a few kB; anything this large is not one.
MAX_BYTES = 2_000_000

HttpGet = Callable[[str], bytes]


class HttpError(Exception):
    pass


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
