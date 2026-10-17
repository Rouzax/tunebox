"""Wires the configured channels, the cover worker and the HTTP server together."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import FrameType

from tunebox import http
from tunebox.bursts import Bursts
from tunebox.catalogs import Catalogs
from tunebox.channel import Channel
from tunebox.config import ConfigError, load
from tunebox.covers import CoverService
from tunebox.logos import Logos
from tunebox.logs import configure, event
from tunebox.server import TuneboxServer
from tunebox.sources import build_source
from tunebox.store import Store

log = logging.getLogger("tunebox")

DEFAULT_CONFIG = "/data/tunebox.toml"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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

    store = Store(config.server.data_dir / "tunebox.db")
    covers = CoverService(store, Catalogs(http.get).find)
    logos = Logos(config.server.data_dir / "logos", http.get)
    bursts = Bursts(http.stream)
    channels = {
        c.id: Channel(c, build_source(c.source, http.get), covers, store) for c in config.channels
    }
    web_dir = Path(os.environ.get("TUNEBOX_WEB", str(WEB_DIR)))
    address = (config.server.host, config.server.port)
    try:
        server = TuneboxServer(
            address, channels, web_dir, logos=logos, player=config.player, bursts=bursts
        )
    except OSError as error:
        print(f"tunebox: cannot listen on {address[0]}:{address[1]}: {error}", file=sys.stderr)
        return 2

    stop = threading.Event()
    received = StopSignal()
    signal.signal(signal.SIGTERM, received)
    signal.signal(signal.SIGINT, received)

    workers = [
        threading.Thread(target=covers.run_forever, args=(stop,), name="covers", daemon=True),
        threading.Thread(target=bursts.run_forever, args=(stop,), name="bursts", daemon=True),
    ]
    workers += [
        threading.Thread(target=c.run, args=(stop,), name=f"channel-{c.config.id}", daemon=True)
        for c in channels.values()
    ]
    workers.append(
        threading.Thread(target=logos.fetch_all, args=(config.channels,), name="logos", daemon=True)
    )
    http_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    for thread in [*workers, http_thread]:
        thread.start()
    event(log, logging.INFO, "started", port=server.server_address[1], channels=list(channels))

    # serve_forever runs in its own thread because shutdown() must be called
    # from another one; the main thread only waits for a signal.
    received.wait()
    stop.set()
    shut_down(server, workers, store)
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


def shut_down(
    server: TuneboxServer, workers: list[threading.Thread], store: Store, timeout_s: float = 8.0
) -> None:
    """Stops serving, lets the workers finish what they are doing, then closes
    the store; closing it first would turn their last writes into errors."""
    server.shutdown()
    server.server_close()
    # One shared deadline, inside Docker's default 10 s stop grace. A worker
    # stuck in a slow fetch past it is a daemon thread and ends with the process.
    deadline = time.monotonic() + timeout_s
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    store.close()
