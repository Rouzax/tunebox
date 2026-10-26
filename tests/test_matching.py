import pytest

from tests.fakes import fixture_rows, make_track
from tunebox.matching import Candidate, artists_match, best_match, compiled, titles_match


def recorded_matches() -> list[tuple[str, str, str, str, bool]]:
    """(artist, title, found artist, found title, should match) from the fixture."""
    cases: list[tuple[str, str, str, str, bool]] = []
    for row in fixture_rows():
        if not row.match:
            continue
        _, found = row.match.split(": ", 1)
        found_artist, found_title = found.split(" - ", 1)
        cases.append((row.artist, row.title, found_artist, found_title, row.outcome != "WRONG"))
    return cases


@pytest.mark.parametrize(
    ("artist", "title", "found_artist", "found_title", "expected"), recorded_matches()
)
def test_recorded_catalogue_matches(
    artist: str, title: str, found_artist: str, found_title: str, expected: bool
) -> None:
    track = make_track(artist, title)
    result = titles_match(track.title, found_title) and artists_match(track.artists, found_artist)
    assert result is expected


def test_the_known_false_match_is_in_the_fixture() -> None:
    assert any(not case[4] for case in recorded_matches())


def test_short_prefix_is_rejected_long_prefix_kept() -> None:
    assert not titles_match("Running", "Running Up That Hill")
    assert titles_match("It's Not Right (But It's Ok)", "It's Not Right But It's Okay")


def test_prefix_must_end_on_a_word() -> None:
    assert not titles_match("Stay", "Staying Alive")


def test_empty_titles_never_match() -> None:
    assert not titles_match("", "Stay")
    assert not titles_match("(Intro)", "Stay")


def test_artist_must_match_on_word_boundaries() -> None:
    assert artists_match(("MK",), "MK")
    assert not artists_match(("MK",), "Mike Mago")
    assert artists_match(("AJNA",), "Ajna (BE)")
    assert not artists_match(("",), "Fisher")


def test_best_match_prefers_the_exact_version() -> None:
    candidates = [
        Candidate(
            "Bedouin", "Make Me Feel (Notre Dame Remix)", "https://example.invalid/remix.jpg"
        ),
        Candidate("Bedouin", "Make Me Feel", "https://example.invalid/original.jpg"),
    ]
    found = best_match(("BEDOUIN",), "Make Me Feel", candidates)
    assert found is not None
    assert found.image.endswith("original.jpg")


def test_best_match_skips_candidates_without_an_image() -> None:
    assert best_match(("FISHER",), "Stay", [Candidate("Fisher", "Stay", "")]) is None


def cover(name: str) -> str:
    return f"https://example.invalid/{name}.jpg"


@pytest.mark.parametrize(
    ("artists", "title", "candidates", "chosen"),
    [
        # Each list is in the order Deezer gave it for the song on the radio.
        (
            ("MORY KANTE",),
            "Yeke Yeke (ID Remix)",
            [
                Candidate(
                    "Mory Kanté", "Yeke Yeke (Hardfloor Edit)", cover("rave"), "Rave Classics"
                ),
                Candidate("Mory Kanté", "Yeke Yeke", cover("akwaba"), "Akwaba Beach"),
            ],
            "akwaba",
        ),
        (
            ("TCHAMI",),
            "Adieu",
            [
                Candidate(
                    "Tchami", "Adieu (Original Mix)", cover("poolside"), "Poolside Miami 2017"
                ),
                Candidate("Tchami", "Adieu (Radio Edit)", cover("single"), "Adieu (Radio Edit)"),
            ],
            "single",
        ),
        (
            ("KASKADE",),
            "Move For Me",
            [
                Candidate(
                    "Kaskade",
                    "Move for Me (Extended Mix)",
                    cover("umf"),
                    "Ultra Music Festival 2008",
                ),
                Candidate(
                    "Kaskade", "Move For Me (Santiago & Bushido Mix)", cover("grand"), "The Grand"
                ),
            ],
            "grand",
        ),
        (
            ("SIAN EVANS", "TINLICKER"),
            "Hide U (Tinlicker Remix)",
            [
                Candidate(
                    "Sian Evans",
                    "Hide U (WYM366) (Tinlicker Mix)",
                    cover("wym"),
                    "Wake Your Mind Radio 366",
                ),
                Candidate(
                    "Sian Evans",
                    "Hide U (Mixed) (Tinlicker Remix)",
                    cover("armada"),
                    "Armada Music presents Summer Hits 2021 (Mini Mix)",
                ),
            ],
            "wym",
        ),
    ],
)
def test_best_match_prefers_the_songs_own_release(
    artists: tuple[str, ...], title: str, candidates: list[Candidate], chosen: str
) -> None:
    found = best_match(artists, title, candidates)
    assert found is not None and found.image == cover(chosen)


def test_an_exact_title_on_a_compilation_loses_to_the_own_release() -> None:
    candidates = [
        Candidate("Fisher", "Stay", cover("mix"), "Ultimate House Vol.1"),
        Candidate("Fisher", "Stay (Radio Edit)", cover("single"), "Stay"),
    ]
    found = best_match(("FISHER",), "Stay", candidates)
    assert found is not None and found.image == cover("single")


@pytest.mark.parametrize(
    "album",
    [
        "After Hours",
        "Settle (Special Edition)",
        "Boys for Pele (Deluxe Edition)",
        "Summer Of Love",
        "1989",
        "Hitsville",
        "Radio Gaga",
    ],
)
def test_own_releases_do_not_look_compiled(album: str) -> None:
    assert not compiled(Candidate("Artist", "Song", cover("x"), album))


@pytest.mark.parametrize(
    ("album", "title"),
    [
        ("Ultimate House Vol.1", "Big Love (Mixed by DJ Disciple)"),
        ("Wake Your Mind Radio 366", "Hide U (WYM366) (Tinlicker Mix)"),
        ("Poolside Miami 2017", "Adieu (Original Mix)"),
        ("Ultra Music Festival 2008", "Move for Me (Extended Mix)"),
        ("Rave Classics", "Yeke Yeke (Hardfloor Edit)"),
        ("Gok Wan Presents Isolation Nation, Vol. 2", "Big Love"),
        ("ADE - Armada Night 2011 (The Compilation)", "Hide U (Radio Edit)"),
        ("Best Of", "Yeke Yeke (Short Mix)"),
    ],
)
def test_mixes_compilations_and_shows_look_compiled(album: str, title: str) -> None:
    assert compiled(Candidate("Artist", title, cover("x"), album))
