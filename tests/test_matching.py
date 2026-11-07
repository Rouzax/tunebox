from dataclasses import replace

import pytest

from tests.fakes import fixture_rows, make_track
from tunebox.matching import (
    OWNER_CHECKS,
    Candidate,
    artists_match,
    best_match,
    compiled,
    lead_artist,
    search_names,
    searchable,
    titles_match,
)


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


def test_search_names_add_the_parts_of_a_joined_name() -> None:
    assert search_names(["AVICII & ALOE BLACC"]) == ("AVICII & ALOE BLACC", "AVICII", "ALOE BLACC")
    assert search_names(["FISHER", "FLORENCE ARMAN"]) == ("FISHER", "FLORENCE ARMAN")
    assert search_names(["Lilly Wood & The Prick", "Robin Schulz"]) == (
        "Lilly Wood & The Prick",
        "Robin Schulz",
        "Lilly Wood",
        "The Prick",
    )
    # "&" without spaces is part of a name.
    assert search_names(["M&M"]) == ("M&M",)


def test_lead_artist_is_the_first_named() -> None:
    assert lead_artist("AVICII & ALOE BLACC") == "AVICII"
    assert lead_artist("Chris Lorenzo & Chromeo") == "Chris Lorenzo"
    assert lead_artist("FISHER") == "FISHER"


def test_a_song_named_like_a_compilation_keeps_its_own_release() -> None:
    own = Candidate("The Rolling Stones", "Mixed Emotions", cover("own"), "Mixed Emotions")
    assert not compiled(own)
    assert not compiled(
        Candidate("The Rolling Stones", "Mixed Emotions", cover("x"), "Steel Wheels")
    )
    hits = Candidate("The Rolling Stones", "Mixed Emotions", cover("hits"), "Greatest Hits")
    found = best_match(("THE ROLLING STONES",), "Mixed Emotions", [hits, own])
    assert found is not None and found.image == cover("own")


def test_searchable_needs_latin_text_in_artist_and_title() -> None:
    assert searchable(make_track("FISHER", "Stay"))
    assert not searchable(make_track("", "E1700 16 Oct 2026 M19 VO"))
    assert not searchable(make_track("FISHER", "(Intro)"))
    assert not searchable(make_track("東京", "Stay"))


# Deezer's own results for "COCA La Noche": Tiësto's mix listed twice, once
# without "mixed" in its title, and a various-artists album.
LA_NOCHE = [
    Candidate("Coca", "La Noche", cover("isos"), "In Search Of Sunrise 4 - Latin America"),
    Candidate(
        "Coca & Villa",
        "La Noche",
        cover("isos-mixed"),
        "In Search of Sunrise 4 (Latin America) mixed by Tiësto",
    ),
    Candidate("Coca & Villa", "La Noche", cover("house"), "House Music"),
    Candidate(
        "Coca & Villa", "La Noche (D-Formation Remix)", cover("own"), "La Noche (2017 Remixes)"
    ),
]
LA_NOCHE_OWNERS = {
    cover("isos"): "Tiësto",
    cover("isos-mixed"): "Tiësto",
    cover("house"): "Varios Artistas",
    cover("own"): "Coca & Villa",
}


def test_an_album_of_someone_elses_looks_compiled() -> None:
    song = Candidate("Coca & Villa", "La Noche", cover("x"), "In Search Of Sunrise 4")
    assert compiled(replace(song, album_artist="Tiësto"))
    assert compiled(replace(song, album_artist="Various Artists"))
    assert not compiled(replace(song, album_artist="Coca & Villa"))
    # The album of one of a duo, or of the duo a song lists one of.
    assert not compiled(replace(song, album_artist="Coca"))
    assert not compiled(replace(song, artist="Coca", album_artist="Coca & Villa"))
    assert not compiled(song)


def test_a_mix_whose_title_hides_it_loses_to_the_own_release() -> None:
    asked: list[str] = []

    def owner(candidate: Candidate) -> str:
        asked.append(candidate.image)
        return LA_NOCHE_OWNERS[candidate.image]

    found = best_match(("COCA & VILLA", "PEPE RUBIO", "COCA", "VILLA"), "La Noche", LA_NOCHE, owner)
    assert found is not None and found.image == cover("own")
    # The mix named as one is never asked about, nor the album named after the song.
    assert asked == [cover("isos"), cover("house")]


def test_without_an_owner_the_catalogue_order_decides() -> None:
    found = best_match(("COCA & VILLA", "COCA"), "La Noche", LA_NOCHE)
    assert found is not None and found.image == cover("isos")


def test_an_unknown_owner_keeps_the_candidate() -> None:
    found = best_match(("COCA & VILLA", "COCA"), "La Noche", LA_NOCHE, lambda _c: "")
    assert found is not None and found.image == cover("isos")


def test_a_song_found_only_on_compilations_keeps_one() -> None:
    found = best_match(
        ("COCA & VILLA", "COCA"), "La Noche", LA_NOCHE[:3], lambda c: LA_NOCHE_OWNERS[c.image]
    )
    # Every album is a compilation: the catalogue's first stays.
    assert found is not None and found.image == cover("isos")


def test_owner_lookups_are_capped() -> None:
    asked: list[Candidate] = []

    def owner(candidate: Candidate) -> str:
        asked.append(candidate)
        return "Various Artists"

    candidates = [Candidate("Fisher", "Stay", cover(f"c{n}"), f"Album {n}") for n in range(6)]
    found = best_match(("FISHER",), "Stay", candidates, owner)
    assert len(asked) == OWNER_CHECKS
    assert found is not None and found.image == cover(f"c{OWNER_CHECKS}")


def test_an_album_named_after_the_song_needs_no_lookup() -> None:
    def owner(_candidate: Candidate) -> str:
        raise AssertionError("looked up")

    for album in ("Stay", "Stay (Remixes)", "Stay - Single"):
        found = best_match(
            ("FISHER",), "Stay", [Candidate("Fisher", "Stay", cover("x"), album)], owner
        )
        assert found is not None
