import pytest

from tests.fakes import fixture_rows, make_track
from tunebox.matching import Candidate, artists_match, best_match, titles_match


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
