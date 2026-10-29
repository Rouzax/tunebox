import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tests.fakes import COVER_JPG, cover_fix, make_track
from tunebox.coverfiles import CoverFiles, name_for
from tunebox.coverfix import (
    PAGE,
    CoverFixes,
    Fix,
    Listing,
    NoFixes,
    Shown,
    Totals,
    View,
    allowed_image,
    effective,
    is_artist_photo,
    is_photo_key,
    song_list,
)
from tunebox.settings import Settings, SettingsWriteError
from tunebox.store import CoverDecision, HeardSong, Outcome

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
    assert effective(NO_PHOTO, None, None) == Shown(None, "artist", NO_PHOTO)
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
        ("https://evil.com\\.dzcdn.net/x.jpg", False),
        ("https://evil.com%2f.dzcdn.net/x", False),
        ("https://evil.com;.dzcdn.net/x", False),
        ("https://a,b.dzcdn.net/", False),
        ("https://a..dzcdn.net/x", False),
        ("https://.dzcdn.net/x", False),
        ("https://cdn.dzcdn.net:/x", False),
        ("https://cdn.dzcdn.net?x", True),
        ("blob:" + "a" * 64, False),
        ("", False),
    ],
)
def test_the_catalogue_image_rule(link: str, allowed: bool) -> None:
    assert allowed_image(link) is allowed


def link(n: int) -> str:
    return f"https://cdn-images.dzcdn.net/images/cover/{n:032x}/1000x1000-000000-80-0-0.jpg"


URL_A, URL_B, URL_C = link(1), link(2), link(3)
DAY_MS = 86_400_000
NOW_MS = 40 * DAY_MS


def heard(
    artist: str,
    title: str,
    last_at: int,
    cover: CoverDecision | None = None,
    first_at: int | None = None,
) -> HeardSong:
    track = make_track(artist, title, last_at)
    return HeardSong(
        track.key,
        track.photo_key,
        artist,
        track.artists,
        title,
        ("owr",),
        first_at if first_at is not None else last_at,
        last_at,
        cover,
    )


def decision(key: str, outcome: str, url: str | None, match: str = "m") -> CoverDecision:
    return CoverDecision(key, cast("Outcome", outcome), url, match, 1.0)


def listed(
    songs: list[HeardSong],
    fixes: dict[str, Fix] | None = None,
    view: View = "all",
    query: str = "",
    after: tuple[int, str] | None = None,
    limit: int = PAGE,
    files: set[str] | None = None,
    failure: dict[str, str] | None = None,
) -> Listing:
    on_disk: set[str] = files if files is not None else set()
    return song_list(
        songs,
        fixes or {},
        lambda link: f"{name_for(link)}.jpg" if link in on_disk else None,
        lambda key: (failure or {}).get(key),
        NOW_MS,
        view,
        query,
        after,
        limit,
    )


def test_states_of_automatic_answers() -> None:
    cover = heard("A", "Cover", NOW_MS - 1)
    songs = [
        replace(cover, cover=decision(cover.key, "cover", URL_A)),
        heard(
            "B",
            "Photo",
            NOW_MS - 2,
            decision(make_track("B", "Photo").key, "artist", URL_B, "deezer artist: B"),
        ),
        heard("C", "Miss", NOW_MS - 3, decision(make_track("C", "Miss").key, "miss", None)),
        heard("D", "New", NOW_MS - 4),
        heard("東京", "歌", NOW_MS - 5),
        heard("E", "Failed", NOW_MS - 6, decision(make_track("E", "Failed").key, "error", None)),
    ]
    rows = listed(songs, files={URL_A}).rows
    assert [r.state for r in rows] == [
        "cover",
        "artist",
        "nothing",
        "looking",
        "nothing",
        "looking",
    ]
    assert rows[0].image == f"{name_for(URL_A)}.jpg" and rows[1].image is None
    assert listed(songs).totals == Totals(songs=6, cover=1, artist=1, nothing=2, fixed=0)


def test_problems_hold_photos_nothing_and_old_unresolved_songs() -> None:
    young = heard("D", "Young", NOW_MS - 1)
    old = heard("F", "Old", NOW_MS - 2, first_at=NOW_MS - 2 * DAY_MS)
    photo = heard("B", "Photo", NOW_MS - 3, decision(make_track("B", "Photo").key, "artist", URL_B))
    good = heard("A", "Cover", NOW_MS - 4, decision(make_track("A", "Cover").key, "cover", URL_A))
    rows = listed([young, old, photo, good], view="problems").rows
    assert [r.key for r in rows] == [old.key, photo.key]


def test_fix_states_and_the_photo_fix_on_an_artist_answer() -> None:
    fixed = heard("A", "Fixed", NOW_MS - 1)
    fetching = heard("B", "Fetching", NOW_MS - 2)
    failed = heard("C", "Failed", NOW_MS - 3)
    lost = heard("D", "Lost", NOW_MS - 4)
    artist = heard(
        "E",
        "Photo",
        NOW_MS - 5,
        decision(make_track("E", "Photo").key, "artist", URL_B, "deezer artist: E"),
    )
    fixes = {
        fixed.key: cover_fix(fixed.key, URL_A),
        fetching.key: cover_fix(fetching.key, URL_C),
        failed.key: cover_fix(failed.key, URL_C),
        lost.key: cover_fix(lost.key, "blob:" + "a" * 64, "upload"),
        artist.photo_key: cover_fix(artist.photo_key, None, "none"),
    }
    rows = listed(
        [fixed, fetching, failed, lost, artist],
        fixes,
        files={URL_A},
        failure={failed.key: "gone", lost.key: "lost"},
    ).rows
    assert [(r.state, r.reason) for r in rows] == [
        ("fix", None),
        ("fetching", None),
        ("failed", "gone"),
        ("lost", "lost"),
        ("fix", None),
    ]
    assert rows[0].image == f"{name_for(URL_A)}.jpg"
    assert rows[4].fix is not None and rows[4].fix.key == artist.photo_key and rows[4].link is None


def test_the_problems_page_stays_full_after_many_fixed_rows() -> None:
    fixed = [heard("F", f"Fixed {n}", NOW_MS - n) for n in range(150)]
    photos = [
        heard(
            "P",
            f"Photo {n}",
            NOW_MS - 1000 - n,
            decision(make_track("P", f"Photo {n}").key, "artist", URL_B),
        )
        for n in range(120)
    ]
    fixes = {s.key: cover_fix(s.key, URL_A) for s in fixed}
    page = listed(fixed + photos, fixes, view="problems")
    assert len(page.rows) == PAGE and page.next is not None
    rest = listed(fixed + photos, fixes, view="problems", after=page.next)
    assert len(rest.rows) == 20 and rest.next is None


def test_a_fix_whose_song_is_not_heard_is_listed_under_fixed() -> None:
    orphan = Fix(
        "agents of time|hearts collide",
        URL_A,
        "Deezer: Agents of Time - Collide",
        "deezer",
        "AGENTS OF TIME",
        "Hearts Collide",
        5.0,
    )
    photo = Fix("photo:jessica", None, "No photo", "none", "JESSICA", "", 6.0)
    song = heard("JESSICA", "Song", NOW_MS - 1)
    rows = listed([song], {orphan.key: orphan, photo.key: photo}, view="fixed").rows
    assert [(r.key, r.last_at, r.artist, r.title) for r in rows] == [
        ("photo:jessica", NOW_MS - 1, "JESSICA", ""),
        (orphan.key, None, "AGENTS OF TIME", "Hearts Collide"),
    ]
    # Fixed is ordered by the later of last heard and set.
    assert rows[0].sort_at == NOW_MS - 1 and rows[1].sort_at == 5_000


def test_a_heard_fixed_song_under_fixed_sorts_by_when_it_was_set() -> None:
    song = heard("A", "Song", NOW_MS - DAY_MS)
    later = heard("B", "Song", NOW_MS - 1)
    fixes = {
        song.key: cover_fix(song.key, URL_A, set_at=NOW_MS / 1000),
        later.key: cover_fix(later.key, URL_B),
    }
    rows = listed([song, later], fixes, view="fixed", files={URL_A}).rows
    assert [(r.key, r.sort_at, r.last_at) for r in rows] == [
        (song.key, NOW_MS, NOW_MS - DAY_MS),
        (later.key, NOW_MS - 1, NOW_MS - 1),
    ]
    assert rows[0].channels == ("owr",) and rows[0].image == f"{name_for(URL_A)}.jpg"


def test_search_matches_artist_or_title_in_normalized_or_raw_text() -> None:
    songs = [
        heard("Beyoncé", "Halo", NOW_MS - 1),
        heard("FISHER", "Stay", NOW_MS - 2),
        heard("東京", "歌", NOW_MS - 3),
    ]
    assert [r.title for r in listed(songs, query="beyonce").rows] == ["Halo"]
    assert [r.title for r in listed(songs, query="STA").rows] == ["Stay"]
    assert [r.title for r in listed(songs, query="東").rows] == ["歌"]
    assert len(listed(songs, query="  ").rows) == 3


def test_the_cursor_survives_a_key_with_colons_and_bars() -> None:
    # Text without ASCII letters keeps its punctuation in the key
    # ("東:一|歌|一"); ties on last_at page by the key.
    names = ["一", "二", "三", "四", "五"]
    songs = [heard(f"東:{c}", f"歌|{c}", NOW_MS - (n // 2)) for n, c in enumerate(names)]
    assert all(":" in s.key and s.key.count("|") == 2 for s in songs)
    first = listed(songs, limit=2)
    second = listed(songs, limit=2, after=first.next)
    third = listed(songs, limit=2, after=second.next)
    keys = [r.key for r in first.rows + second.rows + third.rows]
    assert sorted(keys) == sorted(s.key for s in songs) and len(set(keys)) == 5
    assert third.next is None


def test_the_list_does_not_touch_access_times(tmp_path: Path) -> None:
    files = CoverFiles(tmp_path, free=lambda: None)
    files.save(URL_A, COVER_JPG)
    path = tmp_path / f"{name_for(URL_A)}.jpg"
    os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    song = heard("A", "Cover", NOW_MS - 1, decision(make_track("A", "Cover").key, "cover", URL_A))

    def image(link: str) -> str | None:
        found = files.found(link)
        return found[0].name if found else None

    listing = song_list([song], {}, image, lambda _k: None, NOW_MS, "all", "", None)
    assert listing.rows[0].image == path.name
    assert path.stat().st_atime_ns == 1_000_000_000


def test_an_error_decision_shows_its_kept_image_while_the_file_is_there() -> None:
    kept = heard(
        "A",
        "Kept",
        NOW_MS - 1,
        decision(make_track("A", "Kept").key, "error", URL_A),
        first_at=NOW_MS - 2 * DAY_MS,
    )
    photo = heard(
        "B",
        "Photo",
        NOW_MS - 2,
        decision(make_track("B", "Photo").key, "error", URL_B, "deezer artist: B"),
        first_at=NOW_MS - 2 * DAY_MS,
    )
    gone = heard(
        "C",
        "Gone",
        NOW_MS - 3,
        decision(make_track("C", "Gone").key, "error", URL_C),
        first_at=NOW_MS - 2 * DAY_MS,
    )
    listing = listed([kept, photo, gone], files={URL_A, URL_B})
    assert [(r.state, r.kind, r.link) for r in listing.rows] == [
        ("cover", "album", URL_A),
        ("artist", "artist", URL_B),
        ("looking", None, None),
    ]
    assert listing.rows[0].image == f"{name_for(URL_A)}.jpg"
    assert listing.totals == Totals(songs=3, cover=1, artist=1, nothing=0, fixed=0)
    problems = listed([kept, photo, gone], files={URL_A, URL_B}, view="problems").rows
    assert [r.key for r in problems] == [photo.key, gone.key]


def test_a_photo_fix_under_fixed_lists_the_stations_of_its_songs() -> None:
    one = replace(heard("JESSICA", "One", NOW_MS - 1), channels=("owr",))
    two = replace(heard("JESSICA", "Two", NOW_MS - 2), channels=("anthems", "owr"))
    photo = Fix(one.photo_key, None, "No photo", "none", "JESSICA", "", 6.0)
    rows = listed([one, two], {photo.key: photo}, view="fixed").rows
    assert [(r.key, r.channels) for r in rows] == [(photo.key, ("anthems", "owr"))]
