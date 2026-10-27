"""The panel's stream and feed checks: what a stream address plays, and
what a now-playing source reports, before a station is saved."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Protocol

from tunebox.config import SourceConfig
from tunebox.http import HttpError
from tunebox.logs import event
from tunebox.netguard import RESOLVE_S, Probe
from tunebox.sources import build_source
from tunebox.sources.base import SourceError
from tunebox.tracks import RawEntry

log = logging.getLogger(__name__)

# Long enough for a station's redirect and its first audio; a stream check
# holds a request thread for at most this long.
PROBE_DEADLINE_S = 6.0
# A feed answers in kilobytes; the cap and deadline keep a feed host that
# misbehaves from holding the check slot or memory.
FEED_DEADLINE_S = 8.0
FEED_MAX_BYTES = 262_144
# The longest a check can hold the slot: its deadline, plus one name lookup
# begun just before the deadline passed.
STREAM_BOUND_S = PROBE_DEADLINE_S + RESOLVE_S
FEED_BOUND_S = FEED_DEADLINE_S + RESOLVE_S
# A few seconds of audio: enough to see that it flows and to sniff it.
PROBE_BYTES = 65_536
# Checks per minute, all sessions together: each one opens a connection
# tunebox makes on the owner's behalf.
PER_MINUTE = 12
URL_MAX = 2048
FEED_ENTRIES = 5
# Asked of the feed: a few more than shown, as station IDs and jingles may
# be among them. They come without an artist (the only entries a station_id
# rule ever applies to), and the check leaves every such entry out.
FEED_FETCH = 10
NAME_MAX = 60
REASON_MAX = 120
UNREADABLE = "The feed answered something tunebox cannot read."
UNREACHABLE = (
    "tunebox cannot check this address: its name is unknown, or it is not a public address."
)

_CODECS = {
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/x-aac": "aac",
    "audio/mp4": "aac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mpeg3": "mp3",
}
_PLAYLISTS = (
    "audio/x-mpegurl",
    "audio/mpegurl",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/x-scpls",
    "application/pls+xml",
    "video/x-ms-asf",
    "audio/x-ms-wax",
    "application/x-quicktimeplayer",
)
# How .m3u, .pls and .asx files begin when the server sends no playlist type.
_PLAYLIST_STARTS = (b"#extm3u", b"[playlist]", b"<asx")
# Where each playlist format names its streams: .pls as File1=..., .asx as
# <ref href="...">, .m3u on lines of their own between # comments. An
# .asx also holds other addresses (its author's site), so the general
# pattern is only the fallback.
# Blanks are [ \t], not \s: \s also takes newlines, and an answer of empty
# lines then costs quadratic time with the GIL held.
_PLS_ENTRY = re.compile(rb"^[ \t]*file\d+[ \t]*=[ \t]*(https?://\S+)", re.IGNORECASE | re.MULTILINE)
_ASX_ENTRY = re.compile(rb"<ref\s+href\s*=\s*[\"']?(https?://[^\"'\s>]+)", re.IGNORECASE)
_M3U_ENTRY = re.compile(rb"^[ \t]*(https?://\S+)", re.MULTILINE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Busy(Exception):
    """Another check is running (running), or the minute's checks are used
    up; retry_s is when a check can run again."""

    def __init__(self, running: bool, retry_s: int) -> None:
        super().__init__("check_running" if running else "too_many_checks")
        self.running = running
        self.retry_s = retry_s


class GuardedGet(Protocol):
    def get(self, url: str, deadline_s: float, max_bytes: int) -> bytes: ...


def guarded_feed(guard: GuardedGet) -> Callable[[SourceConfig], list[RawEntry]]:
    """The feed check's fetch. The pollers ask the fixed feed hosts with the
    plain client; a check's answer goes back to the panel, so it fetches
    through the guard: a provider's redirect is judged like any address,
    and the fetch is bounded in time and bytes."""

    def get(url: str) -> bytes:
        return guard.get(url, FEED_DEADLINE_S, FEED_MAX_BYTES)

    return lambda source: build_source(source, get).fetch(FEED_FETCH)


def codec_of(content_type: str, head: bytes) -> str | None:
    """aac or mp3 from the declared type, else from the first frame."""
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared in _CODECS:
        return _CODECS[declared]
    if head.startswith(b"ID3"):
        return "mp3"
    for index in range(min(len(head) - 1, 4096)):
        if head[index] != 0xFF:
            continue
        second = head[index + 1]
        # ADTS (AAC): sync 0xFFF and layer 00; MPEG audio: sync 0xFFE and a
        # layer other than 00.
        if second & 0xF6 == 0xF0:
            return "aac"
        if second & 0xE0 == 0xE0 and second & 0x06:
            return "mp3"
    return None


def kbps_of(icy: dict[str, str]) -> int | None:
    """The bitrate a stream server announces: icy-br, as "128" or "128,128",
    else ice-audio-info's bitrate=128."""
    found = re.match(r"\s*(\d{1,4})\b", icy.get("icy-br", "")) or re.search(
        r"(?:^|;)\s*bitrate=(\d{1,4})\b", icy.get("ice-audio-info", "")
    )
    if found is None:
        return None
    kbps = int(found.group(1))
    return kbps if 0 < kbps < 10_000 else None


def name_of(icy: dict[str, str]) -> str | None:
    name = _CONTROL.sub("", icy.get("icy-name", "")).strip()[:NAME_MAX]
    return name or None


def playlist_address(probe: Probe) -> str | None:
    """The first stream address in a playlist file, or None when the answer
    is no playlist or names none."""
    declared = probe.content_type.split(";", 1)[0].strip().lower()
    if declared not in _PLAYLISTS and not probe.head.lstrip().lower().startswith(_PLAYLIST_STARTS):
        return None
    found = (
        _PLS_ENTRY.search(probe.head)
        or _ASX_ENTRY.search(probe.head)
        or _M3U_ENTRY.search(probe.head)
    )
    if found is None or len(found.group(1)) > URL_MAX:
        return None
    # Playlists are UTF-8 or ASCII; a non-ASCII path stays as written, and
    # the probe percent-encodes it when the address is checked.
    return found.group(1).decode("utf-8", "replace")


def describe(probe: Probe, url: str) -> dict[str, Any]:
    """What the panel shows for a probed address. A playlist names the
    stream inside it, for the form to offer; a plain-http stream plays but
    the station form saves https:// streams only."""
    declared = probe.content_type.split(";", 1)[0].strip().lower()
    if b"#EXT-X-" in probe.head:
        return {
            "ok": False,
            "reason": "This is an HLS stream (a list of short segments), which the player"
            " cannot play. Look for an MP3 or AAC stream address.",
        }
    inside = playlist_address(probe)
    if inside is not None or declared in _PLAYLISTS:
        answer: dict[str, Any] = {"ok": False, "reason": "This is a playlist file, not a stream."}
        if inside is not None:
            answer["inside"] = inside
        return answer
    codec = codec_of(probe.content_type, probe.head)
    if codec is None:
        shown = _CONTROL.sub("", declared)[:40] or "no content type"
        return {"ok": False, "reason": f"No AAC or MP3 audio came back ({shown})."}
    if not probe.head:
        return {"ok": False, "reason": "The server answered but sent no audio."}
    answer = {"ok": True, "codec": codec, "kbps": kbps_of(probe.icy), "name": name_of(probe.icy)}
    if url.lower().startswith("http://"):
        answer["warning"] = (
            "It plays over plain http, which browsers block on the player's https page;"
            " a stream in the form needs an https:// address."
        )
    return answer


def _reason(error: HttpError) -> str:
    if error.unreachable:
        return UNREACHABLE
    # An old SHOUTcast server opens its answer with "ICY 200 OK", which is
    # no HTTP; the parser's complaint about the status line says nothing.
    if "ICY 200" in str(error):
        return (
            "An old SHOUTcast server answered (ICY instead of HTTP): it serves plain http"
            " only, which browsers block on the player's https page."
        )
    return _tidy(str(error))


def _tidy(text: str) -> str:
    """An error as the panel shows it: another server's words (an SSH
    banner on a mistyped port, say) come back on one short line."""
    return _CONTROL.sub("", text)[:REASON_MAX]


class StreamChecks:
    """One check at a time, PER_MINUTE a minute; a check that cannot run
    raises Busy rather than waiting."""

    def __init__(
        self,
        probe: Callable[[str], Probe],
        feed: Callable[[SourceConfig], list[RawEntry]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._feed = feed
        self._clock = clock
        # One lock over the running flag, the minute's checks and when the
        # running check must be done, so a refused check reads all three as
        # one moment.
        self._lock = threading.Lock()
        self._busy = False
        self._times: deque[float] = deque()
        self._ends = 0.0

    def stream(self, url: str) -> dict[str, Any]:
        with self._logged("stream_checked") as logged:
            with self._slot(STREAM_BOUND_S):
                try:
                    answer = describe(self._probe(url), url)
                except HttpError as error:
                    answer = {"ok": False, "reason": _reason(error)}
            logged.update(outcome="ok" if answer["ok"] else "refused", codec=answer.get("codec"))
            return answer

    def feed(self, source: SourceConfig) -> dict[str, Any]:
        with self._logged("feed_checked", source=source.type) as logged:
            with self._slot(FEED_BOUND_S):
                try:
                    entries = self._feed(source)
                except SourceError as error:
                    answer: dict[str, Any] = {"ok": False, "reason": _tidy(str(error))}
                except Exception as error:
                    # A parser is meant to raise SourceError, so this is a bug
                    # to see in the log; its text says nothing the panel can use.
                    logged["error"] = type(error).__name__
                    log.warning(
                        "feed_check_crashed",
                        exc_info=True,
                        extra={"fields": {"source": source.type}},
                    )
                    return {"ok": False, "reason": UNREADABLE}
                else:
                    songs_only = [entry for entry in entries if entry.artist.strip()]
                    newest = sorted(songs_only, key=lambda e: e.started_at_ms or 0, reverse=True)
                    songs = [
                        {"artist": entry.artist, "title": entry.title}
                        for entry in newest[:FEED_ENTRIES]
                    ]
                    answer = (
                        {"ok": True, "songs": songs}
                        if songs
                        else {"ok": False, "reason": "The feed answered but lists no songs."}
                    )
            logged.update(outcome="ok" if answer["ok"] else "refused")
            return answer

    @contextmanager
    def _logged(self, name: str, **fields: object) -> Generator[dict[str, object]]:
        """One event per check, also for a refused slot (outcome busy) and
        a crash (outcome error); the caller fills in its outcome."""
        started = time.monotonic()
        logged: dict[str, object] = {"outcome": "error", **fields}
        try:
            yield logged
        except Busy:
            logged["outcome"] = "busy"
            raise
        finally:
            event(
                log,
                logging.INFO,
                name,
                duration_ms=round((time.monotonic() - started) * 1000),
                **logged,
            )

    @contextmanager
    def _slot(self, bound_s: float) -> Generator[None]:
        """bound_s: the longest this check can hold the slot; a check refused
        meanwhile is told to wait what is left of it."""
        with self._lock:
            now = self._clock()
            if self._busy:
                raise Busy(running=True, retry_s=max(1, math.ceil(self._ends - now)))
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            if len(self._times) >= PER_MINUTE:
                raise Busy(running=False, retry_s=max(1, math.ceil(60 - (now - self._times[0]))))
            self._times.append(now)
            self._busy = True
            self._ends = now + bound_s
        try:
            yield
        finally:
            with self._lock:
                self._busy = False
