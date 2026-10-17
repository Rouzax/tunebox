import urllib.parse

import pytest

from tests.fakes import FakeHttp
from tunebox.config import SourceConfig
from tunebox.http import HttpError
from tunebox.sources import build_source
from tunebox.sources.base import SourceError
from tunebox.sources.triton import TritonSource, parse
from tunebox.tracks import RawEntry

FEED = b"""<?xml version="1.0" encoding="UTF-8"?><nowplaying-info-list>
<nowplaying-info mountName="OWR_ANTHEMS" timestamp="1792218573" type="track">
<property name="cue_time_duration"><![CDATA[{length}]]></property>
<property name="cue_time_start"><![CDATA[1792218573774]]></property>
<property name="cue_title"><![CDATA[The Veldt]]></property>
<property name="track_artist_name"><![CDATA[DEADMAU5]]></property></nowplaying-info>
<nowplaying-info mountName="OWR_ANTHEMS" timestamp="1792218287" type="track">
<property name="cue_time_start"><![CDATA[not a number]]></property>
<property name="cue_title"><![CDATA[ E1700 16 Oct 2026 M19 VO ]]></property></nowplaying-info>
</nowplaying-info-list>"""


def test_parse_reads_entries_newest_first() -> None:
    assert parse(FEED) == [
        RawEntry("DEADMAU5", "The Veldt", 1792218573774),
        RawEntry("", "E1700 16 Oct 2026 M19 VO", None),
    ]


def test_parse_empty_list() -> None:
    assert parse(b"<nowplaying-info-list></nowplaying-info-list>") == []


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"not xml",
        b"<other></other>",
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><nowplaying-info-list></nowplaying-info-list>',
    ],
)
def test_parse_rejects_garbage(body: bytes) -> None:
    with pytest.raises(SourceError):
        parse(body)


def test_fetch_asks_for_the_mount_and_count() -> None:
    http = FakeHttp(lambda _url: FEED)
    entries = TritonSource("OWR_ANTHEMS", http).fetch(5)
    assert len(entries) == 2
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(http.urls[0]).query)
    assert query == {"mountName": ["OWR_ANTHEMS"], "numberToFetch": ["5"], "eventType": ["track"]}


def test_fetch_turns_http_errors_into_source_errors() -> None:
    def fail(url: str) -> bytes:
        raise HttpError("timed out")

    with pytest.raises(SourceError, match="timed out"):
        TritonSource("OWR_ANTHEMS", FakeHttp(fail)).fetch(5)


def test_build_source_for_triton() -> None:
    source = build_source(
        SourceConfig("triton", {"mount": "OWR_ANTHEMS"}), FakeHttp(lambda _url: FEED)
    )
    assert isinstance(source, TritonSource)


@pytest.mark.live
def test_live_feed_answers() -> None:
    from tunebox import http

    entries = TritonSource("OWR_INTERNATIONAL", http.get).fetch(3)
    assert entries
    assert all(e.started_at_ms for e in entries)
