"""Wires the stored configuration, the running channels, the workers and the
HTTP server together."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, tzinfo
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tunebox import http
from tunebox.accounts import Accounts
from tunebox.admin import Admin
from tunebox.blobs import Blobs
from tunebox.bursts import Bursts
from tunebox.catalogs import Catalogs
from tunebox.channel import Channel
from tunebox.config import ChannelConfig, ConfigError, ConfigState, load
from tunebox.coverfiles import MAX_COVER, CoverFiles
from tunebox.covers import CoverService
from tunebox.disk import free_bytes
from tunebox.logos import Logos
from tunebox.logs import add_file, configure, event
from tunebox.netguard import Guard
from tunebox.pin import Pin
from tunebox.rulecheck import stored_refusals
from tunebox.runtime import Runtime
from tunebox.server import TuneboxServer
from tunebox.settings import Settings, SettingsUnavailable, SettingsWriteError, StaleVersion
from tunebox.sources import build_source
from tunebox.store import Store
from tunebox.streamcheck import FEED_FETCH, PROBE_BYTES, PROBE_DEADLINE_S, StreamChecks
from tunebox.usage import Usage
from tunebox.usagecore import FLUSH_S

log = logging.getLogger("tunebox")

DEFAULT_CONFIG = "/data/tunebox.toml"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Outside web/, so the player's static files can never serve the admin pages.
WEB_ADMIN_DIR = Path(__file__).resolve().parent.parent / "web_admin"


def main() -> int:
    try:
        configure(os.environ.get("LOG_LEVEL", "info"))
    except ValueError as error:
        print(f"tunebox: {error}", file=sys.stderr)
        return 2
    try:
        config = load(Path(os.environ.get("TUNEBOX_CONFIG", DEFAULT_CONFIG)))
    except ConfigError as error:
        print(f"tunebox: {error}", file=sys.stderr)
        return 2

    if config.server.log_file is not None:
        try:
            add_file(config.server.log_file)
        except OSError as error:
            # Logging is best-effort: the player matters more than its copy.
            event(
                log,
                logging.WARNING,
                "log_file_unavailable",
                path=str(config.server.log_file),
                error=str(error),
            )

    try:
        settings = open_settings(
            config.server.data_dir / "tunebox.db", config.state, config.admin.enabled
        )
    except (SettingsUnavailable, SettingsWriteError, StaleVersion, ConfigError) as error:
        event(log, logging.ERROR, "settings_unavailable", error=str(error))
        print(f"tunebox: {error}", file=sys.stderr)
        return 2

    def free_space() -> int | None:
        return free_bytes(config.server.data_dir)

    # Beside the logo cache, not in it: data/logos/ can be deleted, while an
    # upload has no other copy.
    blobs = Blobs(config.server.data_dir / "blobs", free_space=free_space)
    try:
        stored = settings.load()[0]
        for field, reason in stored_refusals(stored):
            event(log, logging.WARNING, "rule_refused", field=field, reason=reason)
        for row in stored.channels:
            logo = row.doc.get("logo")
            if isinstance(logo, str) and logo.startswith("blob:") and blobs.path(logo) is None:
                event(log, logging.WARNING, "logo_blob_missing", channel=row.id)
        # No group name in the event: it is free text an admin typed.
        for image in stored.group_images.values():
            if image.startswith("blob:") and blobs.path(image) is None:
                event(log, logging.WARNING, "logo_blob_missing", kind="group")
        removed = blobs.tidy(settings.oldest_change(), settings.referenced_blobs)
        if removed is None:
            event(log, logging.DEBUG, "logo_blobs_kept", reason="database_young")
    except (SettingsUnavailable, SettingsWriteError) as error:
        event(log, logging.ERROR, "settings_unavailable", error=str(error))
        print(f"tunebox: {error}", file=sys.stderr)
        settings.close()
        return 2
    if removed:
        event(log, logging.INFO, "logo_blobs_collected", removed=removed)
    store = Store(config.server.data_dir / "tunebox.db")
    guard = Guard()
    cover_files = CoverFiles(config.server.data_dir / "covers", free=free_space)
    catalogs = Catalogs(http.get)
    covers = CoverService(
        store,
        catalogs.find,
        cover_files,
        lambda url: guard.get(url, max_bytes=MAX_COVER),
        find_photo=catalogs.artist_photo,
    )
    logos = Logos(config.server.data_dir / "logos", guard.get, blobs)
    bursts = Bursts(guard.stream)

    def make_channel(channel: ChannelConfig, carry: Channel | None) -> Channel:
        return Channel(channel, build_source(channel.source, http.get), covers, store, carry=carry)

    runtime = Runtime(settings, make_channel, logos)
    # Both exist with the panel off too: a PIN switched on stays on.
    accounts = Accounts(settings)
    try:
        # Read (or made) now, so checking a PIN grant never touches the
        # database on the player's path, and a failure stops start-up here.
        accounts.server_key()
    except (SettingsUnavailable, SettingsWriteError) as error:
        event(log, logging.ERROR, "settings_unavailable", error=str(error))
        print(f"tunebox: {error}", file=sys.stderr)
        store.close()
        settings.close()
        return 2
    pin = Pin(settings, accounts.server_key)
    admin = None
    usage = None
    if config.admin.enabled:
        usage = open_usage(config.server.data_dir / "usage.db", free_space)
        admin_dir = Path(os.environ.get("TUNEBOX_WEB_ADMIN", str(WEB_ADMIN_DIR)))
        checks = StreamChecks(
            lambda url: guard.probe(url, PROBE_DEADLINE_S, PROBE_BYTES),
            lambda source: build_source(source, http.get).fetch(FEED_FETCH),
        )
        admin = Admin(
            accounts,
            config.admin,
            admin_dir,
            checks=checks,
            settings=settings,
            pin=pin,
            blobs=blobs,
            free_space=free_space,
            usage=usage,
            apply=runtime.apply,
            samples=lambda: [
                title
                for channel in runtime.snapshot.channels.values()
                for title in channel.recent_titles()
            ],
        )
    web_dir = Path(os.environ.get("TUNEBOX_WEB", str(WEB_DIR)))
    address = (config.server.host, config.server.port)
    try:
        server = TuneboxServer(
            address,
            lambda: runtime.snapshot,
            web_dir,
            pin,
            logos=logos,
            covers=cover_files,
            bursts=bursts,
            admin=admin,
            trusted=config.server.trusted_proxies,
            usage=usage,
        )
    except OSError as error:
        print(f"tunebox: cannot listen on {address[0]}:{address[1]}: {error}", file=sys.stderr)
        store.close()
        settings.close()
        return 2

    stop = threading.Event()
    received = StopSignal()
    signal.signal(signal.SIGTERM, received)
    signal.signal(signal.SIGINT, received)

    workers = [
        threading.Thread(target=covers.run_forever, args=(stop,), name="covers", daemon=True),
        threading.Thread(target=bursts.run_forever, args=(stop,), name="bursts", daemon=True),
        threading.Thread(target=logos.run_forever, args=(stop,), name="logos", daemon=True),
    ]
    runtime.apply()
    http_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    for thread in [*workers, http_thread]:
        thread.start()
    if usage is not None:
        # Not among the workers: shut_down joins it first, with its own share
        # of the deadline.
        usage.start(stop)
    event(
        log,
        logging.INFO,
        "started",
        port=server.server_address[1],
        channels=list(runtime.snapshot.channels),
    )

    # serve_forever runs in its own thread because shutdown() must be called
    # from another one; the main thread only waits for a signal.
    received.wait()
    stop.set()
    shut_down(server, workers, store, runtime, settings, usage=usage)
    event(log, logging.INFO, "stopped")
    return 0


class StopSignal:
    """Records SIGTERM and SIGINT for the main thread. The handler only sets an
    attribute: Python runs it on the main thread at whatever point that thread
    is, possibly holding a lock (inside Event.wait, for instance), so a handler
    that takes a lock, as Event.set() does, can wait for its own thread forever."""

    def __init__(self) -> None:
        self.received = False

    def __call__(self, _signum: int, _frame: FrameType | None) -> None:
        self.received = True

    def wait(self, poll_s: float = 0.5) -> None:
        while not self.received:
            time.sleep(poll_s)


def open_usage(path: Path, free_space: Callable[[], int | None]) -> Usage:
    """Usage statistics count local days in the container's TZ; an unset or
    unknown zone counts in UTC."""
    zone_name = os.environ.get("TZ") or "UTC"
    try:
        zone: tzinfo = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError, ValueError:
        event(log, logging.WARNING, "usage_zone_unknown", zone=zone_name)
        zone, zone_name = UTC, "UTC"
    try:
        # A knob for the admin browser check, which cannot wait a whole interval.
        flush_s = float(os.environ.get("TUNEBOX_USAGE_FLUSH_S", FLUSH_S))
    except ValueError:
        flush_s = FLUSH_S
    return Usage(path, zone=zone, zone_name=zone_name, free=free_space, flush_s=flush_s)


def open_settings(path: Path, state: ConfigState, panel: bool) -> Settings:
    """Opens the stored configuration. With the admin panel on, the panel is
    where stations change: tunebox.toml fills an empty database once and is
    ignored afterwards. With it off, the file stays the stations' source."""
    settings = Settings(path)
    try:
        if panel:
            version = settings.import_once(state)
            name = "config_imported"
        else:
            version = settings.adopt_file(state)
            name = "config_from_file"
    except BaseException:
        settings.close()
        raise
    if version is not None:
        event(log, logging.INFO, name, version=version, channels=len(state.channels))
    elif panel:
        ignored = _ignored_parts(state, settings.load()[0])
        if ignored:
            event(log, logging.WARNING, "config_file_ignored", parts=ignored)
    return settings


def _ignored_parts(file: ConfigState, stored: ConfigState) -> list[str]:
    """The parts of tunebox.toml that say something other than the
    database, which the panel's tunebox does not read."""
    parts = {
        "channels": (
            file.channels,
            (file.groups, file.channels, file.group_images),
            (stored.groups, stored.channels, stored.group_images),
        ),
        "defaults": (file.defaults, file.defaults, stored.defaults),
        "player": (file.player, file.player, stored.player),
    }
    return [name for name, (given, mine, theirs) in parts.items() if given and mine != theirs]


def shut_down(
    server: TuneboxServer,
    workers: list[threading.Thread],
    store: Store,
    runtime: Runtime,
    settings: Settings,
    usage: Usage | None = None,
    timeout_s: float = 8.0,
) -> None:
    """Stops serving, lets the pollers and workers finish what they are
    doing, then closes the stores; closing them first would turn their last
    writes into errors."""
    server.shutdown()
    server.server_close()
    # One shared deadline, inside Docker's default 10 s stop grace. A thread
    # stuck in a slow fetch past it is a daemon thread and ends with the process.
    deadline = time.monotonic() + timeout_s
    if usage is not None:
        # Its final flush ends the open listening sessions; capped so a slow
        # disk still leaves the pollers their time.
        usage.stop_and_join(min(deadline, time.monotonic() + 3.0))
    runtime.stop_and_join(deadline)
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    store.close()
    settings.close()
