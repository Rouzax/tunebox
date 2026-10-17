"""Cover decisions: cached answers for request threads, one worker that searches."""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from tunebox.catalogs import Found, SearchError
from tunebox.logs import bound, event
from tunebox.matching import base_title
from tunebox.store import CoverDecision, Outcome, Store
from tunebox.tracks import Track, norm

log = logging.getLogger(__name__)

TTL: dict[Outcome, float] = {
    "cover": 90 * 86400,
    # A track's own cover can be published after its artist photo was settled for.
    "artist": 7 * 86400,
    "miss": 3 * 86400,
    # An upstream failure is not a miss; ask again soon.
    "error": 600,
}
NOW = 0
HISTORY = 1
# Decisions held in memory; older ones are read back from the store on demand.
MEMORY = 2000


@dataclass(frozen=True)
class Cover:
    url: str
    kind: Literal["album", "artist"]


def _is_artist_photo(decision: CoverDecision) -> bool:
    # An error decision keeps the previous URL and match, so the match text,
    # not the outcome, says what kind of image it is.
    return decision.outcome == "artist" or (decision.match or "").startswith("deezer artist:")


def searchable(track: Track) -> bool:
    """Matching compares norm() forms; a name or title with nothing left after
    norm() (another script, punctuation only) can never be matched confidently."""
    return (
        track.kind == "track"
        and bool(norm(" ".join(track.artists)))
        and bool(norm(base_title(track.title)))
    )


class CoverService:
    def __init__(
        self,
        store: Store,
        find: Callable[[Track], Found | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._find = find
        self._clock = clock
        self._lock = threading.Condition()
        self._decisions: dict[str, CoverDecision] = {}
        self._queue: list[tuple[int, int, str]] = []
        self._pending: dict[str, tuple[int, Track]] = {}
        self._searching: set[str] = set()
        self._seq = itertools.count()

    def _known(self, key: str) -> CoverDecision | None:
        """Caller holds the lock."""
        decision = self._decisions.get(key)
        if decision is None:
            decision = self._store.get_cover(key)
            if decision is not None:
                self._remember(decision)
        return decision

    def _remember(self, decision: CoverDecision) -> None:
        """Caller holds the lock."""
        self._decisions.pop(decision.key, None)
        self._decisions[decision.key] = decision
        while len(self._decisions) > MEMORY:
            del self._decisions[next(iter(self._decisions))]

    def _fresh(self, key: str) -> bool:
        """Caller holds the lock."""
        decision = self._known(key)
        return decision is not None and self._clock() - decision.decided_at < TTL[decision.outcome]

    def lookup(self, track: Track) -> Cover | None:
        """The known cover, even past its TTL: a stale cover beats none while
        the worker looks again."""
        with self._lock:
            decision = self._known(track.key)
        if decision is None or decision.url is None:
            return None
        return Cover(decision.url, "artist" if _is_artist_photo(decision) else "album")

    def request(self, track: Track, priority: int) -> None:
        if not searchable(track):
            return
        with self._lock:
            if track.key in self._searching or self._fresh(track.key):
                return
            pending = self._pending.get(track.key)
            if pending is not None and pending[0] <= priority:
                return
            self._pending[track.key] = (priority, track)
            heapq.heappush(self._queue, (priority, next(self._seq), track.key))
            self._lock.notify()

    def run_once(self) -> bool:
        """Searches the most urgent queued track; False when nothing was queued."""
        with self._lock:
            while True:
                if not self._queue:
                    return False
                priority, _, key = heapq.heappop(self._queue)
                pending = self._pending.get(key)
                # A later request with a higher priority left this entry behind.
                if pending is None or pending[0] != priority:
                    continue
                del self._pending[key]
                track = pending[1]
                self._searching.add(key)
                break
        try:
            self._search(track)
        finally:
            with self._lock:
                self._searching.discard(track.key)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.run_once():
                with self._lock:
                    # The timeout lets a stop request through even without a new lookup.
                    self._lock.wait(timeout=1.0)

    def _search(self, track: Track) -> None:
        started = time.monotonic()
        outcome: Outcome = "error"
        found: Found | None = None
        with bound("cover"):
            try:
                found = self._find(track)
                if found is None:
                    outcome = "miss"
                else:
                    outcome = "artist" if found.kind == "artist" else "cover"
            except SearchError:
                pass  # logged by the catalogue as search_failed
            except Exception:  # one broken lookup must not stop the worker
                fields = {"artist": track.artist, "title": track.title}
                log.error("cover_search_crashed", exc_info=True, extra={"fields": fields})
            url, match = (found.url, found.match) if found else (None, None)
            if outcome == "error":
                # A failed re-search must not wipe a cover found earlier; the
                # error outcome only schedules the next attempt.
                with self._lock:
                    previous = self._known(track.key)
                if previous is not None:
                    url, match = previous.url, previous.match
            decision = CoverDecision(track.key, outcome, url, match, self._clock())
            self._store.put_cover(decision)
            with self._lock:
                self._remember(decision)
            event(
                log,
                logging.INFO,
                "cover_found" if found else "cover_miss",
                artist=track.artist,
                title=track.title,
                outcome=outcome,
                match=found.match if found else None,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
