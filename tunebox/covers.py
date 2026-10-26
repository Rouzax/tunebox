"""Cover decisions: cached answers for request threads, one worker that
searches the catalogues and downloads what they find into CoverFiles."""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from tunebox.catalogs import Found, SearchError
from tunebox.coverfiles import MAX_COVER, CleanReport, CoverFiles, SaveResult, name_for, sized
from tunebox.disk import has_room
from tunebox.http import HttpError, HttpGet
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
# An artist photo only adds a second side to a cover: it waits for every song.
PHOTO = 2
# Decisions held in memory; older ones are read back from the store on demand.
MEMORY = 2000
# A song still played is searched again at least every 90 days, so a decision
# this old belongs to a song nobody has heard for about nine months.
PRUNE_AFTER_S = 365 * 86400
# Pause after a failed download of one cover before it is tried again.
RETRY_S = 600
DISK_WARN_S = 3600
HOUSEKEEP_S = 86400
# Consecutive failed downloads of one cover before they are logged as WARNING.
LOUD_AFTER = 3
JobKind = Literal["search", "download"]


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
        files: CoverFiles,
        fetch: HttpGet,
        clock: Callable[[], float] = time.time,
        find_photo: Callable[[Track], Found | None] | None = None,
    ) -> None:
        self._store = store
        self._find = find
        self._find_photo = find_photo
        self._files = files
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Condition()
        self._decisions: dict[str, CoverDecision] = {}
        self._queue: list[tuple[int, int, str]] = []
        self._pending: dict[str, tuple[int, Track, JobKind]] = {}
        self._searching: set[str] = set()
        self._seq = itertools.count()
        # key -> (time of the last failed download, consecutive failures)
        self._failed: dict[str, tuple[float, int]] = {}
        self._disk_warned = -DISK_WARN_S

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

    def _needs_download(self, key: str) -> bool:
        """Caller holds the lock."""
        decision = self._known(key)
        if decision is None or decision.url is None:
            return False
        if self._files.path_for(decision.url) is not None:
            return False
        failed = self._failed.get(key)
        return failed is None or self._clock() - failed[0] >= RETRY_S

    def lookup(self, track: Track) -> Cover | None:
        """The local cover, even past its decision's TTL (a stale cover beats
        none while the worker looks again), or None while it is not
        downloaded: the player then shows the station's own images. The next
        request() queues the missing download."""
        with self._lock:
            decision = self._known(track.key)
        if decision is None or decision.url is None:
            return None
        found = self._files.found(decision.url)
        if found is None:
            return None
        path, address = found
        self._files.touch(path)
        return Cover(address, "artist" if _is_artist_photo(decision) else "album")

    def photo(self, track: Track) -> str | None:
        """The local address of the artists' photo, the other side of a
        song's own cover; None while it is not downloaded or not found."""
        with self._lock:
            decision = self._known(track.photo_key)
        if decision is None or decision.url is None:
            return None
        found = self._files.found(decision.url)
        if found is None:
            return None
        path, address = found
        self._files.touch(path)
        return address

    def remote_link(self, track: Track) -> str | None:
        """The catalogue link, for PawTunes, which counts a changed artwork
        link as a new track."""
        with self._lock:
            decision = self._known(track.key)
        return decision.url if decision else None

    def request(self, track: Track, priority: int) -> None:
        """Queues what the track still needs. A song about to play whose own
        cover was found also queues its artists' photo, after every song."""
        if not searchable(track):
            return
        with self._lock:
            self._queue_job(track, priority)
            if self._find_photo is not None and priority == NOW and self._has_own_cover(track):
                # The photo job runs as the song under the photo's key, so
                # search, download and failure handling stay one path.
                self._queue_job(replace(track, key=track.photo_key), PHOTO)

    def _has_own_cover(self, track: Track) -> bool:
        """Caller holds the lock. A song whose only image is its artists'
        photo has no other side to show."""
        decision = self._known(track.key)
        return decision is not None and decision.url is not None and not _is_artist_photo(decision)

    def _queue_job(self, track: Track, priority: int) -> None:
        """Caller holds the lock."""
        if track.key in self._searching:
            return
        kind: JobKind
        if not self._fresh(track.key):
            kind = "search"
        elif self._needs_download(track.key):
            kind = "download"
        else:
            return
        pending = self._pending.get(track.key)
        if pending is not None and pending[0] <= priority:
            return
        self._pending[track.key] = (priority, track, kind)
        heapq.heappush(self._queue, (priority, next(self._seq), track.key))
        self._lock.notify()

    def run_once(self) -> bool:
        """Runs the most urgent queued job; False when nothing was queued."""
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
                _, track, kind = pending
                self._searching.add(key)
                decision = self._known(key) if kind == "download" else None
                break
        try:
            if decision is not None:
                with bound("cover"):
                    self._download(track, decision)
            elif kind == "search":
                self._search(track)
        finally:
            with self._lock:
                self._searching.discard(track.key)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        self._housekeep_safely()
        last = time.monotonic()
        while not stop.is_set():
            if self.run_once():
                continue
            if time.monotonic() - last >= HOUSEKEEP_S:
                self._housekeep_safely()
                last = time.monotonic()
                continue
            with self._lock:
                # The timeout lets a stop request through even without a new lookup.
                self._lock.wait(timeout=1.0)

    def _housekeep_safely(self) -> None:
        try:
            self.housekeep()
        except Exception:  # cleanup must never stop the worker
            log.error("cover_housekeeping_crashed", exc_info=True)

    def housekeep(self) -> None:
        """Forgets decisions for songs long unplayed, then removes the files
        no remaining decision names and keeps the folder under its cap."""
        removed = self._store.prune_covers(self._clock() - PRUNE_AFTER_S)
        with self._lock:
            for key in removed:
                self._decisions.pop(key, None)
                self._failed.pop(key, None)
        links = self._store.cover_links()
        # Unknown links must not read as "none": only the size cap then applies.
        report = self._files.clean(None if links is None else {name_for(link) for link in links})
        _log_cleaned(report, len(removed), "daily", links is not None)

    def _search(self, track: Track) -> None:
        started = time.monotonic()
        outcome: Outcome = "error"
        found: Found | None = None
        with bound("cover"):
            find = self._find
            if self._find_photo is not None and track.key == track.photo_key:
                find = self._find_photo
            try:
                found = find(track)
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
            replaced = self._working_cover_replaced_by(track, found) if found else None
            # With a working cover to replace, switch only once the new image is
            # on disk; until then the old one keeps serving, with the error pause
            # before the next search.
            if replaced is not None and not self._download(track, decision, replaced):
                outcome = "error"
                decision = CoverDecision(
                    track.key, outcome, replaced.url, replaced.match, self._clock()
                )
            self._settle(decision)
            event(
                log,
                logging.INFO,
                "cover_found" if found else "cover_miss",
                artist=track.artist,
                title=track.title,
                outcome=outcome,
                match=found.match if found else None,
                duration_ms=_ms_since(started),
            )
            # Also when the link is unchanged: save() keeps identical bytes
            # and replaces a cover the catalogue has updated.
            if replaced is None and url is not None and outcome != "error":
                self._download(track, decision)

    def _settle(self, decision: CoverDecision) -> None:
        self._store.put_cover(decision)
        with self._lock:
            self._remember(decision)

    def _working_cover_replaced_by(self, track: Track, found: Found) -> CoverDecision | None:
        """The earlier decision when its file is on disk and the search found
        another link, i.e. a cover that must not be lost to a failed download."""
        with self._lock:
            previous = self._known(track.key)
        if previous is None or previous.url is None or previous.url == found.url:
            return None
        return previous if self._files.path_for(previous.url) is not None else None

    def _download(
        self, track: Track, decision: CoverDecision, keep: CoverDecision | None = None
    ) -> bool:
        """True when the image is on disk. With keep, a failure leaves the
        decision to the caller, which keeps serving keep's cover."""
        link = decision.url
        assert link is not None
        keeping = keep is not None
        started = time.monotonic()
        host = ""
        try:
            host = urllib.parse.urlsplit(link).hostname or ""
            if not has_room(self._files.free(), MAX_COVER):
                self._disk_low(track.key, host, started)
                return False
            body = self._fetch(sized(link))
            result = self._files.save(link, body)
        except HttpError as error:
            if error.status in (404, 410):
                self._dead(track, decision, "gone", host, started, keeping)
            elif error.too_large:
                self._dead(track, decision, SaveResult.TOO_LARGE.value, host, started, keeping)
            else:
                self._failure(track.key, "unreachable", host, started, error.status)
            return False
        except Exception:  # one broken download must not stop the worker
            log.error("cover_download_crashed", exc_info=True, extra={"fields": {"host": host}})
            # Sets the retry pause only; the crash is already logged.
            self._failure(track.key, "crashed", host, started, quiet=True, counted=False)
            return False
        if result in (SaveResult.SAVED, SaveResult.UNCHANGED):
            with self._lock:
                self._failed.pop(track.key, None)
            if result is SaveResult.SAVED and self._files.over_cap():
                # Only the size cap: the links are not looked up on this path.
                _log_cleaned(self._files.clean(None), 0, "cap", None)
            event(
                log,
                logging.DEBUG,
                "cover_downloaded",
                host=host,
                bytes=len(body),
                duration_ms=_ms_since(started),
            )
            return True
        if result in (SaveResult.NOT_IMAGE, SaveResult.TOO_LARGE):
            self._dead(track, decision, result.value, host, started, keeping)
        elif result is SaveResult.DISK_LOW:
            self._disk_low(track.key, host, started)
        else:
            self._failure(track.key, "unwritable", host, started)
        return False

    def _disk_low(self, key: str, host: str, started: float) -> None:
        """Not the cover's fault: it sets the retry pause but is not a failure to count."""
        self._failure(key, "disk_low", host, started, quiet=True, counted=False)
        now = self._clock()
        loud = now - self._disk_warned >= DISK_WARN_S
        if loud:
            self._disk_warned = now
        event(
            log,
            logging.WARNING if loud else logging.DEBUG,
            "cover_disk_low",
            free_bytes=self._files.free(),
        )

    def _dead(
        self,
        track: Track,
        decision: CoverDecision,
        reason: str,
        host: str,
        started: float,
        keep: bool,
    ) -> None:
        """A link that will not give an image: the error outcome, link kept,
        searches again after its TTL, and a file saved earlier stays in use.
        With keep the caller stores the decision."""
        if not keep:
            self._settle(
                CoverDecision(track.key, "error", decision.url, decision.match, self._clock())
            )
        self._failure(track.key, reason, host, started)

    def _failure(
        self,
        key: str,
        reason: str,
        host: str,
        started: float,
        status: int | None = None,
        quiet: bool = False,
        counted: bool = True,
    ) -> None:
        with self._lock:
            count = self._failed.pop(key, (0.0, 0))[1] + counted
            self._failed[key] = (self._clock(), count)
            while len(self._failed) > MEMORY:
                del self._failed[next(iter(self._failed))]
        if quiet:
            return
        event(
            log,
            logging.WARNING if count >= LOUD_AFTER else logging.INFO,
            "cover_download_failed",
            host=host,
            reason=reason,
            status=status,
            duration_ms=_ms_since(started),
        )


def _log_cleaned(
    report: CleanReport, decisions_removed: int, trigger: str, links_known: bool | None
) -> None:
    """links_known is None when this clean did not look the links up."""
    event(
        log,
        logging.INFO,
        "cover_cache_cleaned",
        trigger=trigger,
        decisions_removed=decisions_removed,
        links_known=links_known,
        files_removed=report.files_removed,
        bytes_freed=report.bytes_freed,
        files=report.files,
        bytes=report.bytes,
    )


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
