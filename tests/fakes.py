"""Test doubles and fixtures shared by the test modules."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
