import logging
import math
import threading
import time

import pytest

from tunebox.config import SourceConfig
from tunebox.http import HttpError
from tunebox.netguard import Probe
from tunebox.sources.base import SourceError
from tunebox.streamcheck import (
    FEED_DEADLINE_S,
    FEED_FETCH,
    FEED_MAX_BYTES,
    PER_MINUTE,
    STREAM_BOUND_S,
    UNREACHABLE,
    Busy,
    StreamChecks,
    codec_of,
    describe,
    guarded_feed,
    kbps_of,
    name_of,
)
from tunebox.tracks import RawEntry

ADTS = b"\xff\xf1\x50\x80" + b"\x00" * 60
MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 60
TRITON = SourceConfig("triton", {"mount": "OWR_INTERNATIONAL"})


def test_codec_from_the_declared_type() -> None:
    assert codec_of("audio/aacp", b"") == "aac"
    assert codec_of("audio/mpeg; charset=x", b"") == "mp3"
    assert codec_of("AUDIO/AAC", b"") == "aac"


def test_codec_from_the_first_frame_when_the_type_says_nothing() -> None:
    assert codec_of("application/octet-stream", ADTS) == "aac"
    assert codec_of("", MP3) == "mp3"
    assert codec_of("", b"ID3\x04" + b"\x00" * 20) == "mp3"
    assert codec_of("", b"<html>not audio</html>") is None
    assert codec_of("", b"") is None


def test_bitrate_and_name_from_icy_headers() -> None:
    assert kbps_of({"icy-br": "256"}) == 256
    assert kbps_of({"icy-br": "128,128"}) == 128
    assert kbps_of({"icy-br": "fast"}) is None
    assert kbps_of({"icy-br": "0"}) is None
    assert kbps_of({}) is None
    assert kbps_of({"ice-audio-info": "samplerate=48000;channels=2;bitrate=192"}) == 192
    assert kbps_of({"icy-br": "128", "ice-audio-info": "bitrate=192"}) == 128
    assert name_of({"icy-name": " NPO Radio2 "}) == "NPO Radio2"
    assert name_of({"icy-name": "a\x00b\x1bc"}) == "abc"
    assert name_of({"icy-name": "x" * 200}) == "x" * 60
    assert name_of({"icy-name": ""}) is None


def test_describe_a_stream() -> None:
    probe = Probe("audio/aacp", {"icy-br": "256", "icy-name": "OWR_INTERNATIONAL"}, ADTS)
    assert describe(probe, "https://example.test/live") == {
        "ok": True,
        "codec": "aac",
        "kbps": 256,
        "name": "OWR_INTERNATIONAL",
    }


def test_describe_refuses_a_playlist_and_a_page() -> None:
    playlist = describe(Probe("audio/x-mpegurl", {}, b"#EXTM3U\n"), "https://example.test/a.m3u")
    assert not playlist["ok"] and "playlist" in playlist["reason"]
    page = describe(
        Probe("text/html; charset=utf-8", {}, b"<html></html>"), "https://example.test/"
    )
    assert page == {"ok": False, "reason": "No AAC or MP3 audio came back (text/html)."}
    silent = describe(Probe("audio/mpeg", {}, b""), "https://example.test/live")
    assert not silent["ok"] and "no audio" in silent["reason"]


PLAYING = Probe("audio/mpeg", {"icy-br": "128"}, MP3)


def checks(
    probe: Probe | Exception = PLAYING,
    entries: list[RawEntry] | Exception | None = None,
    now: list[float] | None = None,
) -> StreamChecks:
    clock = now if now is not None else [0.0]

    def do_probe(_url: str) -> Probe:
        if isinstance(probe, Exception):
            raise probe
        return probe

    def do_feed(_source: SourceConfig) -> list[RawEntry]:
        if isinstance(entries, Exception):
            raise entries
        return entries or []

    return StreamChecks(do_probe, do_feed, clock=lambda: clock[0])


def test_a_failed_fetch_is_an_answer_not_an_error() -> None:
    answer = checks(HttpError("example.test: HTTP 404", 404))
    assert answer.stream("http://example.test/") == {
        "ok": False,
        "reason": "example.test: HTTP 404",
    }


@pytest.mark.parametrize(
    "error",
    [
        HttpError("printer.lan: name lookup failed", unreachable=True),
        HttpError("printer.lan: name lookup timed out", unreachable=True),
        HttpError("http://nas.lan/: refused, nas.lan has a non-public address", unreachable=True),
    ],
)
def test_an_unknown_name_and_a_home_address_read_the_same(error: HttpError) -> None:
    # Two different texts would tell which names exist inside the network.
    answer = checks(error).stream("http://nas.lan/")
    assert answer == {"ok": False, "reason": UNREACHABLE}
    assert "lan" not in UNREACHABLE


def test_checks_are_paced_per_minute() -> None:
    now = [100.0]
    paced = checks(now=now)
    for _ in range(PER_MINUTE):
        assert paced.stream("https://example.test/a.mp3")["ok"]
        now[0] += 1
    with pytest.raises(Busy) as used_up:
        paced.stream("https://example.test/a.mp3")
    # Room again once the oldest check of the minute leaves its window.
    assert (used_up.value.running, used_up.value.retry_s) == (False, 60 - PER_MINUTE)
    now[0] += 60 - PER_MINUTE
    assert paced.stream("https://example.test/a.mp3")["ok"]


def test_one_check_at_a_time() -> None:
    entered = threading.Event()
    release = threading.Event()
    now = [100.0]

    def slow(_url: str) -> Probe:
        entered.set()
        release.wait(5)
        return Probe("audio/mpeg", {}, MP3)

    paced = StreamChecks(slow, lambda _source: [], clock=lambda: now[0])
    first = threading.Thread(target=paced.stream, args=("https://example.test/a",))
    first.start()
    assert entered.wait(5)
    now[0] += 3.5
    with pytest.raises(Busy) as running:
        paced.feed(TRITON)
    # The wait is what is left of the running check's longest run.
    assert (running.value.running, running.value.retry_s) == (
        True,
        math.ceil(STREAM_BOUND_S - 3.5),
    )
    release.set()
    first.join(5)
    # The slot is free again afterwards, also after a refused check.
    assert paced.stream("https://example.test/a")["ok"]


def test_feed_lists_the_newest_songs() -> None:
    entries = [RawEntry(f"Artist {n}", f"Song {n}", n * 1000) for n in range(8)]
    answer = checks(entries=entries).feed(TRITON)
    assert answer["ok"]
    assert [song["title"] for song in answer["songs"]] == [
        "Song 7",
        "Song 6",
        "Song 5",
        "Song 4",
        "Song 3",
    ]


def test_feed_failure_and_empty_feed() -> None:
    assert checks(entries=SourceError("HTTP 404")).feed(TRITON) == {
        "ok": False,
        "reason": "HTTP 404",
    }
    empty = checks(entries=[]).feed(TRITON)
    assert not empty["ok"] and "no songs" in empty["reason"]


@pytest.mark.parametrize(
    ("content_type", "body", "inside"),
    [
        (
            "audio/x-scpls",
            b"[playlist]\nnumberofentries=1\nFile1=https://a.test/live.mp3\nTitle1=A\n",
            "https://a.test/live.mp3",
        ),
        ("audio/x-mpegurl", b"http://b.test:80/3aac\n", "http://b.test:80/3aac"),
        (
            "application/vnd.apple.mpegurl",
            b"[playlist]\nFile1=http://c.test:9016/listen.pls?sid=1\n",
            "http://c.test:9016/listen.pls?sid=1",
        ),
        (
            "video/x-ms-asf",
            b'<asx version = "3.0">\n<Author>https://www.d.test</Author>\n'
            b'<entry><ref href="http://d.test:8982/1_mp3_192" /></entry></asx>',
            "http://d.test:8982/1_mp3_192",
        ),
        ("text/plain", b"#EXTM3U\n#EXTINF:-1,E\nhttps://e.test/stream\n", "https://e.test/stream"),
    ],
)
def test_a_playlist_names_the_stream_inside(content_type: str, body: bytes, inside: str) -> None:
    answer = describe(Probe(content_type, {}, body), "https://example.test/list")
    assert answer == {
        "ok": False,
        "reason": "This is a playlist file, not a stream.",
        "inside": inside,
    }


def test_a_playlist_without_an_address_names_none() -> None:
    answer = describe(
        Probe("audio/x-mpegurl", {}, b"#EXTM3U\n# nothing here\n"), "https://example.test/a.m3u"
    )
    assert answer == {"ok": False, "reason": "This is a playlist file, not a stream."}


def test_a_plain_http_stream_plays_with_a_warning() -> None:
    answer = describe(Probe("audio/mpeg", {"icy-br": "192"}, MP3), "http://radio.test:8000/;")
    assert answer["ok"] and answer["codec"] == "mp3" and answer["kbps"] == 192
    assert "https://" in answer["warning"]
    assert "warning" not in describe(Probe("audio/mpeg", {}, MP3), "https://radio.test/")


def test_an_hls_stream_is_named_as_such() -> None:
    body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\nsegment1.aac\n"
    answer = describe(
        Probe("application/vnd.apple.mpegurl", {}, body), "https://example.test/x.m3u8"
    )
    assert not answer["ok"] and "HLS" in answer["reason"] and "inside" not in answer


def test_an_old_shoutcast_answer_gets_a_reason_people_can_read() -> None:
    answer = checks(HttpError("http://radio.test:8128/: ICY 200 OK\r\n")).stream(
        "http://radio.test:8128/"
    )
    assert not answer["ok"] and "SHOUTcast" in answer["reason"]


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("audio/x-scpls", b"[playlist]" + b"\n" * 65_526),
        ("audio/x-mpegurl", b"#EXTM3U" + b"\r\n" * 32_000),
    ],
    ids=["pls", "m3u"],
)
def test_a_playlist_full_of_blank_lines_is_read_at_once(content_type: str, body: bytes) -> None:
    started = time.perf_counter()
    answer = describe(Probe(content_type, {}, body), "https://example.test/list")
    assert time.perf_counter() - started < 0.5
    assert answer == {"ok": False, "reason": "This is a playlist file, not a stream."}


def test_a_pls_entry_with_spaces_around_the_equals_sign() -> None:
    body = b"[playlist]\nFile1 = https://a.test/live.mp3\n"
    assert (
        describe(Probe("audio/x-scpls", {}, body), "https://x.test/")["inside"]
        == "https://a.test/live.mp3"
    )


def test_another_servers_banner_comes_back_tidied() -> None:
    banner = HttpError("http://x.test:22/: SSH-2.0-OpenSSH_9.6 " + "a" * 500 + "\r\n")
    reason = checks(banner).stream("http://x.test:22/")["reason"]
    assert "\r" not in reason and "\n" not in reason and len(reason) <= 120


def test_every_check_is_logged_with_its_outcome(caplog: pytest.LogCaptureFixture) -> None:
    def outcomes() -> list[tuple[str, str]]:
        return [
            (r.getMessage(), r.__dict__["fields"]["outcome"])
            for r in caplog.records
            if r.getMessage() in ("stream_checked", "feed_checked")
        ]

    with caplog.at_level(logging.INFO):
        checks().stream("https://x.test/a")
        checks(HttpError("x.test: refused")).stream("https://x.test/a")
        checks(entries=[RawEntry("A", "B", 1)]).feed(TRITON)
        busy = checks(now=[0.0])
        for _ in range(PER_MINUTE):
            busy.stream("https://x.test/a")
        caplog.clear()
        with pytest.raises(Busy):
            busy.stream("https://x.test/a")
        assert outcomes() == [("stream_checked", "busy")]
        caplog.clear()
        checks(HttpError("x.test: refused")).stream("https://x.test/a")
        checks(entries=SourceError("HTTP 404")).feed(TRITON)
        assert outcomes() == [("stream_checked", "refused"), ("feed_checked", "refused")]
    assert all(
        "duration_ms" in r.__dict__["fields"]
        for r in caplog.records
        if r.getMessage().endswith("_checked")
    )


def test_a_playlist_address_keeps_its_letters() -> None:
    body = "[playlist]\nFile1=https://radio.test/ström\n".encode()
    answer = describe(Probe("audio/x-scpls", {}, body), "https://x.test/list.pls")
    assert answer["inside"] == "https://radio.test/ström"


def test_feed_leaves_out_station_ids_and_jingles() -> None:
    entries = [
        RawEntry("FISHER", "Stay", 4000),
        RawEntry("", "A1630 26 Oct 2026 M05 VO", 3000),
        RawEntry("  ", "Jingle", 2000),
        RawEntry("CHRIS LAKE", "Turn Off The Lights", 1000),
    ]
    answer = checks(entries=entries).feed(TRITON)
    assert [song["title"] for song in answer["songs"]] == ["Stay", "Turn Off The Lights"]
    only_ids = checks(entries=[RawEntry("", "A1630 26 Oct 2026 M05 VO", 1)]).feed(TRITON)
    assert not only_ids["ok"] and "no songs" in only_ids["reason"]


def test_a_feed_answer_the_parser_trips_on_is_a_reason(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        answer = checks(entries=ValueError("\u00b2 is no digit")).feed(TRITON)
    assert answer == {"ok": False, "reason": "The feed answered something tunebox cannot read."}
    (record,) = [r for r in caplog.records if r.getMessage() == "feed_checked"]
    assert record.__dict__["fields"]["outcome"] == "error"
    assert record.__dict__["fields"]["error"] == "ValueError"
    # A parser is meant to raise SourceError: anything else is a bug to see.
    (crash,) = [r for r in caplog.records if r.getMessage() == "feed_check_crashed"]
    assert crash.levelno == logging.WARNING and crash.exc_info is not None


def test_the_feed_is_asked_through_the_guard() -> None:
    asked: list[tuple[str, float, int]] = []

    class Guarded:
        def get(self, url: str, deadline_s: float, max_bytes: int) -> bytes:
            asked.append((url, deadline_s, max_bytes))
            return b"<nowplaying-info-list/>"

    assert guarded_feed(Guarded())(TRITON) == []
    ((url, deadline_s, max_bytes),) = asked
    assert url.startswith("https://np.tritondigital.com/public/nowplaying?")
    assert f"numberToFetch={FEED_FETCH}" in url
    assert (deadline_s, max_bytes) == (FEED_DEADLINE_S, FEED_MAX_BYTES)


class RacingClock:
    """Once armed, starts a second check at the moment a check reads the
    clock while taking the slot, and keeps the second's Busy answer."""

    def __init__(self) -> None:
        self.checks: StreamChecks | None = None
        self.armed = False
        self.refused: list[Busy] = []
        self.second: threading.Thread | None = None

    def __call__(self) -> float:
        if self.armed and self.second is None:
            self.second = threading.Thread(target=self._check)
            self.second.start()
            self.second.join(0.2)
        return 100.0

    def _check(self) -> None:
        assert self.checks is not None
        try:
            self.checks.stream("https://x.test/second")
        except Busy as busy:
            self.refused.append(busy)


def test_a_check_refused_as_another_starts_is_told_its_whole_wait() -> None:
    release = threading.Event()

    def slow(_url: str) -> Probe:
        release.wait(5)
        return Probe("audio/mpeg", {}, MP3)

    clock = RacingClock()
    clock.checks = StreamChecks(slow, lambda _source: [], clock=clock)
    clock.armed = True
    first = threading.Thread(target=clock.checks.stream, args=("https://x.test/first",))
    first.start()
    deadline = time.monotonic() + 5
    while (clock.second is None or clock.second.is_alive()) and time.monotonic() < deadline:
        time.sleep(0.02)
    release.set()
    first.join(5)
    assert [(b.running, b.retry_s) for b in clock.refused] == [(True, math.ceil(STREAM_BOUND_S))]


def test_a_check_refused_with_the_minute_full_is_not_told_one_runs() -> None:
    clock = RacingClock()
    clock.checks = StreamChecks(lambda _url: PLAYING, lambda _source: [], clock=clock)
    for _ in range(PER_MINUTE):
        clock.checks.stream("https://x.test/a")
    clock.armed = True
    with pytest.raises(Busy) as first:
        clock.checks.stream("https://x.test/a")
    assert clock.second is not None
    clock.second.join(5)
    assert first.value.running is False
    assert [b.running for b in clock.refused] == [False]
