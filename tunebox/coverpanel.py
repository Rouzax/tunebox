"""The Covers page's actions on the cover pieces: the list, candidate
searches, saving a picked or uploaded fix, going back to automatic, and the
row images. admin.py parses the request and calls this."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path

from tunebox.blobs import Blobs
from tunebox.coverfiles import MAX_COVER, CoverFiles, sniff_cover
from tunebox.coverfix import CoverFixes, Fix, Row, View, is_photo_key, song_list
from tunebox.covers import CoverService
from tunebox.coversearch import CoverSearch, SearchKind
from tunebox.logs import event
from tunebox.store import HEARD_KEEP_MS, Store

log = logging.getLogger(__name__)

IMAGE_PATH = "/admin/api/covers/image/"


class NotACover(Exception):
    """An upload that is not a jpg, png or webp image within MAX_COVER."""


class StaleCandidate(Exception):
    """A token that is forged, older than an hour, or no longer allowed."""


class CoverPanel:
    def __init__(
        self,
        fixes: CoverFixes,
        service: CoverService,
        store: Store,
        files: CoverFiles,
        blobs: Blobs,
        search: CoverSearch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fixes = fixes
        self._service = service
        self._store = store
        self._files = files
        self._blobs = blobs
        self._search = search
        self._clock = clock

    def listing(self, view: View, query: str, after: tuple[int, str] | None) -> dict[str, object]:
        """Never calls lookup or photo: the list neither touches access
        times nor fills the decision memory, and never asks a catalogue.
        available is False when the heard songs cannot be read (the
        in-memory store keeps none)."""
        now_ms = round(self._clock() * 1000)
        songs = self._store.heard_songs(now_ms - HEARD_KEEP_MS)
        listing = song_list(
            songs or [],
            {fix.key: fix for fix in self._fixes.all()},
            self._file_name,
            self._service.fetch_failure,
            now_ms,
            view,
            query,
            after,
        )
        return {
            "available": songs is not None,
            "totals": dataclasses.asdict(listing.totals),
            "rows": [_row(row) for row in listing.rows],
            # The key may hold colons itself; the reader splits at the first.
            "next": f"{listing.next[0]}:{listing.next[1]}" if listing.next else None,
        }

    def search(
        self, kind: SearchKind, key: str, artist: str, title: str, text: str
    ) -> dict[str, object]:
        """Raises what CoverSearch.search raises."""
        return dataclasses.asdict(self._search.search(kind, key, artist, title, text))

    def pick(self, token: str) -> Fix:
        """Saves the candidate a token names, for the key the token was
        issued for; a replayed token sets the same fix again."""
        pick = self._search.redeem(token)
        if pick is None:
            raise StaleCandidate
        fix = Fix(
            pick.key, pick.link, pick.label, pick.source, pick.artist, pick.title, self._clock()
        )
        self._set(fix)
        return fix

    def upload(self, key: str, artist: str, title: str, body: bytes) -> Fix:
        """A song's cover from the owner's file. Raises ValueError for a
        photo key (a photo is picked from Deezer only), NotACover, or what
        Blobs.save raises when the upload store is full or cannot be
        written."""
        if is_photo_key(key):
            raise ValueError("an upload is for a song key")
        if len(body) > MAX_COVER or sniff_cover(body) is None:
            raise NotACover
        ref, _kind = self._blobs.save(body)
        fix = Fix(key, ref, "Uploaded image", "upload", artist, title, self._clock())
        self._set(fix)
        return fix

    def clear(self, key: str) -> bool:
        """False when the key had no fix."""
        removed = self._fixes.clear(key)
        if removed is None:
            return False
        # After the map change: with the fix still there, the request
        # fix_cleared makes would queue a fetch of it instead of a search.
        self._service.fix_cleared(key, self._store.heard_track(key))
        event(log, logging.INFO, "cover_fix_cleared", key=key)
        return True

    def image(self, name: str) -> Path | None:
        return self._files.file(name)

    def _set(self, fix: Fix) -> None:
        # The map first: fix_set reads the fix it queues from it.
        self._fixes.set(fix)
        self._service.fix_set(fix.key)
        event(log, logging.INFO, "cover_fix_set", key=fix.key, source=fix.source)

    def _file_name(self, link: str) -> str | None:
        # found() stats the file; lookup's touch() would mark it handed out.
        found = self._files.found(link)
        return found[0].name if found else None


def _row(row: Row) -> dict[str, object]:
    fix = row.fix
    return {
        "key": row.key,
        "photo_key": row.photo_key,
        "artist": row.artist,
        "title": row.title,
        "channels": list(row.channels),
        "first_at": row.first_at,
        "last_at": row.last_at,
        "state": row.state,
        "kind": row.kind,
        "reason": row.reason,
        "image": IMAGE_PATH + row.image if row.image else None,
        "fix": None
        if fix is None
        else {"key": fix.key, "label": fix.label, "source": fix.source, "set_at": fix.set_at},
    }
