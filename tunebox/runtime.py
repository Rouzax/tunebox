"""The live set of channels. A committed configuration change is applied
here without a restart: pollers start, stop or restart, and request threads
see the result as one immutable snapshot, read once per request."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from tunebox.channel import Channel
from tunebox.config import ChannelConfig, ConfigError, PlayerConfig, validate_state
from tunebox.logos import Logos
from tunebox.logs import event
from tunebox.settings import Settings, SettingsUnavailable

log = logging.getLogger(__name__)

# A change to one of these changes what the poller fetches or keeps, so the
# channel restarts; every other field is swapped in place.
POLLER_FIELDS = frozenset({"source", "rules", "poll_interval_s", "history_size"})
LIVE_FIELDS = frozenset(
    {
        "id",
        "name",
        "streams",
        "default_image",
        "delay_s",
        "pawtunes_delay_s",
        "group",
        "logo",
        "always",
        "needs_pin",
    }
)
# A field added to ChannelConfig must be put in one of the two sets.
_UNCLASSIFIED = {f.name for f in dataclasses.fields(ChannelConfig)} ^ (POLLER_FIELDS | LIVE_FIELDS)
if _UNCLASSIFIED:
    raise RuntimeError(f"ChannelConfig fields not classified for reload: {sorted(_UNCLASSIFIED)}")

MakeChannel = Callable[[ChannelConfig, Channel | None], Channel]


@dataclass(frozen=True)
class Snapshot:
    version: int
    # Enabled channels in listing order.
    channels: Mapping[str, Channel]
    player: PlayerConfig


EMPTY = Snapshot(0, MappingProxyType({}), PlayerConfig())


@dataclass
class _Live:
    channel: Channel
    stop: threading.Event
    thread: threading.Thread


def needs_restart(old: ChannelConfig, new: ChannelConfig) -> bool:
    return any(getattr(old, name) != getattr(new, name) for name in POLLER_FIELDS)


class Runtime:
    """apply() is serialised by one lock; readers only read the snapshot
    attribute, which is replaced whole and never changed in place."""

    def __init__(
        self, settings: Settings, make_channel: MakeChannel, logos: Logos | None = None
    ) -> None:
        self._settings = settings
        self._make = make_channel
        self._logos = logos
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}
        self._retired: list[threading.Thread] = []
        self._logos_swept = False
        self.snapshot = EMPTY

    def apply(self) -> int:
        """Brings the live channels in line with the latest committed
        configuration and returns the version now live. It reads the latest
        commit itself, so two saves applied in either order end the same."""
        with self._lock:
            try:
                if self._settings.version() <= self.snapshot.version:
                    return self.snapshot.version
                state, version = self._settings.load()
                resolved = validate_state(state)
            except (SettingsUnavailable, ConfigError) as error:
                event(log, logging.ERROR, "apply_failed", error=str(error))
                return self.snapshot.version
            if version <= self.snapshot.version:
                return self.snapshot.version
            # A stopped old entry (retired before an earlier apply failed) is
            # made again too. Every channel that needs making is made first: a source that
            # cannot be built fails here, before any running one is touched.
            # A channel made with carry copies the old one's recent list a
            # moment before the old one is retired; a poll landing in between
            # is fetched again by the new poller.
            made: dict[str, Channel] = {}
            try:
                for config in resolved.channels:
                    old = self._live.get(config.id)
                    if old is None:
                        made[config.id] = self._make(config, None)
                    elif old.stop.is_set() or needs_restart(old.channel.config, config):
                        made[config.id] = self._make(config, old.channel)
            except Exception as error:  # noqa: BLE001 - any build failure keeps the live set
                event(log, logging.ERROR, "apply_failed", error=str(error), version=version)
                return self.snapshot.version
            live: dict[str, _Live] = {}
            started: list[_Live] = []
            try:
                for config in resolved.channels:
                    old = self._live.get(config.id)
                    previous = old.channel.config if old else None
                    if config.id in made:
                        if old is not None:
                            # Retired first, so a poll it has in flight cannot
                            # save after the replacement has started. An old
                            # channel retired before a later failure stays in
                            # the snapshot until the next apply rebuilds it.
                            self._retire(old)
                        entry = self._start(made[config.id])
                        started.append(entry)
                        live[config.id] = entry
                    else:
                        assert old is not None
                        old.channel.replace_config(config)
                        live[config.id] = old
                    if self._logos is not None and (
                        previous is None
                        or previous.logo != config.logo
                        or previous.default_image != config.default_image
                    ):
                        self._logos.request(config)
            except Exception as error:  # noqa: BLE001 - a thread that cannot start keeps the live set
                for entry in started:
                    self._retire(entry)
                event(log, logging.ERROR, "apply_failed", error=str(error), version=version)
                return self.snapshot.version
            for channel_id, gone in self._live.items():
                if channel_id not in live:
                    self._retire(gone)
                    if self._logos is not None:
                        self._logos.forget(channel_id)
            self._live = live
            if self._logos is not None and not self._logos_swept:
                # Files of a station removed while tunebox was stopped; later
                # removals are forgotten above as they happen.
                self._logos_swept = True
                self._logos.forget_all_except(live)
            self._retired = [thread for thread in self._retired if thread.is_alive()]
            channels = MappingProxyType({cid: entry.channel for cid, entry in live.items()})
            self.snapshot = Snapshot(version, channels, resolved.player)
        event(log, logging.INFO, "config_applied", version=version, channels=len(live))
        return version

    def stop_and_join(self, deadline: float) -> None:
        """Stops every poller and waits for them until the monotonic
        deadline; a last poll in flight still saves its list."""
        with self._lock:
            entries = list(self._live.values())
            threads = [*(entry.thread for entry in entries), *self._retired]
            for entry in entries:
                entry.stop.set()
                entry.channel.wake()
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _start(self, channel: Channel) -> _Live:
        stop = threading.Event()
        thread = threading.Thread(
            target=channel.run, args=(stop,), name=f"channel-{channel.config.id}", daemon=True
        )
        thread.start()
        return _Live(channel, stop, thread)

    def _retire(self, entry: _Live) -> None:
        entry.channel.retire()
        entry.stop.set()
        self._retired.append(entry.thread)
