import logging
from pathlib import Path

import pytest

from tunebox.coverfix import CoverFixes, Fix, NoFixes
from tunebox.settings import Settings, SettingsWriteError

LINK = "https://cdn-images.dzcdn.net/images/cover/" + "a" * 32 + "/1000x1000-000000-80-0-0.jpg"
BLOB = "blob:" + "b" * 64


def song(value: str = LINK, key: str = "agents of time|hearts collide") -> Fix:
    return Fix(
        key,
        value,
        "Deezer: Agents of Time - Collide",
        "deezer",
        "AGENTS OF TIME",
        "Hearts Collide",
        1000.0,
    )


def test_set_get_and_clear_replace_the_map(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    fixes = CoverFixes(settings)
    assert fixes.get(song().key) is None and fixes.values() == set()
    fixes.set(song())
    fixes.set(Fix("photo:jessica", None, "No photo", "none", "JESSICA", "", 1001.0))
    assert fixes.get(song().key) == song()
    assert fixes.values() == {LINK}
    assert {f.key for f in fixes.all()} == {song().key, "photo:jessica"}
    # Read back by a new map, as at the next start-up.
    assert CoverFixes(settings).get("photo:jessica") is not None
    assert fixes.clear(song().key) == song()
    assert fixes.clear(song().key) is None
    assert fixes.get(song().key) is None
    settings.close()


def test_a_failed_write_leaves_the_map(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    fixes = CoverFixes(settings)
    fixes.set(song())
    with settings.transaction() as db:
        db.execute(
            "CREATE TRIGGER refuse BEFORE INSERT ON cover_fixes"
            " BEGIN SELECT RAISE(ABORT, 'full'); END"
        )
    with pytest.raises(SettingsWriteError):
        fixes.set(song(BLOB))
    assert fixes.get(song().key) == song()
    settings.close()


def test_reading_the_map_never_queries_the_database(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "t.db")
    fixes = CoverFixes(settings)
    fixes.set(song())
    settings.close()
    assert fixes.get(song().key) == song()
    assert fixes.values() == {LINK}
    assert len(fixes.all()) == 1


def test_an_unreadable_table_starts_empty_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(tmp_path / "t.db")
    settings.close()
    with caplog.at_level(logging.ERROR):
        fixes = CoverFixes(settings)
    assert [r.getMessage() for r in caplog.records] == ["cover_fixes_unreadable"]
    assert fixes.get(song().key) is None
    # Unknown, not "none": the daily sweep must not remove fixed images.
    assert fixes.values() is None


def test_no_fixes_reader() -> None:
    assert NoFixes().get("x") is None and NoFixes().values() == set()
