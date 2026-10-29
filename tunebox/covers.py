"""Cover decisions: cached answers for request threads, one worker that
searches the catalogues and downloads what they find into CoverFiles."""

from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from tunebox.catalogs import Found, SearchError
from tunebox.config import BLOB_REF
from tunebox.coverfiles import MAX_COVER, CleanReport, CoverFiles, SaveResult, name_for, sized
from tunebox.coverfix import FETCH_LOST, Fix, FixReader, NoFixes, Shown, allowed_image, effective
from tunebox.disk import has_room
from tunebox.http import HttpError, HttpGet
from tunebox.logs import bound, event
from tunebox.matching import searchable
from tunebox.store import HEARD_KEEP_MS, CoverDecision, Outcome, Store
from tunebox.tracks import Track

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
JobKind = Literal["search", "download", "fetch"]
# A failure is held under the song or photo key for an automatic download,
# and under (key, fix value) for a fix's fetch.
FailKey = str | tuple[str, str]


@dataclass(frozen=True)
class Cover:
    url: str
    kind: Literal["album", "artist"]


class CoverService:
    def __init__(
        self,
        store: Store,
        find: Callable[[Track], Found | None],
        files: CoverFiles,
        fetch: HttpGet,
        clock: Callable[[], float] = time.time,
        find_photo: Callable[[Track], Found | None] | None = None,
        fixes: FixReader | None = None,
        blob_path: Callable[[str], Path | None] | None = None,
    ) -> None:
        self._store = store
        self._find = find
        self._find_photo = find_photo
        self._files = files
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Condition()
        self._fixes = fixes or NoFixes()
        self._blob_path = blob_path
        self._decisions: dict[str, CoverDecision] = {}
        # A fix's fetch has its own queue slot (the bool), so a pending
        # search of the same key never swallows it.
        self._queue: list[tuple[int, int, str, bool]] = []
        self._pending: dict[tuple[str, bool], tuple[int, Track | None, JobKind]] = {}
        self._searching: set[str] = set()
        # (key, fix value) of the fetches running now.
        self._fetching: set[tuple[str, str]] = set()
        self._seq = itertools.count()
        # The time of the last failure, consecutive failures and the reason.
        # A fix's failure is held per value, so a fetch of a value the owner
        # has since replaced can neither pause nor label the new one.
        self._failed: dict[FailKey, tuple[float, int, str]] = {}
        # Fixes whose refusal or lost blob was logged; once per fix value.
        self._told: set[tuple[str, str]] = set()
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

    def _shown(self, track: Track) -> list[Shown | None]:
        """Caller holds the lock. What the song shows, then what it shows
        while a fixed image is not in the cover store yet: without the
        song's own fix, then without the photo fix either."""
        fix = self._fixes.get(track.key)
        photo_fix = self._fixes.get(track.photo_key)
        automatic = self._known(track.key)
        return [
            effective(fix, automatic, photo_fix),
            effective(None, automatic, photo_fix),
            effective(None, automatic, None),
        ]

    def _local(self, choices: list[Shown | None]) -> tuple[str, Shown] | None:
        """The local address of the first choice that can be shown, and that
        choice. A fixed image not yet downloaded passes to the next choice;
        "no photo" and an automatic image not downloaded end the search."""
        for shown in choices:
            if shown is None or shown.url is None:
                break
            found = self._files.found(shown.url)
            if found is not None:
                path, address = found
                self._files.touch(path)
                return address, shown
            if shown.fix is None:
                break
        return None

    def lookup(self, track: Track) -> Cover | None:
        """The local cover, even past its decision's TTL (a stale cover beats
        none while the worker looks again), or None while it is not
        downloaded: the player then shows the station's own images. A fix
        wins over the automatic answer once its image is in the cover store.
        The next request() queues what is missing."""
        with self._lock:
            choices = self._shown(track)
        local = self._local(choices)
        return Cover(local[0], local[1].kind) if local is not None else None

    def photo(self, track: Track) -> str | None:
        """The local address of the artists' photo, the other side of a
        song's own cover; None while it is not downloaded or not found."""
        with self._lock:
            fix = self._fixes.get(track.photo_key)
            automatic = self._known(track.photo_key)
        local = self._local([effective(fix, automatic, None), effective(None, automatic, None)])
        return local[0] if local is not None else None

    def remote_link(self, track: Track) -> str | None:
        """The catalogue link, for PawTunes, which counts a changed artwork
        link as a new track. An upload has no public link and is omitted."""
        with self._lock:
            shown = self._shown(track)[0]
        if shown is None or shown.url is None or BLOB_REF.fullmatch(shown.url):
            return None
        return shown.url

    def request(self, track: Track, priority: int) -> None:
        """Queues what the track still needs: the fetch of the fixes it may
        show (also for a song no search could match), else a search or
        download. A song about to play whose own cover was found also
        queues its artists' photo, after every song."""
        with self._lock:
            for fix in (self._fixes.get(track.key), self._fixes.get(track.photo_key)):
                if fix is not None:
                    self._queue_fetch(fix, priority)
            if not searchable(track):
                return
            self._queue_job(track, priority)
            if self._find_photo is not None and priority == NOW and self._has_own_cover(track):
                # The photo job runs as the song under the photo's key, so
                # search, download and failure handling stay one path.
                self._queue_job(replace(track, key=track.photo_key), PHOTO)

    def _has_own_cover(self, track: Track) -> bool:
        """Caller holds the lock. A song whose only image is its artists'
        photo has no other side to show."""
        shown = self._shown(track)[0]
        return shown is not None and shown.url is not None and shown.kind == "album"

    def _queue_job(self, track: Track, priority: int) -> None:
        """Caller holds the lock. A fixed key is never searched."""
        fix = self._fixes.get(track.key)
        if fix is not None:
            self._queue_fetch(fix, priority)
            return
        if track.key in self._searching:
            return
        kind: JobKind
        if not self._fresh(track.key):
            kind = "search"
        elif self._needs_download(track.key):
            kind = "download"
        else:
            return
        self._push(track.key, priority, kind, track)

    def _queue_fetch(self, fix: Fix, priority: int) -> None:
        """Caller holds the lock. Only a fix whose image is missing, and not
        within the pause after a failed fetch."""
        if fix.value is None or (fix.key, fix.value) in self._fetching:
            return
        if self._files.path_for(fix.value) is not None:
            return
        failed = self._failed.get((fix.key, fix.value))
        if failed is not None and self._clock() - failed[0] < RETRY_S:
            return
        self._push(fix.key, priority, "fetch", None)

    def _push(self, key: str, priority: int, kind: JobKind, track: Track | None) -> None:
        """Caller holds the lock."""
        slot = (key, kind == "fetch")
        pending = self._pending.get(slot)
        if pending is not None and pending[0] <= priority:
            return
        self._pending[slot] = (priority, track, kind)
        heapq.heappush(self._queue, (priority, next(self._seq), key, kind == "fetch"))
        self._lock.notify()

    def run_once(self) -> bool:
        """Runs the most urgent queued job; False when nothing was queued."""
        fix: Fix | None = None
        running: tuple[str, str] | None = None
        decision: CoverDecision | None = None
        with self._lock:
            while True:
                if not self._queue:
                    return False
                priority, _, key, fetching = heapq.heappop(self._queue)
                slot = (key, fetching)
                pending = self._pending.get(slot)
                # A later request with a higher priority left this entry behind.
                if pending is None or pending[0] != priority:
                    continue
                del self._pending[slot]
                _, track, kind = pending
                if fetching:
                    fix = self._fixes.get(key)
                    if fix is not None and fix.value is not None:
                        running = (key, fix.value)
                        self._fetching.add(running)
                else:
                    self._searching.add(key)
                    decision = self._known(key) if kind == "download" else None
                break
        try:
            if fix is not None:
                with bound("cover"):
                    self._fetch_fix(fix)
            elif track is None:
                pass  # a fetch whose fix was cleared while it waited
            elif decision is not None:
                with bound("cover"):
                    self._download(track, decision)
            elif kind == "search":
                self._search(track)
        finally:
            with self._lock:
                if running is not None:
                    self._fetching.discard(running)
                elif not fetching:
                    self._searching.discard(key)
        return True

    def fix_set(self, key: str) -> None:
        """A fix just saved (or picked again): fetched at once, whatever an
        earlier failure paused, also while a fetch of an earlier value runs."""
        with self._lock:
            fix = self._fixes.get(key)
            if fix is not None and fix.value is not None:
                self._failed.pop((key, fix.value), None)
                self._told.discard((key, fix.value))
                self._queue_fetch(fix, NOW)

    def fix_cleared(self, key: str, track: Track | None) -> None:
        """Back to automatic: the automatic answer is forgotten, so the key
        searches again; with a heard song at once, behind the songs
        playing. track is that song under this key."""
        self._store.delete_cover(key)
        with self._lock:
            self._decisions.pop(key, None)
            # The search's pause is held under the key, each fix fetch's
            # under (key, value).
            for failed in [k for k in self._failed if k == key or k[0] == key]:
                del self._failed[failed]
            self._told = {told for told in self._told if told[0] != key}
        if track is not None:
            self.request(track, HISTORY)

    def fetch_failure(self, key: str) -> str | None:
        """Why the last fetch of the key's current fix failed, while its
        pause holds the entry."""
        with self._lock:
            fix = self._fixes.get(key)
            if fix is None or fix.value is None:
                return None
            failed = self._failed.get((key, fix.value))
        return failed[2] if failed is not None else None

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
        """Forgets decisions for songs long unplayed and heard songs past the
        panel's window, then removes the files no decision and no fix names
        and keeps the folder under its cap."""
        removed = self._store.prune_covers(self._clock() - PRUNE_AFTER_S)
        heard = self._store.prune_heard(round(self._clock() * 1000) - HEARD_KEEP_MS)
        with self._lock:
            for key in removed:
                self._decisions.pop(key, None)
                self._failed.pop(key, None)
        links = self._store.cover_links()
        fixed = self._fixes.values()
        # Unknown links or fixes must not read as "none": only the size cap
        # then applies. The cap can still remove a fix's image, which its
        # next request fetches again.
        keep = None if links is None or fixed is None else {name_for(i) for i in links | fixed}
        report = self._files.clean(keep)
        _log_cleaned(report, len(removed), "daily", keep is not None, heard_removed=heard)

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
            reason = _http_reason(error)
            if reason == "unreachable":
                self._failure(track.key, reason, host, started, error.status)
            else:
                self._dead(track, decision, reason, host, started, keeping)
            return False
        except Exception:  # one broken download must not stop the worker
            log.error("cover_download_crashed", exc_info=True, extra={"fields": {"host": host}})
            # Sets the retry pause only; the crash is already logged.
            self._failure(track.key, "crashed", host, started, quiet=True, counted=False)
            return False
        if result in (SaveResult.SAVED, SaveResult.UNCHANGED):
            self._done(track.key, result, host, len(body), started)
            return True
        if result in (SaveResult.NOT_IMAGE, SaveResult.TOO_LARGE):
            self._dead(track, decision, result.value, host, started, keeping)
        elif result is SaveResult.DISK_LOW:
            self._disk_low(track.key, host, started)
        else:
            self._failure(track.key, "unwritable", host, started)
        return False

    def _done(self, key: FailKey, result: SaveResult, host: str, size: int, started: float) -> None:
        with self._lock:
            self._failed.pop(key, None)
        if result is SaveResult.SAVED and self._files.over_cap():
            # Only the size cap: the links are not looked up on this path.
            _log_cleaned(self._files.clean(None), 0, "cap", None)
        event(
            log,
            logging.DEBUG,
            "cover_downloaded",
            host=host,
            bytes=size,
            duration_ms=_ms_since(started),
        )

    def _fetch_fix(self, fix: Fix) -> None:
        """A fix's image into the cover store: an upload is copied from its
        blob (no HTTP), a catalogue link fetched like any cover once the
        host rule passes again. A failure only pauses the next try; a fix
        never becomes a decision in the covers table."""
        value = fix.value
        if value is None or self._files.path_for(value) is not None:
            return
        fail = (fix.key, value)
        started = time.monotonic()
        host = ""
        try:
            if BLOB_REF.fullmatch(value):
                body = self._blob_body(fail, started)
                if body is None:
                    return
            else:
                if not allowed_image(value):
                    self._failure(fail, "refused", host, started, quiet=True, counted=False)
                    self._tell_once(fail, logging.WARNING, "cover_fix_refused", why="host")
                    return
                host = urllib.parse.urlsplit(value).hostname or ""
                if not has_room(self._files.free(), MAX_COVER):
                    self._disk_low(fail, host, started)
                    return
                body = self._fetch(sized(value))
            result = self._files.save(value, body)
        except HttpError as error:
            self._failure(fail, _http_reason(error), host, started, error.status)
            return
        except Exception:  # one broken fetch must not stop the worker
            log.error("cover_download_crashed", exc_info=True, extra={"fields": {"host": host}})
            self._failure(fail, "crashed", host, started, quiet=True, counted=False)
            return
        if result in (SaveResult.SAVED, SaveResult.UNCHANGED):
            self._done(fail, result, host, len(body), started)
        elif result is SaveResult.DISK_LOW:
            self._disk_low(fail, host, started)
        elif result in (SaveResult.NOT_IMAGE, SaveResult.TOO_LARGE):
            self._failure(fail, result.value, host, started)
        else:
            self._failure(fail, "unwritable", host, started)

    def _blob_body(self, fix: tuple[str, str], started: float) -> bytes | None:
        """The upload's bytes; None when the blob is gone, which is logged
        once and reads as FETCH_LOST until the owner picks again."""
        path = self._blob_path(fix[1]) if self._blob_path is not None else None
        body: bytes | None = None
        if path is not None:
            with contextlib.suppress(OSError):
                body = path.read_bytes()
        if body is None:
            self._failure(fix, FETCH_LOST, "", started, quiet=True, counted=False)
            self._tell_once(fix, logging.ERROR, "cover_fix_blob_missing")
        return body

    def _tell_once(self, fix: tuple[str, str], level: int, name: str, **fields: object) -> None:
        with self._lock:
            if fix in self._told:
                return
            self._told.add(fix)
        event(log, level, name, key=fix[0], **fields)

    def _disk_low(self, key: FailKey, host: str, started: float) -> None:
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
        key: FailKey,
        reason: str,
        host: str,
        started: float,
        status: int | None = None,
        quiet: bool = False,
        counted: bool = True,
    ) -> None:
        with self._lock:
            count = self._failed.pop(key, (0.0, 0, ""))[1] + counted
            self._failed[key] = (self._clock(), count, reason)
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


def _http_reason(error: HttpError) -> str:
    if error.status in (404, 410):
        return "gone"
    if error.too_large:
        return SaveResult.TOO_LARGE.value
    return "unreachable"


def _log_cleaned(
    report: CleanReport,
    decisions_removed: int,
    trigger: str,
    links_known: bool | None,
    heard_removed: int = 0,
) -> None:
    """links_known is None when this clean did not look the links up."""
    event(
        log,
        logging.INFO,
        "cover_cache_cleaned",
        trigger=trigger,
        decisions_removed=decisions_removed,
        heard_removed=heard_removed,
        links_known=links_known,
        files_removed=report.files_removed,
        bytes_freed=report.bytes_freed,
        files=report.files,
        bytes=report.bytes,
    )


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
