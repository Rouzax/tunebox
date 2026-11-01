import contextlib
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from tests.fakes import make_track
from tunebox.store import CoverDecision, HealthRow, HealthRun, HeardUnreadable, Store, transition

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


def row(ok: bool, at: int, target: str = "https://example.invalid/a", **kw: object) -> HealthRow:
    fields: dict[str, object] = {
        "channel": "owr",
        "item": "stream:AAC",
        "target": target,
        "checked_at": at,
        "ok": ok,
        "reason": None if ok else "unreachable",
        "codec": "aac" if ok else None,
        "kbps": 64 if ok else None,
        "failing_since": None if ok else at,
    }
    fields.update(kw)
    return HealthRow(**fields)  # type: ignore[arg-type]


def test_failing_since_is_kept_set_and_cleared(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    assert store.health_write(row(True, 1)).change is None
    assert store.health_write(row(False, 10)).change == "failing"
    again = store.health_write(row(False, 20))
    assert again.change is None
    (stored,) = store.health_rows() or []
    assert stored.failing_since == 10 and stored.checked_at == 20
    back = store.health_write(row(True, 30))
    assert back.change == "recovered" and back.since == 10
    (stored,) = store.health_rows() or []
    assert stored.failing_since is None


def test_a_new_target_starts_over(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.health_write(row(False, 10))
    moved = store.health_write(row(False, 50, target="https://example.invalid/b"))
    assert moved.change == "failing"
    (stored,) = store.health_rows() or []
    assert stored.failing_since == 50
    # Working at a new address is no recovery of the old one.
    assert store.health_write(row(True, 60, target="https://example.invalid/c")).change is None


def test_transition_without_the_database() -> None:
    assert transition(None, row(False, 5)) == ("failing", 5)
    assert transition(row(False, 5), row(False, 9)) == (None, 5)
    assert transition(row(False, 5), row(True, 9)) == ("recovered", None)


def test_prune_keeps_only_current_items(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.health_write(row(True, 1))
    store.health_write(row(True, 1, item="feed"))
    store.health_write(row(True, 1, channel="gone"))
    store.health_prune({("owr", "feed")})
    assert [(r.channel, r.item) for r in store.health_rows() or []] == [("owr", "feed")]


def test_a_start_keeps_the_last_finish(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    assert store.health_run() == HealthRun()
    store.health_run_started(100)
    store.health_run_finished(200, 100, 3, "ok")
    store.health_run_started(300)
    assert store.health_run() == HealthRun(300, 200, 100, 3, "ok")


def test_only_the_stores_own_older_health_tables_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    others = ("health_ignored", "health_settings", "Healthcheck_mutes", "Health_x")
    with contextlib.closing(sqlite3.connect(path)) as db:
        for name in (*others, "health_v0", "health_run_v0"):
            db.execute(f'CREATE TABLE "{name}" (channel TEXT, x INTEGER)')
        db.commit()
    store = Store(path)
    store.health_write(row(True, 1))
    assert len(store.health_rows() or []) == 1
    store.close()
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert "health_v0" not in names and "health_run_v0" not in names
    assert set(others) <= names


def test_a_failed_health_write_does_not_mark_cover_decisions_unknown(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store._db.execute("DROP TABLE health_v1")  # pyright: ignore[reportPrivateUsage]
    assert store.health_write(row(True, 1)).change == "write_failed"
    assert store.health_rows() is None
    assert store.cover_links() is not None


def test_health_lives_in_memory_without_a_file() -> None:
    store = Store(None)
    store.health_write(row(True, 1))
    assert len(store.health_rows() or []) == 1
