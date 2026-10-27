"""Outbound fetches of addresses that come from configuration (logos,
stream measurements) or from the panel's checks (Check stream, and Check
feed's provider host). Only public addresses are reached, checked again on
every redirect and connected to as checked, and every fetch ends by a
deadline, so a saved or typed URL can neither reach the home network nor
hold a thread. Catalogue searches and the pollers' title feeds go to fixed
provider hosts and use tunebox.http directly.

client_address() and nearby() are the inbound side: which address a
request comes from, believing X-Forwarded-For only from trusted proxies,
and whether that address is on the owner's own network."""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Generator
from dataclasses import dataclass

from tunebox.config import Network
from tunebox.http import MAX_BYTES, STREAM_CHUNK, USER_AGENT, HttpError

MAX_REDIRECTS = 5
# getaddrinfo has no timeout of its own.
RESOLVE_S = 3.0
_REDIRECTS = (301, 302, 303, 307, 308)
_TLS = ssl.create_default_context()

Resolver = Callable[[str, int], list[str]]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# What a request line may carry as is; everything else in a path (a typed
# "é", a redirect to "/ström") is percent-encoded, as browsers do.
_PATH_SAFE = "/%:@!$&'()*+,;=?~-._"


@dataclass(frozen=True)
class Probe:
    """The start of an answer: its content type, the icy-* and ice-*
    headers a stream server sends (names lower-case) and its first bytes."""

    content_type: str
    icy: dict[str, str]
    head: bytes


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
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 addresses inside IPv6 are judged as IPv4: ::ffff:0:0/96
        # (mapped), 64:ff9b::/96 (NAT64, which a resolver behind DNS64
        # answers) and the old IPv4-compatible ::/96, whose private
        # addresses would otherwise read as global.
        packed = int(ip)
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or (packed >> 32 == 0 and packed > 1):
            ip = ipaddress.IPv4Address(packed & 0xFFFF_FFFF)
    # is_global is false for private, loopback, link-local, reserved and
    # shared (100.64.0.0/10) ranges, but true for multicast.
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """Looks the name up in a helper thread, so a hanging resolver costs
    that thread rather than the caller."""
    found: list[str] = []
    failed: list[Exception] = []

    def look_up() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            found.extend(str(info[4][0]) for info in infos)
        # UnicodeError: a name the IDNA codec refuses (a label over 63
        # characters) fails before any lookup.
        except (OSError, UnicodeError) as error:
            failed.append(error)

    worker = threading.Thread(target=look_up, name="resolve", daemon=True)
    worker.start()
    worker.join(RESOLVE_S)
    if worker.is_alive():
        raise HttpError(f"{host}: name lookup timed out", unreachable=True)
    if failed or not found:
        raise HttpError(f"{host}: name lookup failed", unreachable=True)
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

    def __init__(
        self, host: str, port: int, address: str, tls: bool, timeout: float, cut: threading.Event
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address
        self._tls = tls
        self._cut = cut
        # Keeps the Host header free of :443 on TLS.
        self.default_port = 443 if tls else 80
        # The socket the answer is read from. http.client drops self.sock
        # once it has an answer that ends with the connection (HTTP/1.0
        # without a length, as stream servers send), while the body is
        # still read from it.
        self.opened: socket.socket | None = None

    def connect(self) -> None:
        # Each socket is made visible to _cut before it is used, and the cut
        # checked after: either the cut sees the socket, or this sees the
        # cut. The TLS socket replaces the raw one before its handshake,
        # which a slow server could otherwise stretch past the deadline.
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.opened = sock
        if self._cut.is_set():
            raise TimeoutError
        if self._tls:
            sock = _TLS.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.opened = sock
            if self._cut.is_set():
                raise TimeoutError
            sock.do_handshake()
        self.sock = sock


def _cut(connection: _Pinned, cut: threading.Event) -> None:
    cut.set()
    sock = connection.opened
    if sock is not None:
        # Already closed means the request ended on its own.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


@dataclass
class _Exchange:
    """An answer being read. watchdog, when set, cuts the connection at the
    deadline: the socket timeout bounds one recv, and http.client makes many
    recvs inside one read (a chunk-size line, a header line), so only the
    cut bounds the whole exchange."""

    response: http.client.HTTPResponse
    connection: _Pinned
    cut: threading.Event
    watchdog: threading.Timer | None

    def close(self) -> None:
        if self.watchdog is not None:
            self.watchdog.cancel()
        self.connection.close()


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
        opened = self._open(url, ends, self._get_schemes)
        late = HttpError(f"{_short(url)}: took longer than {deadline_s:g} s")
        try:
            body = bytearray()
            while chunk := opened.response.read1(STREAM_CHUNK):
                body += chunk
                if len(body) > max_bytes:
                    raise HttpError(
                        f"{_short(url)}: response larger than {max_bytes} bytes", too_large=True
                    )
            # A cut connection reads as a short, complete answer.
            if opened.cut.is_set():
                raise late
            return bytes(body)
        except (http.client.HTTPException, OSError) as error:
            if opened.cut.is_set() or isinstance(error, TimeoutError):
                raise late from error
            raise HttpError(f"{_short(url)}: {error}") from error
        finally:
            opened.close()

    def stream(self, url: str) -> Generator[bytes]:
        """An endless answer such as an audio stream; http is allowed, as
        for a stream_url. The caller bounds how long it reads."""
        ends = time.monotonic() + self._timeout
        opened = self._open(url, ends, ("https", "http"), hold=False)
        try:
            while chunk := opened.response.read1(STREAM_CHUNK):
                yield chunk
        except (http.client.HTTPException, OSError) as error:
            raise HttpError(f"{_short(url)}: {error}") from error
        finally:
            opened.close()

    def probe(self, url: str, deadline_s: float, max_bytes: int) -> Probe:
        """The headers and first bytes of an answer, for a stream the panel
        checks: http is allowed, as for a stream_url, and reading stops at
        max_bytes or the deadline, whichever comes first. A stream that
        stalls after its first bytes ends the probe with what came."""
        ends = time.monotonic() + deadline_s
        opened = self._open(url, ends, ("https", "http"))
        try:
            head = bytearray()
            try:
                while len(head) < max_bytes and time.monotonic() < ends:
                    chunk = opened.response.read1(min(STREAM_CHUNK, max_bytes - len(head)))
                    if not chunk:
                        break
                    head += chunk
            except (http.client.HTTPException, OSError) as error:
                stopped = opened.cut.is_set() or isinstance(error, TimeoutError)
                if not stopped:
                    raise
                if not head:
                    raise HttpError(f"{_short(url)}: took too long") from error
            if opened.cut.is_set() and not head:
                raise HttpError(f"{_short(url)}: took too long")
            icy = {
                name.lower(): value
                for name, value in opened.response.getheaders()
                if name.lower().startswith(("icy-", "ice-"))
            }
            return Probe(opened.response.getheader("Content-Type") or "", icy, bytes(head))
        except (http.client.HTTPException, OSError) as error:
            raise HttpError(f"{_short(url)}: {error}") from error
        finally:
            opened.close()

    def _open(
        self, url: str, ends: float, schemes: tuple[str, ...], hold: bool = True
    ) -> _Exchange:
        """hold: the deadline keeps cutting the connection while the caller
        reads the body; stream() reads without end and bounds itself."""
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
                    f"{_short(url)}: refused, {parts.hostname} has a non-public address",
                    unreachable=True,
                )
            remaining = ends - time.monotonic()
            if remaining <= 0:
                raise HttpError(f"{_short(url)}: took too long")
            tls = parts.scheme == "https"
            timeout = min(self._timeout, remaining)
            cut = threading.Event()
            connection = _Pinned(parts.hostname, port, addresses[0], tls, timeout, cut)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            path = urllib.parse.quote(path, safe=_PATH_SAFE)
            # The socket timeout bounds each recv only: a server sending its
            # headers a byte at a time would hold the request far past the
            # deadline, so the deadline also cuts the connection. A cut
            # connection can also read as a short, complete answer, so the
            # cut itself is what fails the request.
            watchdog = threading.Timer(max(0.0, ends - time.monotonic()), _cut, (connection, cut))
            watchdog.start()
            try:
                connection.request("GET", path, headers={"User-Agent": USER_AGENT})
                response = connection.getresponse()
            except (http.client.HTTPException, OSError, UnicodeError) as error:
                watchdog.cancel()
                connection.close()
                late = cut.is_set() or time.monotonic() >= ends
                raise HttpError(f"{_short(url)}: {'took too long' if late else error}") from error
            opened = _Exchange(response, connection, cut, watchdog)
            if cut.is_set():
                opened.close()
                raise HttpError(f"{_short(url)}: took too long")
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                opened.close()
                if not location:
                    raise HttpError(f"{_short(url)}: redirect without a location")
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                opened.close()
                raise HttpError(f"{_short(url)}: HTTP {response.status}", response.status)
            if not hold:
                watchdog.cancel()
                opened.watchdog = None
            return opened
        raise HttpError(f"{_short(url)}: too many redirects")
