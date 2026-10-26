import dataclasses
import json
import logging
import re
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tunebox.config import ChannelRow, ConfigError, ConfigState
from tunebox.settings import (
    MIGRATIONS,
    HistoryEntry,
    Settings,
    SettingsUnavailable,
    SettingsWriteError,
    StaleVersion,
    state_from_dict,
    state_to_json,
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


def test_version_is_0_when_new_and_moves_with_every_commit(settings: Settings) -> None:
    assert settings.version() == 0
    state = ConfigState(channels=(ChannelRow("a", None, True, doc("a")),))
    assert settings.commit(state, 0, "test") == 1
    assert settings.version() == 1


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
    assert len(settings.history(None)[0]) == 1


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
    assert len(settings.history(None)[0]) == 2


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


def test_state_round_trips_through_json() -> None:
    wanted = ConfigState(
        defaults={"delay_s": 8.0},
        player={"default_channel": "a"},
        groups=("G",),
        channels=(ChannelRow("a", "G", True, doc("a")), ChannelRow("b", None, False, doc("b"))),
    )
    assert state_from_dict(json.loads(state_to_json(wanted))) == wanted


MALFORMED: list[tuple[object, str]] = [
    ([], "state: expected an object"),
    ({"channels": {}}, "channels: expected a list"),
    ({"channels": [5]}, "channels[0]: expected an object"),
    ({"channels": [{"id": 1, "group": None, "enabled": True, "doc": {}}]}, "channels[0].id"),
    (
        {"channels": [{"id": "a", "group": None, "enabled": "yes", "doc": {}}]},
        "channels[0].enabled",
    ),
    ({"channels": [{"id": "a", "group": 3, "enabled": True, "doc": {}}]}, "channels[0].group"),
    ({"channels": [{"id": "a", "group": None, "enabled": True, "doc": []}]}, "channels[0].doc"),
    (
        {"channels": [{"id": "a", "group": None, "enabled": True, "doc": {"group": "Pop"}}]},
        "channels[0].doc.group",
    ),
    ({"groups": [1]}, "groups[0]"),
    ({"groups": {}}, "groups: expected a list"),
    ({"defaults": []}, "defaults"),
    ({"player": []}, "player"),
]


@pytest.mark.parametrize(("data", "message"), MALFORMED)
def test_a_malformed_state_names_the_path(data: object, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        state_from_dict(data)


def test_import_once_only_fills_an_empty_database(settings: Settings) -> None:
    assert settings.import_once(ConfigState()) is None
    assert settings.import_once(state("a")) == 1
    assert settings.import_once(state("a", "b")) is None
    assert settings.load() == (state("a"), 1)


def test_history_pages_newest_first(settings: Settings) -> None:
    version = 0
    for index in range(5):
        ids = [f"c{i}" for i in range(index + 1)]
        version = settings.commit(state(*ids), version, "edit", f"t{index}")
    entries, more = settings.history(limit=2)
    assert [e.target for e in entries] == ["t4", "t3"]
    assert more
    entries, more = settings.history(before=entries[-1].id, limit=10)
    assert [e.target for e in entries] == ["t2", "t1", "t0"]
    assert not more
    found = settings.history_entry(entries[0].id)
    assert found is not None
    entry, before, after = found
    assert entry == HistoryEntry(entries[0].id, 1000.0, "edit", "t2")
    assert [c.id for c in before.channels] == ["c0", "c1"]
    assert [c.id for c in after.channels] == ["c0", "c1", "c2"]
    assert settings.history_entry(9999) is None


def test_schema_3_adds_the_pin_table_and_the_reauth_column(settings: Settings) -> None:
    with settings.read() as db:
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        version = db.execute("SELECT version FROM schema_version").fetchone()[0]
    assert "listener_pin" in tables
    assert "reauth_at" in columns
    assert version == len(MIGRATIONS) == 3


def test_an_unchanged_state_is_not_stored_again(settings: Settings) -> None:
    first = settings.commit(state("a"), 0, "edit")
    again = settings.commit(state("a"), first, "edit")
    assert again == first
    assert settings.commit_changed(state("a"), first, "edit") == (first, False)
    assert len(settings.history(None)[0]) == 1


def test_each_migration_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    Settings(tmp_path / "new.db").close()
    fields = [r.__dict__["fields"] for r in caplog.records if r.getMessage() == "migration_applied"]
    assert fields == [
        {"database": "settings", "version": number} for number in range(1, len(MIGRATIONS) + 1)
    ]


def with_logo(base: ConfigState, ref: str) -> ConfigState:
    first = base.channels[0]
    return ConfigState(
        groups=base.groups,
        channels=(ChannelRow(first.id, first.group, first.enabled, {**first.doc, "logo": ref}),),
    )


def test_referenced_blobs_include_stored_stations_and_history(settings: Settings) -> None:
    first = "blob:" + "a" * 64
    second = "blob:" + "b" * 64
    assert settings.referenced_blobs() == set()
    version = settings.commit(state("owr"), 0, "edit")
    version = settings.commit(with_logo(state("owr"), first), version, "edit")
    settings.commit(with_logo(state("owr"), second), version, "edit")
    assert settings.referenced_blobs() == {first, second}


def test_referenced_blobs_include_group_images(settings: Settings) -> None:
    ref = "blob:" + "c" * 64
    settings.commit(dataclasses.replace(state("a", group="G"), group_images={"G": ref}), 0, "save")
    assert ref in settings.referenced_blobs()


def test_oldest_change_is_the_first_history_entry(settings: Settings) -> None:
    assert settings.oldest_change() is None
    version = settings.commit(state("a"), 0, "edit")
    settings.commit(state("a", "b"), version, "edit")
    assert settings.oldest_change() == 1000.0


def test_group_images_are_stored_and_read_back(settings: Settings) -> None:
    wanted = dataclasses.replace(state("a", group="G"), group_images={"G": "https://x.test/g.png"})
    settings.commit(wanted, 0, "save")
    assert settings.load()[0].group_images == {"G": "https://x.test/g.png"}


def test_a_database_without_the_row_reads_no_images(settings: Settings) -> None:
    settings.commit(state("a"), 0, "save")
    # A database written before group images has no such settings row.
    with settings._db:  # pyright: ignore[reportPrivateUsage]
        settings._db.execute("DELETE FROM settings WHERE key = 'group_images'")  # pyright: ignore[reportPrivateUsage]
    assert settings.load()[0].group_images == {}


def test_state_from_dict_reads_group_images_and_defaults_them() -> None:
    assert state_from_dict({"groups": [], "channels": []}).group_images == {}
    given = {"groups": ["G"], "channels": [], "group_images": {"G": "https://x.test/g.png"}}
    assert state_from_dict(given).group_images == {"G": "https://x.test/g.png"}
    with pytest.raises(ConfigError, match="group_images: expected an object of strings"):
        state_from_dict({"groups": [], "channels": [], "group_images": {"G": 3}})
    with pytest.raises(ConfigError, match="group_images: expected an object of strings"):
        state_from_dict({"groups": [], "channels": [], "group_images": []})
