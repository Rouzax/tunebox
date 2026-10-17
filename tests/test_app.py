import logging
import socket
import threading
import time
from pathlib import Path

import pytest

from tunebox.app import main, shut_down
from tunebox.server import TuneboxServer
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
    server = TuneboxServer(("127.0.0.1", 0), {}, tmp_path)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}).start()
    stop.set()
    with caplog.at_level(logging.ERROR):
        shut_down(server, [worker], store)
    assert [r.getMessage() for r in caplog.records] == []
    assert not worker.is_alive()
