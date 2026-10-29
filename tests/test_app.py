import contextlib
import dataclasses
import http.client
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import threading
import time
import tomllib
from datetime import UTC
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from tunebox import app
from tunebox.accounts import Accounts
from tunebox.app import WEB_ADMIN_DIR, StopSignal, main, open_settings, shut_down
from tunebox.catalogs import Catalogs
from tunebox.config import Config, state_from_toml
from tunebox.logos import group_key
from tunebox.pin import Pin
from tunebox.runtime import EMPTY, Runtime
from tunebox.server import TuneboxServer
from tunebox.settings import Settings, SettingsUnavailable, SettingsWriteError
from tunebox.store import CoverDecision, Store
from tunebox.usage import Usage
from tunebox.usagecore import FLUSH_S


def test_bad_log_level_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "loud")
    assert main() == 2
    assert "loud" in capsys.readouterr().err


def test_missing_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("TUNEBOX_CONFIG", str(tmp_path / "tunebox.toml"))
    assert main() == 2
    assert "tunebox.toml" in capsys.readouterr().err


def test_port_in_use_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\n\n'
            '[[channels]]\nid = "owr"\nname = "One World Radio"\n'
            'stream_url = "https://example.invalid/stream.aac"\n'
            'source = { type = "triton", mount = "OWR_INTERNATIONAL" }\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        assert main() == 2
    assert f"127.0.0.1:{port}" in capsys.readouterr().err


def test_shutdown_waits_for_workers_before_closing_the_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(None)
    stop = threading.Event()

    def late_writer() -> None:
        stop.wait()
        time.sleep(0.2)
        store.put_cover(CoverDecision("k", "miss", None, None, 1.0))

    worker = threading.Thread(target=late_writer)
    worker.start()
    settings = Settings(tmp_path / "t.db")
    runtime = Runtime(settings, lambda _config, _carry: pytest.fail("no channels expected"))
    pin = Pin(settings, lambda: b"k" * 32)
    server = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, pin)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}).start()
    stop.set()
    with caplog.at_level(logging.ERROR):
        shut_down(server, [worker], store, runtime, settings)
    assert [r.getMessage() for r in caplog.records] == []
    assert not worker.is_alive()


def test_a_stop_signal_ends_the_wait() -> None:
    received = StopSignal()
    waiter = threading.Thread(target=received.wait, kwargs={"poll_s": 0.01})
    waiter.start()
    received(signal.SIGTERM, None)
    waiter.join(2)
    assert not waiter.is_alive()


def test_the_stop_handler_takes_no_lock() -> None:
    # Python runs a signal handler on the main thread wherever it happens to
    # be, possibly holding a lock; a handler that waits for any lock can wait
    # for its own thread forever. A second signal must be just as harmless.
    received = StopSignal()
    held = threading.Lock()
    done: list[bool] = []

    def land_while_holding() -> None:
        with held:
            received(signal.SIGTERM, None)
            received(signal.SIGINT, None)
            done.append(received.received)

    caller = threading.Thread(target=land_while_holding)
    caller.start()
    caller.join(2)
    assert done == [True]


CHANNEL_DOC = {
    "id": "owr",
    "name": "One World Radio",
    "stream_url": "https://example.invalid/stream.aac",
    "source": {"type": "triton", "mount": "OWR_INTERNATIONAL"},
}
CHANNEL_TOML = (
    '[[channels]]\nid = "owr"\nname = "One World Radio"\n'
    'stream_url = "https://example.invalid/stream.aac"\n'
    'source = { type = "triton", mount = "OWR_INTERNATIONAL" }\n'
)


def test_a_corrupt_database_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tunebox.db").write_bytes(b"not a database" * 100)
    config = tmp_path / "tunebox.toml"
    config.write_text(CHANNEL_TOML, encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
    assert main() == 2
    assert "tunebox.db" in capsys.readouterr().err
    assert (tmp_path / "tunebox.db").read_bytes().startswith(b"not a database")


def test_open_settings_adopts_the_file_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = state_from_toml({"channels": [CHANNEL_DOC]})
    with caplog.at_level(logging.INFO):
        open_settings(tmp_path / "t.db", state, panel=False).close()
        settings = open_settings(tmp_path / "t.db", state, panel=False)
    assert settings.load()[1] == 1
    settings.close()
    assert [r.getMessage() for r in caplog.records].count("config_from_file") == 1


def test_with_the_panel_on_the_file_is_imported_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    one = state_from_toml({"channels": [CHANNEL_DOC]})
    two = state_from_toml({"channels": [CHANNEL_DOC, {**CHANNEL_DOC, "id": "two"}]})
    with caplog.at_level(logging.INFO):
        open_settings(tmp_path / "t.db", one, panel=True).close()
        settings = open_settings(tmp_path / "t.db", two, panel=True)
    assert [c.id for c in settings.load()[0].channels] == ["owr"]
    settings.close()
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("config_imported") == 1
    ignored = [r for r in caplog.records if r.getMessage() == "config_file_ignored"]
    assert [r.__dict__["fields"]["parts"] for r in ignored] == [["channels"]]


def test_group_images_count_with_the_stations_part_of_the_file() -> None:
    stored = state_from_toml({"channels": [CHANNEL_DOC]})
    file = dataclasses.replace(stored, group_images={"G": "https://x.test/g.png"})
    ignored_parts = app._ignored_parts  # pyright: ignore[reportPrivateUsage]
    assert ignored_parts(file, stored) == ["channels"]
    assert ignored_parts(stored, stored) == []


def test_with_the_panel_on_a_file_like_the_database_is_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    one = state_from_toml(
        {"channels": [CHANNEL_DOC], "player": {"latitude": 52.0, "longitude": 5.0}}
    )
    open_settings(tmp_path / "t.db", one, panel=False).close()
    with caplog.at_level(logging.INFO):
        open_settings(tmp_path / "t.db", one, panel=True).close()
    assert caplog.records == []


def test_with_the_panel_on_changed_defaults_and_player_are_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    one = state_from_toml({"channels": [CHANNEL_DOC]})
    two = state_from_toml(
        {
            "channels": [CHANNEL_DOC],
            "defaults": {"delay_s": 9},
            "player": {"default_channel": "owr"},
        }
    )
    open_settings(tmp_path / "t.db", one, panel=True).close()
    with caplog.at_level(logging.INFO):
        open_settings(tmp_path / "t.db", two, panel=True).close()
    ignored = [r for r in caplog.records if r.getMessage() == "config_file_ignored"]
    assert [r.__dict__["fields"]["parts"] for r in ignored] == [["defaults", "player"]]


def test_start_up_names_stored_rules_the_lint_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The file's stations are stored as they are; only the panel lints.
    rules = '[channels.rules]\nplayed_by = "(a+)+$"\n'
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {taken.getsockname()[1]}\n'
            f'log_file = "tunebox.log"\n\n{CHANNEL_TOML}{rules}',
            encoding="utf-8",
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        assert main() == 2
    lines = [json.loads(x) for x in (tmp_path / "tunebox.log").read_text("utf-8").splitlines()]
    refusals = [line for line in lines if line["event"] == "rule_refused"]
    assert [line["field"] for line in refusals] == ["channels[0].rules.played_by"]


def _blob_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_age_s: float) -> Path:
    """Starts tunebox (it exits 2 on a taken port) over a database whose first
    history entry is database_age_s old, with an old upload nothing names."""
    blobs = tmp_path / "blobs"
    blobs.mkdir(parents=True)
    named = "a" * 64
    orphan = blobs / f"{'b' * 64}.png"
    young = blobs / f"{'c' * 64}.png"
    kept = blobs / f"{named}.png"
    for path in (orphan, young, kept):
        path.write_bytes(b"x")
    long_ago = time.time() - 3 * 86_400
    os.utime(orphan, (long_ago, long_ago))
    os.utime(kept, (long_ago, long_ago))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {taken.getsockname()[1]}\n'
            f'log_file = "tunebox.log"\n\n{CHANNEL_TOML}logo = "blob:{named}"\n',
            encoding="utf-8",
        )
        seeded = Settings(tmp_path / "tunebox.db", clock=lambda: time.time() - database_age_s)
        seeded.adopt_file(state_from_toml(tomllib.loads(config.read_text("utf-8"))))
        seeded.close()
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        assert main() == 2
    return blobs


def test_start_up_warns_about_a_logo_whose_upload_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {taken.getsockname()[1]}\n'
            f'log_file = "tunebox.log"\n\n{CHANNEL_TOML}logo = "blob:{"d" * 64}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        assert main() == 2
    lines = [json.loads(x) for x in (tmp_path / "tunebox.log").read_text("utf-8").splitlines()]
    missing = [line for line in lines if line["event"] == "logo_blob_missing"]
    assert [(line["level"], line["channel"]) for line in missing] == [("warning", "owr")]


def test_start_up_warns_about_a_group_image_whose_upload_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {taken.getsockname()[1]}\n'
            f'log_file = "tunebox.log"\n\n[[groups]]\nname = "Secret group"\n'
            f'image = "blob:{"e" * 64}"\n\n{CHANNEL_TOML}',
            encoding="utf-8",
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        assert main() == 2
    text = (tmp_path / "tunebox.log").read_text("utf-8")
    lines = [json.loads(x) for x in text.splitlines()]
    missing = [line for line in lines if line["event"] == "logo_blob_missing"]
    assert [(line["level"], line["kind"], line["group"]) for line in missing] == [
        ("warning", "group", group_key("Secret group"))
    ]
    assert "Secret group" not in json.dumps(missing)


def test_start_up_removes_old_unnamed_blobs_and_keeps_named_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blobs = _blob_start(monkeypatch, tmp_path, 3 * 86_400)
    assert not (blobs / f"{'b' * 64}.png").exists()
    assert (blobs / f"{'c' * 64}.png").exists()
    assert (blobs / f"{'a' * 64}.png").exists()
    lines = [json.loads(x) for x in (tmp_path / "tunebox.log").read_text("utf-8").splitlines()]
    collected = [line for line in lines if line["event"] == "logo_blobs_collected"]
    assert [line["removed"] for line in collected] == [1]


def test_start_up_keeps_uploads_while_the_database_is_young(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blobs = _blob_start(monkeypatch, tmp_path, 60)
    assert (blobs / f"{'b' * 64}.png").exists()
    lines = [json.loads(x) for x in (tmp_path / "tunebox.log").read_text("utf-8").splitlines()]
    assert not [line for line in lines if line["event"] == "logo_blobs_collected"]


def test_uploads_live_outside_the_logo_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "owr.png").write_bytes(b"x")
    blobs = _blob_start(monkeypatch, tmp_path, 3 * 86_400)
    # Start-up collected in data_dir/blobs: the orphan there is gone.
    assert not (blobs / f"{'b' * 64}.png").exists()
    shutil.rmtree(tmp_path / "logos")
    assert (blobs / f"{'a' * 64}.png").exists()


def test_a_database_that_fails_while_listing_blobs_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_self: Settings) -> set[str]:
        raise SettingsUnavailable("disk I/O error")

    def old(_self: Settings) -> float:
        return 0.0

    monkeypatch.setattr(Settings, "referenced_blobs", broken)
    monkeypatch.setattr(Settings, "oldest_change", old)
    config = tmp_path / "tunebox.toml"
    config.write_text(CHANNEL_TOML, encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
    assert main() == 2
    assert "disk I/O error" in capsys.readouterr().err


def test_admin_needs_its_web_folder() -> None:
    assert (WEB_ADMIN_DIR / "index.html").is_file()


def test_a_signing_key_that_cannot_be_stored_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_self: Accounts) -> bytes:
        raise SettingsWriteError("disk full")

    monkeypatch.setattr(Accounts, "server_key", refuse)
    # A taken port ends start-up at the listen step if the key is not read
    # before it; read first, the key's error comes first.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\n\n{CHANNEL_TOML}', encoding="utf-8"
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        # Before serving, not on the first PIN request: the PIN must never
        # touch the database on the player's path.
        assert main() == 2
    assert "disk full" in capsys.readouterr().err


def start_on_a_taken_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_file: str) -> int:
    """Runs main until it fails to listen: the start-up lines are logged by then."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        config = tmp_path / "tunebox.toml"
        config.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\nlog_file = "{log_file}"\n\n'
            + CHANNEL_TOML,
            encoding="utf-8",
        )
        monkeypatch.setenv("LOG_LEVEL", "info")
        monkeypatch.setenv("TUNEBOX_CONFIG", str(config))
        return main()


def test_the_log_file_gets_the_start_up_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert start_on_a_taken_port(monkeypatch, tmp_path, "logs/tunebox.log") == 2
    lines = (tmp_path / "logs" / "tunebox.log").read_text(encoding="utf-8").splitlines()
    assert "config_from_file" in {json.loads(line)["event"] for line in lines}


def test_a_log_file_that_cannot_be_made_does_not_stop_tunebox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "blocked").write_text("a file, not a folder", encoding="utf-8")
    seen: list[str] = []
    real_event = app.event

    def record(logger: logging.Logger, level: int, name: str, **fields: object) -> None:
        seen.append(name)
        real_event(logger, level, name, **fields)

    monkeypatch.setattr(app, "event", record)
    assert start_on_a_taken_port(monkeypatch, tmp_path, "blocked/tunebox.log") == 2
    assert "log_file_unavailable" in seen
    # It went on to the listen step, past the log file.
    assert "cannot listen" in capsys.readouterr().err


def run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, panel: bool
) -> tuple[int, dict[str, str]]:
    """Runs main to a clean stop: the wait for a signal loads the player page
    once and returns. Gives main's exit code and that answer's headers. The
    server listens on port 0 (the file's port 1 only passes validation) and
    the page is loaded on the port the "started" line names."""
    admin = (
        '[admin]\nenabled = true\npublic_origin = "https://example.invalid"\n\n' if panel else ""
    )
    config = tmp_path / "tunebox.toml"
    config.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = 1\n\n{admin}{CHANNEL_TOML}',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("TUNEBOX_CONFIG", str(config))

    def no_handler(*_: object) -> None:
        """signal.signal works on the main thread only; nothing sends one here."""

    monkeypatch.setattr(app.signal, "signal", no_handler)
    real_load = app.load

    def any_port(path: Path) -> Config:
        loaded = real_load(path)
        return dataclasses.replace(loaded, server=dataclasses.replace(loaded.server, port=0))

    monkeypatch.setattr(app, "load", any_port)
    started: list[object] = []
    real_event = app.event

    def record(logger: logging.Logger, level: int, name: str, **fields: object) -> None:
        if name == "started":
            started.append(fields["port"])
        real_event(logger, level, name, **fields)

    monkeypatch.setattr(app, "event", record)
    seen: dict[str, str] = {}

    def load_the_page(_self: StopSignal, poll_s: float = 0.5) -> None:
        port = cast(int, started[0])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        connection.close()
        seen.update({k.lower(): v for k, v in response.getheaders()})
        seen["status"] = str(response.status)

    monkeypatch.setattr(StopSignal, "wait", load_the_page)
    return main(), seen


def test_with_the_panel_off_usage_is_never_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, seen = run_main(monkeypatch, tmp_path, panel=False)
    assert code == 0
    assert seen["status"] == "200"
    assert "set-cookie" not in seen
    assert not (tmp_path / "usage.db").exists()


def test_with_the_panel_on_usage_records_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, seen = run_main(monkeypatch, tmp_path, panel=True)
    assert code == 0
    assert seen["set-cookie"].startswith("tunebox_device=")
    device = seen["set-cookie"].split(";")[0].split("=")[1]
    # The final round at stop wrote the visit before the file was let go.
    with contextlib.closing(sqlite3.connect(tmp_path / "usage.db")) as db:
        assert [row[0] for row in db.execute("SELECT device FROM visits")] == [device]


@pytest.mark.parametrize("given", [None, "http://catalogue.invalid"])
def test_the_catalogue_knob_points_both_catalogues_or_leaves_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: str | None
) -> None:
    if given is None:
        monkeypatch.delenv("TUNEBOX_CATALOGS", raising=False)
    else:
        monkeypatch.setenv("TUNEBOX_CATALOGS", given)
    made: list[dict[str, Any]] = []
    real = app.Catalogs

    def recording(http_get: Any, **kwargs: Any) -> Catalogs:
        made.append(kwargs)
        return real(http_get, **kwargs)

    monkeypatch.setattr(app, "Catalogs", recording)
    assert run_main(monkeypatch, tmp_path, panel=True)[0] == 0
    expected = (
        {}
        if given is None
        else {
            "deezer": "http://catalogue.invalid/deezer",
            "itunes": "http://catalogue.invalid/itunes/search",
        }
    )
    # One instance: the panel's searches take their turn in the worker's throttles.
    assert made == [expected]


def usages_made(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The keyword arguments of every Usage main makes."""
    made: list[dict[str, Any]] = []
    real = app.Usage

    def recording(path: Path, **kwargs: Any) -> Usage:
        made.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(app, "Usage", recording)
    return made


@pytest.mark.parametrize(
    ("given", "zone", "name"),
    [
        ("Europe/Amsterdam", ZoneInfo("Europe/Amsterdam"), "Europe/Amsterdam"),
        (None, ZoneInfo("UTC"), "UTC"),
        ("Nowhere/Atlantis", UTC, "UTC"),
        ("../etc", UTC, "UTC"),
    ],
)
def test_usage_counts_days_in_tz_or_else_utc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: str | None, zone: object, name: str
) -> None:
    if given is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", given)
    made = usages_made(monkeypatch)
    assert run_main(monkeypatch, tmp_path, panel=True)[0] == 0
    assert [(m["zone"], m["zone_name"]) for m in made] == [(zone, name)]


@pytest.mark.parametrize(
    ("given", "warned"),
    [("Europe/Amsterdam", []), (None, []), ("Nowhere/Atlantis", ["Nowhere/Atlantis"])],
)
def test_an_unknown_tz_is_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    given: str | None,
    warned: list[str],
) -> None:
    if given is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", given)
    caplog.set_level(logging.WARNING, "tunebox")
    app.open_usage(tmp_path / "usage.db", lambda: None)
    assert [
        r.__dict__["fields"]["zone"]
        for r in caplog.records
        if r.getMessage() == "usage_zone_unknown" and r.levelno == logging.WARNING
    ] == warned


@pytest.mark.parametrize(("given", "flush_s"), [("0.5", 0.5), ("soon", FLUSH_S), (None, FLUSH_S)])
def test_the_flush_knob_is_passed_on_or_else_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: str | None, flush_s: float
) -> None:
    if given is None:
        monkeypatch.delenv("TUNEBOX_USAGE_FLUSH_S", raising=False)
    else:
        monkeypatch.setenv("TUNEBOX_USAGE_FLUSH_S", given)
    made = usages_made(monkeypatch)
    assert run_main(monkeypatch, tmp_path, panel=True)[0] == 0
    assert [m["flush_s"] for m in made] == [flush_s]


@pytest.mark.parametrize(("timeout_s", "share_s"), [(0.5, 0.5), (8.0, 3.0)])
def test_shutdown_joins_usage_first_within_its_share(
    tmp_path: Path, timeout_s: float, share_s: float
) -> None:
    calls: list[str] = []
    slack: list[float] = []

    class SpyUsage(Usage):
        def stop_and_join(self, deadline: float) -> None:
            calls.append("usage")
            slack.append(time.monotonic() + share_s - deadline)

    class SpyRuntime(Runtime):
        def stop_and_join(self, deadline: float) -> None:
            calls.append("runtime")
            super().stop_and_join(deadline)

    settings = Settings(tmp_path / "t.db")
    runtime = SpyRuntime(settings, lambda _config, _carry: pytest.fail("no channels expected"))
    usage = SpyUsage(tmp_path / "usage.db", zone=UTC, zone_name="UTC", free=lambda: None)
    pin = Pin(settings, lambda: b"k" * 32)
    server = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path, pin)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    shut_down(server, [], Store(None), runtime, settings, usage=usage, timeout_s=timeout_s)
    assert calls == ["usage", "runtime"]
    # At most the share from now, and not far under it either.
    assert 0 <= slack[0] < 0.25
