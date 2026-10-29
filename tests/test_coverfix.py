import logging
from pathlib import Path

import pytest

from tunebox.coverfix import (
    CoverFixes,
    Fix,
    NoFixes,
    Shown,
    allowed_image,
    effective,
    is_artist_photo,
    is_photo_key,
)
from tunebox.settings import Settings, SettingsWriteError
from tunebox.store import CoverDecision

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


AUTO = CoverDecision("k", "cover", "https://example.invalid/a.jpg", "deezer: A - B", 1.0)
ARTIST = CoverDecision("k", "artist", "https://example.invalid/p.jpg", "deezer artist: A", 1.0)
OLD_ARTIST = CoverDecision("k", "error", "https://example.invalid/p.jpg", "deezer artist: A", 1.0)
MISS = CoverDecision("k", "miss", None, None, 1.0)
PHOTO = Fix("photo:a", LINK, "Deezer artist: A", "deezer", "A", "", 2.0)
NO_PHOTO = Fix("photo:a", None, "No photo", "none", "A", "", 2.0)


def test_a_cover_fix_wins_over_any_automatic_answer() -> None:
    fixed = song()
    shown = effective(fixed, AUTO, PHOTO)
    assert shown is not None and (shown.url, shown.kind, shown.fix) == (LINK, "album", fixed)


def test_a_photo_fix_shows_for_a_photo_key_and_behind_an_artist_answer() -> None:
    assert effective(PHOTO, None, None) == Shown(LINK, "artist", PHOTO)
    # A song whose automatic answer is an artist photo shows the fixed photo,
    # as an artist image, so /now sends no second side.
    assert effective(None, ARTIST, PHOTO) == Shown(LINK, "artist", PHOTO)
    assert effective(None, OLD_ARTIST, PHOTO) == Shown(LINK, "artist", PHOTO)
    assert effective(None, ARTIST, NO_PHOTO) == Shown(None, "artist", NO_PHOTO)


def test_a_photo_fix_does_not_touch_a_song_with_its_own_cover_or_none() -> None:
    assert effective(None, AUTO, PHOTO) == Shown(AUTO.url, "album")
    assert effective(None, MISS, PHOTO) == Shown(None, "album")
    assert effective(None, None, PHOTO) is None


def test_the_automatic_answer_without_fixes() -> None:
    assert effective(None, ARTIST, None) == Shown(ARTIST.url, "artist")
    assert effective(None, AUTO, None) == Shown(AUTO.url, "album")


def test_kinds_by_key_and_match() -> None:
    assert is_photo_key("photo:fisher") and not is_photo_key("fisher|stay")
    assert is_artist_photo(ARTIST) and is_artist_photo(OLD_ARTIST)
    assert not is_artist_photo(AUTO)


@pytest.mark.parametrize(
    ("link", "allowed"),
    [
        (LINK, True),
        ("https://is1-ssl.mzstatic.com/image/thumb/x/1000x1000bb.jpg", True),
        ("https://dzcdn.net/x.jpg", True),
        ("HTTPS://cdn-images.DZCDN.net/x.jpg", True),
        ("http://cdn-images.dzcdn.net/x.jpg", False),
        ("https://dzcdn.net.example.invalid/x.jpg", False),
        ("https://evildzcdn.net/x.jpg", False),
        ("https://cdn-images.dzcdn.net:8443/x.jpg", False),
        ("https://user@cdn-images.dzcdn.net/x.jpg", False),
        ("https://cdn-images.dzcdn.net:bad/x.jpg", False),
        ("https://cdn-images.dzcdn.net/x y.jpg", False),
        ("blob:" + "a" * 64, False),
        ("", False),
    ],
)
def test_the_catalogue_image_rule(link: str, allowed: bool) -> None:
    assert allowed_image(link) is allowed
