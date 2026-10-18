import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tunebox.config import ChannelRow, ConfigError, ConfigState
from tunebox.settings import (
    MIGRATIONS,
    Settings,
    SettingsUnavailable,
    SettingsWriteError,
    StaleVersion,
)


def doc(channel_id: str = "owr", **extra: Any) -> dict[str, Any]:
    return {
        "id": channel_id,
        "name": channel_id.upper(),
        "stream_url": "https://example.invalid/stream.aac",
        "source": {"type": "triton", "mount": "OWR_INTERNATIONAL"},
        **extra,
    }


def state(*ids: str, group: str | None = None) -> ConfigState:
    groups = (group,) if group else ()
    return ConfigState(
        groups=groups, channels=tuple(ChannelRow(i, group, True, doc(i)) for i in ids)
    )


@pytest.fixture
def settings(tmp_path: Path) -> Generator[Settings]:
    opened = Settings(tmp_path / "tunebox.db", clock=lambda: 1000.0)
    yield opened
    opened.close()


def test_a_new_database_is_empty_at_version_0(settings: Settings) -> None:
    assert settings.load() == (ConfigState(), 0)


def test_commit_round_trips_and_bumps_the_version(settings: Settings) -> None:
    wanted = ConfigState(
        defaults={"delay_s": 8.0},
        player={"default_channel": "b"},
        groups=("G",),
        channels=(ChannelRow("a", "G", True, doc("a")), ChannelRow("b", None, False, doc("b"))),
    )
    # b is disabled, so it cannot be the default: the whole state is refused.
    with pytest.raises(ConfigError, match="default_channel"):
        settings.commit(wanted, 0, "test")
    wanted = ConfigState(wanted.defaults, {"default_channel": "a"}, wanted.groups, wanted.channels)
    assert settings.commit(wanted, 0, "test") == 1
    assert settings.load() == (wanted, 1)
    assert settings.history_count() == 1


def test_a_stale_version_is_refused_and_changes_nothing(settings: Settings) -> None:
    settings.commit(state("a"), 0, "test")
    with pytest.raises(StaleVersion) as raised:
        settings.commit(state("b"), 0, "test")
    assert raised.value.current == 1
    assert settings.load() == (state("a"), 1)


def test_the_last_enabled_channel_cannot_go(settings: Settings) -> None:
    settings.commit(state("a"), 0, "test")
    with pytest.raises(ConfigError, match="at least one enabled channel"):
        settings.commit(ConfigState(), 1, "test")
    off = ConfigState(channels=(ChannelRow("a", None, False, doc("a")),))
    with pytest.raises(ConfigError, match="at least one enabled channel"):
        settings.commit(off, 1, "test")
    assert settings.load() == (state("a"), 1)


def test_adopt_file_commits_only_a_difference(settings: Settings) -> None:
    assert settings.adopt_file(ConfigState()) is None
    assert settings.adopt_file(state("a", group="G")) == 1
    assert settings.adopt_file(state("a", group="G")) is None
    assert settings.adopt_file(state("a", "b", group="G")) == 2
    assert settings.history_count() == 2


def test_data_survives_reopening(tmp_path: Path) -> None:
    first = Settings(tmp_path / "t.db")
    first.commit(state("a"), 0, "test")
    first.close()
    second = Settings(tmp_path / "t.db")
    assert second.load() == (state("a"), 1)
    second.close()


def test_a_corrupt_file_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(SettingsUnavailable):
        Settings(path)


def test_a_schema_newer_than_this_tunebox_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    Settings(path).close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS) + 1,))
    db.close()
    with pytest.raises(SettingsUnavailable, match="newer"):
        Settings(path)


def test_an_unwritable_folder_is_unavailable(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(SettingsUnavailable):
        Settings(blocker / "t.db")


def test_a_transaction_commits_or_rolls_back(settings: Settings) -> None:
    with settings.transaction() as db:
        db.execute("INSERT INTO secrets VALUES ('a', x'01')")
    with pytest.raises(RuntimeError), settings.transaction() as db:
        db.execute("INSERT INTO secrets VALUES ('b', x'02')")
        raise RuntimeError("stop")
    with settings.read() as db:
        assert db.execute("SELECT key FROM secrets").fetchall() == [("a",)]


def test_a_database_error_in_a_transaction_is_a_write_error(settings: Settings) -> None:
    with pytest.raises(SettingsWriteError), settings.transaction() as db:
        db.execute("INSERT INTO no_such_table VALUES (1)")


def test_a_database_error_while_reading_is_unavailable(settings: Settings) -> None:
    with pytest.raises(SettingsUnavailable), settings.read() as db:
        db.execute("SELECT * FROM no_such_table")


def test_the_account_tables_exist(settings: Settings) -> None:
    with settings.read() as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert {"admin", "recovery_codes", "sessions", "secrets"} <= {row[0] for row in tables}
