import logging
import os
from pathlib import Path

import pytest

from tests.fakes import make_track
from tunebox.store import CoverDecision, HeardUnreadable, Store

URL = "https://example.invalid/a.jpg"
PHOTO_URL = "https://example.invalid/p.jpg"


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


def test_prune_covers_removes_old_decisions_only() -> None:
    store = Store(None)
    store.put_cover(CoverDecision("old", "cover", "https://example.invalid/o.jpg", "m", 100.0))
    store.put_cover(CoverDecision("new", "miss", None, None, 900.0))
    assert store.prune_covers(older_than=500.0) == ["old"]
    assert store.get_cover("old") is None
    assert store.get_cover("new") is not None


def test_cover_links_lists_every_stored_link() -> None:
    store = Store(None)
    store.put_cover(CoverDecision("a", "cover", "https://example.invalid/1.jpg", "m", 1.0))
    store.put_cover(CoverDecision("b", "artist", "https://example.invalid/1.jpg", "m", 1.0))
    store.put_cover(CoverDecision("c", "miss", None, None, 1.0))
    assert store.cover_links() == {"https://example.invalid/1.jpg"}


def test_cover_links_are_unknown_on_the_in_memory_fallback(tmp_path: Path) -> None:
    store = Store(tmp_path)  # a directory cannot be opened as a database
    assert store.degraded and store.cover_links() is None


def test_cover_links_are_unknown_when_the_read_fails() -> None:
    store = Store(None)
    store.close()
    assert store.cover_links() is None


def test_prune_is_empty_when_the_delete_fails() -> None:
    store = Store(None)
    store.close()
    assert store.prune_covers(1.0) == []


def test_links_are_unknown_once_a_write_has_failed() -> None:
    store = Store(None)
    store.put_cover(CoverDecision("a", "cover", URL, "m", 1.0))
    assert store.cover_links() == {URL}
    store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "CREATE TRIGGER refuse BEFORE INSERT ON covers BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    store.put_cover(CoverDecision("b", "cover", "https://example.invalid/b.jpg", "m", 1.0))
    assert store.get_cover("b") is None
    assert store.cover_links() is None


def test_heard_keeps_the_first_and_moves_the_last_play(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.record_heard("owr", [make_track("FISHER", "Stay", 1_000)])
    store.record_heard("owr", [make_track("FISHER", "Stay", 5_000)])
    store.record_heard("anthems", [make_track("FISHER", "Stay", 3_000)])
    store.put_cover(CoverDecision("fisher|stay", "cover", URL, "m", 1.0))
    (song,) = store.heard_songs(0) or []
    assert (song.key, song.first_at, song.last_at) == ("fisher|stay", 1_000, 5_000)
    assert song.channels == ("anthems", "owr")
    assert song.photo_key == "photo:fisher" and song.artists == ("FISHER",)
    assert song.cover == CoverDecision("fisher|stay", "cover", URL, "m", 1.0)
    assert song.photo is None
    photo = CoverDecision("photo:fisher", "artist", PHOTO_URL, "deezer artist: FISHER", 2.0)
    store.put_cover(photo)
    (song,) = store.heard_songs(0) or []
    assert song.photo == photo


def test_heard_songs_take_the_newest_row_text_and_the_window(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.record_heard("owr", [make_track("FISHER", "Stay", 1_000)])
    store.record_heard("anthems", [make_track("Fisher", "Stay", 9_000)])
    store.record_heard("owr", [make_track("JAZZY", "My People", 100)])
    songs = store.heard_songs(500) or []
    assert [(s.key, s.artist, s.cover) for s in songs] == [("fisher|stay", "Fisher", None)]


def test_prune_heard_by_last_play(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.record_heard("owr", [make_track("FISHER", "Stay", 1_000), make_track("A", "B", 9_000)])
    assert store.prune_heard(5_000) == 1
    assert [s.key for s in store.heard_songs(0) or []] == ["a|b"]


def test_the_list_is_unknown_on_the_in_memory_store() -> None:
    store = Store(None)
    store.record_heard("owr", [make_track()])
    assert store.heard_songs(0) is None


def test_the_list_reads_beside_a_held_store_lock(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.record_heard("owr", [make_track()])
    # A player lookup holding the store's lock must not stall the panel.
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        assert [s.key for s in store.heard_songs(0) or []] == ["fisher|stay"]


def test_a_failed_heard_write_leaves_the_links_known(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.put_cover(CoverDecision("a", "cover", URL, "m", 1.0))
    store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "CREATE TRIGGER refuse BEFORE INSERT ON heard BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    with caplog.at_level(logging.ERROR):
        store.record_heard("owr", [make_track()])
    assert [r.getMessage() for r in caplog.records] == ["store_unavailable"]
    assert store.cover_links() == {URL}
    assert store.heard_songs(0) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file without permissions")
def test_an_unreadable_list_is_its_own_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "tunebox.db"
    store = Store(path)
    store.record_heard("owr", [make_track()])
    path.chmod(0)
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(HeardUnreadable):
            store.heard_songs(0)
    finally:
        path.chmod(0o600)
    assert [r.getMessage() for r in caplog.records] == ["cover_list_unreadable"]
    assert caplog.records[0].levelno == logging.WARNING
    # The store's own one-shot report is still there for its own connection.
    caplog.clear()
    store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "CREATE TRIGGER refuse BEFORE INSERT ON heard BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    with caplog.at_level(logging.ERROR):
        store.record_heard("owr", [make_track("A", "B")])
    assert [r.getMessage() for r in caplog.records] == ["store_unavailable"]


def test_delete_cover_and_heard_track(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    store.put_cover(CoverDecision("fisher|stay", "cover", URL, "m", 1.0))
    store.delete_cover("fisher|stay")
    assert store.get_cover("fisher|stay") is None
    assert store.heard_track("fisher|stay") is None
    store.record_heard("owr", [make_track("FISHER", "Stay", 1_000)])
    store.record_heard("owr", [make_track("FISHER", "Losing It", 2_000)])
    track = store.heard_track("fisher|stay")
    assert track is not None and (track.key, track.artists, track.title) == (
        "fisher|stay",
        ("FISHER",),
        "Stay",
    )
    photo = store.heard_track("photo:fisher")
    assert photo is not None and photo.key == "photo:fisher" and photo.title == "Losing It"


def test_a_photo_key_lookup_uses_its_index(tmp_path: Path) -> None:
    store = Store(tmp_path / "tunebox.db")
    db = store._db  # pyright: ignore[reportPrivateUsage]
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "heard_photo" in names
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT artist, artists, title, last_at FROM heard"
        " WHERE photo_key = ? ORDER BY last_at DESC LIMIT 1",
        ("photo:fisher",),
    ).fetchall()
    assert any("heard_photo" in str(row[-1]) for row in plan)
