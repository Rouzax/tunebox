"""Outbound fetches of addresses that come from configuration (logos,
stream measurements). Only public addresses are reached, checked again on
every redirect and connected to as checked, and every fetch ends by a
deadline, so a saved URL can neither reach the home network nor hold a
thread. Catalogue and title feeds go to fixed provider hosts and use
tunebox.http directly.

client_address() and nearby() are the inbound side: which address a
request comes from, believing X-Forwarded-For only from trusted proxies,
and whether that address is on the owner's own network."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Generator

from tunebox.config import Network
from tunebox.http import MAX_BYTES, STREAM_CHUNK, USER_AGENT, HttpError

MAX_REDIRECTS = 5
# getaddrinfo has no timeout of its own.
RESOLVE_S = 3.0
_REDIRECTS = (301, 302, 303, 307, 308)
_TLS = ssl.create_default_context()

Resolver = Callable[[str, int], list[str]]

# Headers through which proxies name the client, besides X-Forwarded-For.
OTHER_FORWARDING = (
    "Forwarded",
    "X-Real-IP",
    "CF-Connecting-IP",
    "True-Client-IP",
    "X-Client-IP",
)

# What a home network, a VPN or a tailnet hands out. Not is_private: that
# also holds documentation and benchmark ranges no home uses.
NEARBY = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global is false for private, loopback, link-local, reserved and
    # shared (100.64.0.0/10) ranges, but true for multicast.
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """Looks the name up in a helper thread, so a hanging resolver costs
    that thread rather than the caller."""
    found: list[str] = []
    failed: list[OSError] = []

    def look_up() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            found.extend(str(info[4][0]) for info in infos)
        except OSError as error:
            failed.append(error)

    worker = threading.Thread(target=look_up, name="resolve", daemon=True)
    worker.start()
    worker.join(RESOLVE_S)
    if worker.is_alive():
        raise HttpError(f"{host}: name lookup timed out")
    if failed or not found:
        raise HttpError(f"{host}: name lookup failed")
    return found


def trusted_peer(address: str, trusted: tuple[Network, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in trusted)


def client_address(peer: str, forwarded: str | None, trusted: tuple[Network, ...]) -> str:
    """The client's address: from X-Forwarded-For only when the connection
    comes from a trusted proxy, taking the right-most entry that is not
    itself a trusted proxy (entries to its left are the client's own word).
    An entry that is not an address stops the walk: skipping it would reach
    an entry the client wrote."""
    if not forwarded or not trusted_peer(peer, trusted):
        return peer
    for entry in reversed([part.strip() for part in forwarded.split(",")]):
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            return peer
        if not trusted_peer(entry, trusted):
            return entry
    return peer


def nearby(
    peer: str, forwarded: str | None, other_forwarding: bool, trusted: tuple[Network, ...]
) -> bool:
    """Whether the client is on the owner's network. Fails closed where
    client_address falls back to the peer: a request passed on by a proxy
    that is not trusted never counts (on the same host such a proxy
    connects from loopback while relaying the internet), nor does one from
    a trusted proxy that names no readable client. other_forwarding is any
    OTHER_FORWARDING header, which tunebox does not read."""
    if not trusted_peer(peer, trusted):
        return not forwarded and not other_forwarding and _on_network(peer)
    if not forwarded:
        return False
    for entry in reversed([part.strip() for part in forwarded.split(",")]):
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            return False
        if not trusted_peer(entry, trusted):
            return _on_network(entry)
    return False


def _on_network(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in NEARBY)


def _short(url: str) -> str:
    return url.split("?")[0]


class _Pinned(http.client.HTTPConnection):
    """Connects to the address that was checked, not to whatever the name
    resolves to by the time the socket opens; TLS still verifies the name."""

    def __init__(self, host: str, port: int, address: str, tls: bool, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address
        self._tls = tls
        # Keeps the Host header free of :443 on TLS.
        self.default_port = 443 if tls else 80

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        if self._tls:
            sock = _TLS.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


class Guard:
    def __init__(
        self,
        resolver: Resolver = resolve,
        allow: Callable[[str], bool] = public,
        https_only: bool = True,
        timeout_s: float = 10.0,
    ) -> None:
        self._resolve = resolver
        self._allow = allow
        self._get_schemes = ("https",) if https_only else ("https", "http")
        self._timeout = timeout_s

    def get(self, url: str, deadline_s: float = 15.0, max_bytes: int = MAX_BYTES) -> bytes:
        ends = time.monotonic() + deadline_s
        response, connection = self._open(url, ends, self._get_schemes)
        try:
            body = bytearray()
            while chunk := response.read1(STREAM_CHUNK):
                body += chunk
                if len(body) > max_bytes:
                    raise HttpError(f"{_short(url)}: response larger than {max_bytes} bytes")
                if time.monotonic() > ends:
                    raise HttpError(f"{_short(url)}: took longer than {deadline_s:g} s")
            return bytes(body)
        except (http.client.HTTPException, OSError) as error:
            raise HttpError(f"{_short(url)}: {error}") from error
        finally:
            connection.close()

    def stream(self, url: str) -> Generator[bytes]:
        """An endless answer such as an audio stream; http is allowed, as
        for a stream_url. The caller bounds how long it reads."""
        ends = time.monotonic() + self._timeout
        response, connection = self._open(url, ends, ("https", "http"))
        try:
            while chunk := response.read1(STREAM_CHUNK):
                yield chunk
        except (http.client.HTTPException, OSError) as error:
            raise HttpError(f"{_short(url)}: {error}") from error
        finally:
            connection.close()

    def _open(
        self, url: str, ends: float, schemes: tuple[str, ...]
    ) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                parts = urllib.parse.urlsplit(url)
                port = parts.port or (443 if parts.scheme == "https" else 80)
            except ValueError as error:
                raise HttpError(f"{_short(url)}: refused, {error}") from error
            if parts.scheme not in schemes or not parts.hostname:
                raise HttpError(f"{_short(url)}: refused scheme or host")
            addresses = self._resolve(parts.hostname, port)
            if not all(self._allow(address) for address in addresses):
                raise HttpError(
                    f"{_short(url)}: refused, {parts.hostname} has a non-public address"
                )
            remaining = ends - time.monotonic()
            if remaining <= 0:
                raise HttpError(f"{_short(url)}: took too long")
            tls = parts.scheme == "https"
            timeout = min(self._timeout, remaining)
            connection = _Pinned(parts.hostname, port, addresses[0], tls, timeout)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            try:
                connection.request("GET", path, headers={"User-Agent": USER_AGENT})
                response = connection.getresponse()
            except (http.client.HTTPException, OSError) as error:
                connection.close()
                raise HttpError(f"{_short(url)}: {error}") from error
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                connection.close()
                if not location:
                    raise HttpError(f"{_short(url)}: redirect without a location")
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                connection.close()
                raise HttpError(f"{_short(url)}: HTTP {response.status}")
            return response, connection
        raise HttpError(f"{_short(url)}: too many redirects")
