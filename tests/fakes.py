"""Test doubles and fixtures shared by the test modules."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tunebox.blobs import Blobs
from tunebox.config import ChannelConfig, SourceConfig, StreamConfig
from tunebox.http import HttpError
from tunebox.netguard import Probe
from tunebox.sources.base import SourceError
from tunebox.streamcheck import StreamChecks
from tunebox.tracks import RawEntry, Rules, Track, clean

STATION_ID = r"^[A-Z]\d{4} \d{2} [A-Za-z]{3} \d{4}\b"
PLAYED_BY = r"\s*\(Played by ([^)]*)\)\s*$"
RULES = Rules(station_id=re.compile(STATION_ID), played_by=re.compile(PLAYED_BY))
FIXTURE = Path(__file__).parent / "fixtures" / "tracks-2026-10-16.psv"
COVER_JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


def fetch_cover(_url: str) -> bytes:
    """Stands in for the cover download: every link answers a small JPEG."""
    return COVER_JPG


@dataclass(frozen=True)
class FixtureRow:
    mount: str
    artist: str
    title: str
    outcome: str
    match: str


def fixture_rows() -> list[FixtureRow]:
    rows: list[FixtureRow] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        mount, artist, title, outcome, match = (part.strip() for part in line.split("|"))
        rows.append(FixtureRow(mount, artist, title, outcome, match))
    return rows


def blob_store(directory: Path, clock: Callable[[], float] = time.time) -> Blobs:
    """Blobs on a disk that always has room, so a test does not depend on the
    free space of the machine running it."""
    return Blobs(directory, clock=clock, free_space=lambda: 1 << 40)


def make_track(artist: str = "FISHER", title: str = "Stay", started_at: int = 1_000_000) -> Track:
    return clean(RawEntry(artist, title, started_at), RULES, started_at)


class FakeHttp:
    """Stands in for tunebox.http.get; records every URL asked for."""

    def __init__(self, respond: Callable[[str], bytes]) -> None:
        self.urls: list[str] = []
        self._respond = respond

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self._respond(url)


class FakeSource:
    """Answers each fetch with the next queued response; an empty list when none are left."""

    def __init__(self) -> None:
        self.responses: list[list[RawEntry] | Exception] = []
        self.counts: list[int] = []

    def fetch(self, count: int) -> list[RawEntry]:
        self.counts.append(count)
        response = self.responses.pop(0) if self.responses else []
        if isinstance(response, Exception):
            raise response
        return response


class NoJitter(random.Random):
    """Always the lowest value uniform() could return, so delays are exact."""

    def uniform(self, a: float, b: float) -> float:
        return min(a, b)


def channel_config(
    delay_s: float = 7.0,
    pawtunes_delay_s: float = 28.0,
    history_size: int = 50,
    always: bool = False,
    group: str | None = None,
    logo: str | None = None,
    default_image: str | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id="owr",
        name="One World Radio",
        streams=(StreamConfig("Default", "https://example.invalid/stream.aac", None, None),),
        default_image=default_image,
        source=SourceConfig("triton", {"mount": "OWR_INTERNATIONAL"}),
        rules=RULES,
        delay_s=delay_s,
        pawtunes_delay_s=pawtunes_delay_s,
        poll_interval_s=12.0,
        history_size=history_size,
        group=group,
        logo=logo,
        always=always,
    )


OWR_STATION_ID = r"^(?:[A-Z]\d{4}(?: CH\d{2})? \d{2} [A-Za-z]{3} \d{4}\b|RECAP \d+-\d+$)"
OWR_BADGE = r"^(#\d+)\s+|\s*\[((?:Top 30|Last Week)[^\]]*)\]\s*$"
OWR_RULES = Rules(
    station_id=re.compile(OWR_STATION_ID),
    played_by=re.compile(PLAYED_BY),
    badge=re.compile(OWR_BADGE),
)
OWR_TOP30 = Path(__file__).parent / "fixtures" / "owr-top30-2026-10-24.psv"


def owr_top30() -> list[RawEntry]:
    """The OWR feed during the Top 30 of 2026-10-24, newest first."""
    rows: list[RawEntry] = []
    for line in OWR_TOP30.read_text(encoding="utf-8").splitlines():
        artist, title = (part.strip() for part in line.split("|"))
        rows.append(RawEntry(artist, title, 1))
    return rows


def offline_checks() -> StreamChecks:
    """The panel's stream and feed checks with no network: every one fails."""

    def probe(url: str) -> Probe:
        raise HttpError(f"{url}: no network in tests")

    def feed(source: SourceConfig) -> list[RawEntry]:
        raise SourceError(f"{source.type}: no network in tests")

    return StreamChecks(probe, feed)
