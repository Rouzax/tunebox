"""Test doubles and fixtures shared by the test modules."""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tunebox.config import ChannelConfig, SourceConfig
from tunebox.tracks import RawEntry, Rules, Track, clean

STATION_ID = r"^[A-Z]\d{4} \d{2} [A-Za-z]{3} \d{4}\b"
PLAYED_BY = r"\s*\(Played by ([^)]*)\)\s*$"
RULES = Rules(station_id=re.compile(STATION_ID), played_by=re.compile(PLAYED_BY))
FIXTURE = Path(__file__).parent / "fixtures" / "tracks-2026-10-16.psv"


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
    delay_s: float = 7.0, pawtunes_delay_s: float = 28.0, history_size: int = 50
) -> ChannelConfig:
    return ChannelConfig(
        id="owr",
        name="One World Radio",
        stream_url="https://example.invalid/stream.aac",
        default_image=None,
        source=SourceConfig("triton", {"mount": "OWR_INTERNATIONAL"}),
        rules=RULES,
        delay_s=delay_s,
        pawtunes_delay_s=pawtunes_delay_s,
        poll_interval_s=12.0,
        history_size=history_size,
    )
