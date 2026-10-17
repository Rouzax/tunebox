import json
from pathlib import Path

import pytest

from tests.fakes import FakeHttp
from tunebox.config import ConfigError, SourceConfig
from tunebox.config import parse as parse_config
from tunebox.sources import build_source
from tunebox.sources.base import SourceError
from tunebox.sources.npo import SITES, NpoSource, parse
from tunebox.tracks import RawEntry

TRACKS = {
    "data": [
        {
            "artist": "Panic! At The Disco",
            "title": "High Hopes",
            "startdatetime": "2026-10-17T14:53:59",
        },
        {"artist": "Winter", "title": "Clock", "startdatetime": "2026-01-15T09:00:00"},
        {"artist": "No", "title": "Time", "startdatetime": "soon"},
    ]
}


def test_parse_reads_amsterdam_local_time_in_summer_and_winter() -> None:
    assert parse(json.dumps(TRACKS).encode()) == [
        # 14:53:59 CEST is 12:53:59 UTC.
        RawEntry("Panic! At The Disco", "High Hopes", 1792241639000),
        # 09:00 CET is 08:00 UTC.
        RawEntry("Winter", "Clock", 1768464000000),
    ]


@pytest.mark.parametrize("raw", [b"<html>", b"{}", b'{"data": {}}', b'{"data": ["x"]}'])
def test_parse_rejects_other_answers(raw: bytes) -> None:
    with pytest.raises(SourceError):
        parse(raw)


def test_source_uses_the_site_and_returns_at_most_count() -> None:
    http = FakeHttp(lambda _url: json.dumps(TRACKS).encode())
    source = build_source(SourceConfig("npo", {"site": "npo3fm"}), http)
    assert isinstance(source, NpoSource)
    assert len(source.fetch(1)) == 1
    assert http.urls == [SITES["npo3fm"]]


def test_config_rejects_an_unknown_site(tmp_path: Path) -> None:
    data = {
        "channels": [
            {
                "id": "npo",
                "name": "NPO",
                "stream_url": "https://example.invalid/a.mp3",
                "source": {"type": "npo", "site": "nporadio9"},
            }
        ]
    }
    with pytest.raises(ConfigError, match=r"channels\[0\]\.source\.site: one of"):
        parse_config(data, tmp_path)


@pytest.mark.live
def test_live_npo_answers() -> None:
    from tunebox import http

    entries = NpoSource("nporadio2", http.get).fetch(3)
    assert entries
    assert all(e.started_at_ms for e in entries)


def test_npo_choices_match_sites() -> None:
    from tunebox.config import SOURCE_CHOICES

    assert set(SOURCE_CHOICES["npo"]["site"]) == set(SITES)
