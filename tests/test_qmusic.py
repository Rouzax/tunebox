import json
import urllib.parse

import pytest

from tests.fakes import FakeHttp
from tunebox.config import SourceConfig
from tunebox.http import HttpError
from tunebox.sources import build_source
from tunebox.sources.base import SourceError
from tunebox.sources.qmusic import QmusicSource, parse
from tunebox.tracks import RawEntry

PLAYS = {
    "played_tracks": [
        {
            "title": "Take Me Back ",
            "artist": {"name": "KYGO & MAX MCNOWN", "id": 8825},
            "played_at": "2026-10-17T14:53:19+02:00",
        },
        {"title": "No time", "artist": {"name": "X"}, "played_at": None},
        {
            "title": "This Girl",
            "artist": {"name": "Kungs"},
            "played_at": "2026-10-17T14:48:15+02:00",
        },
    ]
}


def body(data: object) -> bytes:
    return json.dumps(data).encode()


def test_parse_reads_plays_newest_first_and_skips_untimed() -> None:
    assert parse(body(PLAYS)) == [
        RawEntry("KYGO & MAX MCNOWN", "Take Me Back", 1792241599000),
        RawEntry("Kungs", "This Girl", 1792241295000),
    ]


def test_parse_sorts_by_time() -> None:
    plays = {"played_tracks": list(reversed(PLAYS["played_tracks"]))}
    assert [e.title for e in parse(body(plays))] == ["Take Me Back", "This Girl"]


def test_parse_tolerates_a_missing_artist() -> None:
    plays = {"played_tracks": [{"title": "Jingle", "played_at": "2026-10-17T14:00:00+02:00"}]}
    assert parse(body(plays)) == [RawEntry("", "Jingle", 1792238400000)]


@pytest.mark.parametrize(
    "raw",
    [b"<html>", b"\xff\xfe", body([]), body({"played_tracks": "x"}), body({"played_tracks": [1]})],
)
def test_parse_rejects_other_answers(raw: bytes) -> None:
    with pytest.raises(SourceError):
        parse(raw)


def test_source_asks_for_the_station_and_caps_the_count() -> None:
    http = FakeHttp(lambda _url: body(PLAYS))
    source = build_source(SourceConfig("qmusic", {"station": "qnl_top40"}), http)
    assert isinstance(source, QmusicSource)
    source.fetch(80)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(http.urls[0]).query)
    assert query == {"limit": ["50"], "_station_id": ["qnl_top40"]}


def test_source_turns_http_errors_into_source_errors() -> None:
    def fail(_url: str) -> bytes:
        raise HttpError("down")

    with pytest.raises(SourceError, match="down"):
        QmusicSource("qmusic_nl", fail).fetch(5)


@pytest.mark.live
def test_live_qmusic_answers() -> None:
    from tunebox import http

    entries = QmusicSource("qmusic_nl", http.get).fetch(3)
    assert entries
    assert all(e.started_at_ms for e in entries)
