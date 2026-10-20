"""One radio channel: polls its source and answers what is playing."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from tunebox.config import ChannelConfig
from tunebox.covers import HISTORY, NOW, CoverService
from tunebox.logs import bound, event
from tunebox.sources.base import Source, SourceError
from tunebox.store import Store
from tunebox.timeline import Timeline, build
from tunebox.tracks import Track, clean

log = logging.getLogger(__name__)

# Triton returns at most 50 entries; afterwards a few are enough to see changes.
FIRST_FETCH = 50
STEADY_FETCH = 5
MAX_BACKOFF_S = 120.0
JITTER_S = 3.0
# A cold start would otherwise queue a lookup for every history entry, and
# iTunes allows about 20 searches a minute.
HISTORY_LOOKUPS = 20
# PawTunes' browser keeps at most 20 history entries.
PAWTUNES_HISTORY = 20
# A channel that is not pinned keeps polling this long after the last request.
ACTIVE_MS = 600_000
# How often an idle poller looks again; a request wakes it at once.
IDLE_CHECK_S = 1.0


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Channel:
    """The poller thread writes; request threads read. Everything shared is
    guarded by one lock, never held while talking to the source."""

    def __init__(
        self,
        config: ChannelConfig,
        source: Source,
        covers: CoverService,
        store: Store,
        clock_ms: Callable[[], int] = now_ms,
        rng: random.Random | None = None,
        carry: Channel | None = None,
    ) -> None:
        self.config = config
        self._source = source
        self._covers = covers
        self._store = store
        self._clock = clock_ms
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._recent: list[Track] = store.load_recent(config.id)[: config.history_size]
        self._polled = False
        self._retired = False
        self._failures = 0
        self._stale_since: int | None = None
        self._last_ok: int | None = None
        self._frozen_artwork: dict[tuple[int, str], str | None] = {}
        self._active_until = 0
        self._wake = threading.Event()
        # Set by a successful poll; cleared while idle. What was current
        # before an idle spell is long over, so nothing is current until then.
        self._fresh = threading.Event()
        if carry is not None:
            self._carry_from(carry)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.active():
                self.sleep_until_active(stop)
                continue
            with bound("poll"):
                try:
                    delay = self.poll_once()
                except Exception as error:  # a bug in one poll must not end the channel
                    fields = {"channel": self.config.id}
                    log.error("poll_crashed", exc_info=True, extra={"fields": fields})
                    delay = self._failed(error)
            stop.wait(delay)

    def touch(self) -> bool:
        """Marks the channel as listened to; an idle poller wakes at once.
        Returns whether the channel was idle."""
        was_idle = not self.active()
        with self._lock:
            self._active_until = self._clock() + ACTIVE_MS
        self._wake.set()
        return was_idle

    def wait_fresh(self, timeout_s: float) -> bool:
        """Waits up to timeout_s for a successful poll since waking."""
        return self._fresh.wait(timeout_s)

    def _carry_from(self, old: Channel) -> None:
        """A restart for new poll settings keeps what listeners see: the
        recent list, PawTunes' artwork decisions, the activity window and,
        when the source is the same, the freshness of the last poll."""
        with old._lock:
            self._recent = list(old._recent)[: self.config.history_size]
            self._frozen_artwork = dict(old._frozen_artwork)
            self._active_until = old._active_until
            fresh = old._fresh.is_set() and old.config.source == self.config.source
        if fresh:
            self._fresh.set()

    def retire(self) -> None:
        """Called when a replacement takes over: a poll still in flight then
        ends without writing over the replacement's recent list."""
        self._retired = True
        self.wake()

    def wake(self) -> None:
        """Ends an idle wait at once, so a stop is seen without waiting out
        the idle check."""
        self._wake.set()

    def replace_config(self, config: ChannelConfig) -> None:
        """Swaps settings that do not change what is polled (name, streams,
        delays, logo); readers see the old or the new one, never a mix."""
        self.config = config

    def active(self) -> bool:
        if self.config.always:
            return True
        with self._lock:
            return self._clock() < self._active_until

    def sleep_until_active(self, stop: threading.Event) -> None:
        """Waits without network traffic until a request or stop."""
        # Back after a gap, the feed may have moved on by more than a few entries.
        self._polled = False
        self._fresh.clear()
        # Idle, the feed is not polled, so an earlier failure says nothing
        # about it now; the next poll after waking decides afresh.
        with self._lock:
            self._stale_since = None
            self._failures = 0
        event(log, logging.DEBUG, "channel_idle", channel=self.config.id)
        while not stop.is_set() and not self.active():
            self._wake.wait(IDLE_CHECK_S)
            self._wake.clear()
        if not stop.is_set():
            event(log, logging.DEBUG, "channel_active", channel=self.config.id)

    def poll_once(self) -> float:
        """Polls the source once; returns the seconds to wait before the next poll."""
        started = time.monotonic()
        count = STEADY_FETCH if self._polled else FIRST_FETCH
        try:
            raw = self._source.fetch(count)
        except SourceError as error:
            return self._failed(error)
        now = self._clock()
        entries = [(r.started_at_ms is None, clean(r, self.config.rules, now)) for r in raw]
        with self._lock:
            had_tracks = bool(self._recent)
            added = self._merge(entries)
            recent = list(self._recent)
            self._polled = True
            self._fresh.set()
            self._failures = 0
            self._last_ok = now
            recovered_from, self._stale_since = self._stale_since, None
        if added and not self._retired:
            self._store.save_recent(self.config.id, recent)
        if had_tracks:
            for track in added:
                event(
                    log,
                    logging.INFO,
                    "track_changed",
                    channel=self.config.id,
                    kind=track.kind,
                    artist=track.artist,
                    title=track.full_title,
                    started_at=track.started_at,
                )
        if recovered_from is not None:
            stale_ms = now - recovered_from
            event(log, logging.INFO, "source_recovered", channel=self.config.id, stale_ms=stale_ms)
        self._request_covers(recent, now)
        event(
            log,
            logging.DEBUG,
            "poll",
            channel=self.config.id,
            entries=len(raw),
            added=len(added),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return self.config.poll_interval_s + self._rng.uniform(0, JITTER_S)

    def _merge(self, entries: list[tuple[bool, Track]]) -> list[Track]:
        """Caller holds the lock."""
        known = {t.ident for t in self._recent}
        newest = self._recent[0].key if self._recent else None
        added: list[Track] = []
        for untimed, track in entries:
            # An untimed entry is stamped with the time it was seen, so the
            # same track would otherwise be added again on every poll.
            if track.ident in known or (untimed and track.key == newest):
                continue
            added.append(track)
            known.add(track.ident)
        if added:
            merged = sorted([*self._recent, *added], key=lambda t: t.started_at, reverse=True)
            self._recent = merged[: self.config.history_size]
        return added

    def _failed(self, error: Exception) -> float:
        with self._lock:
            self._failures += 1
            failures = self._failures
            first = self._stale_since is None
            if first:
                self._stale_since = self._clock()
        if first:
            event(log, logging.WARNING, "source_stale", channel=self.config.id, error=str(error))
        backoff = min(MAX_BACKOFF_S, self.config.poll_interval_s * 2**failures)
        return backoff + self._rng.uniform(0, JITTER_S)

    def _request_covers(self, recent: list[Track], now: int) -> None:
        timeline = build(recent, now, self.config.delay_s)
        for track in timeline.not_heard:
            self._covers.request(track, NOW)
        if timeline.audible is not None:
            self._covers.request(timeline.audible, NOW)
        pawtunes = build(recent, now, self.config.pawtunes_delay_s)
        if pawtunes.audible is not None:
            self._covers.request(pawtunes.audible, NOW)
        for track in timeline.history[:HISTORY_LOOKUPS]:
            self._covers.request(track, HISTORY)

    def info(self) -> dict[str, Any]:
        return {
            "id": self.config.id,
            "name": self.config.name,
            # The first stream's address, kept for clients written before streams.
            "stream_url": self.config.stream_url,
            "default_image": self.config.default_image,
            "group": self.config.group,
            "streams": [
                {"name": s.name, "url": s.url, "codec": s.codec, "kbps": s.kbps}
                for s in self.config.streams
            ],
        }

    def recent_titles(self) -> list[str]:
        """The full titles of the recent list, newest first: the sample new
        title rules are checked against."""
        with self._lock:
            return [track.full_title for track in self._recent]

    def status(self) -> dict[str, Any]:
        active = self.active()
        with self._lock:
            return {
                "last_poll_ok": self._last_ok,
                "stale_since": self._stale_since,
                "active": active,
            }

    def _timeline(self, delay_s: float) -> tuple[Timeline, int | None]:
        with self._lock:
            recent = list(self._recent)
            stale = self._stale_since
        return build(recent, self._clock(), delay_s), stale

    def _track_json(self, track: Track) -> dict[str, Any]:
        cover = self._covers.lookup(track)
        return {
            "artist": track.artist,
            "artists": list(track.artists),
            "title": track.title,
            "played_by": track.played_by,
            "started_at": track.started_at,
            "cover": cover.url if cover else None,
            "cover_kind": cover.kind if cover else None,
        }

    def now_view(
        self,
        buffer_s: float = 0,
        version: int = 0,
        stream: str | None = None,
        pin: str = "off.0",
    ) -> tuple[dict[str, Any], str]:
        """buffer_s is how far the asking player plays behind the live stream.
        Counting it here, rather than in the player, keeps the switch time a
        relative countdown that a wrong device clock cannot shift. version is
        the configuration version; it is in the ETag, so a player on an
        unchanged track still learns of a change; pin, the PIN revision, is
        in the body for the same reason."""
        timeline, stale = self._timeline(self.config.delay_s + buffer_s)
        current = timeline.current
        history = list(timeline.history)
        upcoming: dict[str, Any] | None = None
        if not self._fresh.is_set():
            # Woken from idle (or just started): the last known track is history.
            if current is not None:
                history.insert(0, current)
            current = None
        elif timeline.upcoming is not None:
            track = timeline.upcoming
            upcoming = {
                # A station ID is coming: the player switches to the channel name.
                "track": self._track_json(track) if track.kind == "track" else None,
                "in_s": timeline.upcoming_in_s,
            }
        body: dict[str, Any] = {
            "current": self._track_json(current) if current else None,
            "upcoming": upcoming,
            "history": [self._track_json(t) for t in history],
            "stale_since": stale,
            "version": version,
            "stream": stream,
            "pin": pin,
        }
        return body, _etag(body)

    def pawtunes_view(self) -> dict[str, Any]:
        """The shape PawTunes' "custom" stats source reads."""
        timeline, _ = self._timeline(self.config.pawtunes_delay_s)
        audible = timeline.audible if self._fresh.is_set() else None
        body: dict[str, Any]
        if audible is None:
            body = {"artist": "", "title": self.config.name}
        elif audible.kind == "station_id":
            # PawTunes fills in its default artist; the car theme recognises the
            # pair and shows the channel name.
            body = {"artist": "", "title": audible.full_title}
        else:
            body = {"artist": audible.artist, "title": audible.full_title}
            artwork = self._frozen(audible)
            if artwork:
                body["artwork"] = artwork
        entries = ([timeline.current] if timeline.current else []) + list(timeline.history)
        history: list[dict[str, Any]] = []
        for track in entries[:PAWTUNES_HISTORY]:
            entry: dict[str, Any] = {
                "artist": track.artist,
                "title": track.full_title,
                "played_at": track.started_at,
            }
            if cover := self._covers.lookup(track):
                entry["artwork"] = cover.url
            history.append(entry)
        body["history"] = history
        return body

    def _frozen(self, track: Track) -> str | None:
        """The artwork PawTunes gets for a track is decided once, when PawTunes
        first sees it as current. PawTunes treats a changed artwork link,
        including one appearing or disappearing, as a new track."""
        with self._lock:
            if track.ident not in self._frozen_artwork:
                cover = self._covers.lookup(track)
                self._frozen_artwork[track.ident] = cover.url if cover else None
                live = {t.ident for t in self._recent}
                for ident in [i for i in self._frozen_artwork if i not in live]:
                    del self._frozen_artwork[ident]
            return self._frozen_artwork[track.ident]


def _etag(body: dict[str, Any]) -> str:
    # The countdown changes every second; the player keeps its own deadline,
    # so leaving it out lets unchanged polls end in a 304.
    upcoming = body["upcoming"]
    stable = {**body, "upcoming": {"track": upcoming["track"]} if upcoming else None}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:20]
    return f'"{digest}"'
