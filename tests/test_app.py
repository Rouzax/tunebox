import logging
import signal
import socket
import threading
import time
from pathlib import Path

import pytest

from tunebox.accounts import Accounts
from tunebox.app import WEB_ADMIN_DIR, StopSignal, main, open_settings, shut_down
from tunebox.config import state_from_toml
from tunebox.runtime import EMPTY, Runtime
from tunebox.server import TuneboxServer
from tunebox.settings import Settings, SettingsWriteError
from tunebox.store import CoverDecision, Store


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
    server = TuneboxServer(("127.0.0.1", 0), lambda: EMPTY, tmp_path)
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
