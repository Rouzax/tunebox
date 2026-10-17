"""Triton Digital's public now-playing feed."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ElementTree

from tunebox.http import HttpError, HttpGet
from tunebox.sources.base import SourceError
from tunebox.tracks import RawEntry

FEED = "https://np.tritondigital.com/public/nowplaying"


def parse(body: bytes) -> list[RawEntry]:
    # expat already caps entity expansion and etree never fetches external
    # entities; a now-playing feed has no DTD, so refusing one costs nothing.
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise SourceError("feed declares a DTD")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise SourceError(f"unreadable feed: {error}") from error
    if root.tag != "nowplaying-info-list":
        raise SourceError(f"unexpected feed root {root.tag!r}")
    entries: list[RawEntry] = []
    for info in root.iter("nowplaying-info"):
        props = {p.get("name", ""): (p.text or "").strip() for p in info.iter("property")}
        # cue_time_duration is never read: every mount sends the literal "{length}".
        start = props.get("cue_time_start", "")
        entries.append(
            RawEntry(
                artist=props.get("track_artist_name", ""),
                title=props.get("cue_title", ""),
                started_at_ms=int(start) if start.isdigit() else None,
            )
        )
    return entries


class TritonSource:
    def __init__(self, mount: str, http_get: HttpGet) -> None:
        self._mount = mount
        self._get = http_get

    def fetch(self, count: int) -> list[RawEntry]:
        query = urllib.parse.urlencode(
            {"mountName": self._mount, "numberToFetch": count, "eventType": "track"}
        )
        try:
            body = self._get(f"{FEED}?{query}")
        except HttpError as error:
            raise SourceError(str(error)) from error
        return parse(body)
