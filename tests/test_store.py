import logging
from pathlib import Path

import pytest

from tests.fakes import make_track
from tunebox.store import CoverDecision, Store

URL = "https://example.invalid/a.jpg"


def test_cover_decisions_round_trip_and_replace(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    assert store.get_cover("fisher|stay") is None
    store.put_cover(CoverDecision("fisher|stay", "miss", None, None, 10.0))
    store.put_cover(CoverDecision("fisher|stay", "cover", URL, "deezer: Fisher - Stay", 20.0))
    assert store.get_cover("fisher|stay") == CoverDecision(
        "fisher|stay", "cover", URL, "deezer: Fisher - Stay", 20.0
    )


def test_recent_lists_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "tunebox.db"
    older = make_track("FISHER", "Stay", 1_000)
    newer = make_track("NOIZU", "Moon Groove (Played by Noizu Library TML 22)", 2_000)
    Store(path).save_recent("owr", [newer, older])
    assert Store(path).load_recent("owr") == [newer, older]
    assert Store(path).load_recent("daybreak") == []


def test_save_recent_replaces_the_channel(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.save_recent("owr", [make_track("FISHER", "Stay", 1_000)])
    store.save_recent("owr", [make_track("JAZZY", "My People", 3_000)])
    assert [t.title for t in store.load_recent("owr")] == ["My People"]


def test_unusable_path_falls_back_to_memory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        store = Store(blocker / "tunebox.db")
    assert [r.getMessage() for r in caplog.records] == ["store_unavailable"]
    store.put_cover(CoverDecision("k", "miss", None, None, 1.0))
    assert store.get_cover("k") is not None


def test_memory_only_store() -> None:
    store = Store(None)
    store.save_recent("owr", [make_track()])
    assert len(store.load_recent("owr")) == 1


def test_close_releases_the_database(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.close()
    store.close()
    assert store.get_cover("k") is None
