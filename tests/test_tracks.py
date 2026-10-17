from tests.fakes import RULES, fixture_rows, make_track
from tunebox.tracks import RawEntry, Rules, clean, norm


def test_norm_drops_accents_apostrophes_and_punctuation() -> None:
    assert norm("Somebody's Story") == "somebodys story"
    assert norm("Kölsch") == "kolsch"
    assert norm("Dance Dance Dance!") == "dance dance dance"
    assert norm("  X CLUB.  ") == "x club"


def test_fixture_station_ids_are_recognised() -> None:
    for row in fixture_rows():
        track = clean(RawEntry(row.artist, row.title, 1), RULES, 1)
        assert track.kind == ("station_id" if not row.artist else "track"), row


def test_fixture_played_by_is_split_off() -> None:
    rows = [row for row in fixture_rows() if "(Played by" in row.title]
    assert len(rows) >= 15
    for row in rows:
        track = clean(RawEntry(row.artist, row.title, 1), RULES, 1)
        assert track.played_by, row
        assert "Played by" not in track.title, row
        assert track.full_title == row.title


def test_fixture_artists_are_never_empty_or_trailing() -> None:
    for row in fixture_rows():
        track = clean(RawEntry(row.artist, row.title, 1), RULES, 1)
        assert all(track.artists), row
        assert not track.artist.endswith(","), row


def test_trailing_comma_is_removed() -> None:
    track = make_track("BEDOUIN, IVETA MUKUCHYAN,", "Make Me Feel")
    assert track.artist == "BEDOUIN, IVETA MUKUCHYAN"
    assert track.artists == ("BEDOUIN", "IVETA MUKUCHYAN")


def test_ampersand_stays_inside_one_name() -> None:
    track = make_track("ROBIN SCHULZ, LILLY WOOD & THE PRICK", "Prayer In C (Robin Schulz Remix)")
    assert track.artists == ("ROBIN SCHULZ", "LILLY WOOD & THE PRICK")
    assert track.title == "Prayer In C (Robin Schulz Remix)"


def test_played_by_without_a_space_before_it() -> None:
    track = make_track(
        "TODD TERJE", "Inspector Norse (Westend Edit)(Played by Disco Dom Orbyz TLFR 26)"
    )
    assert track.title == "Inspector Norse (Westend Edit)"
    assert track.played_by == "Disco Dom Orbyz TLFR 26"


def test_neutral_rules_treat_everything_as_a_track() -> None:
    track = clean(RawEntry("", "E1700 16 Oct 2026 M19 VO", 5), Rules(), 5)
    assert track.kind == "track"
    assert track.played_by is None


def test_missing_start_time_uses_the_time_seen() -> None:
    track = clean(RawEntry("FISHER", "Stay", None), RULES, 42)
    assert track.started_at == 42


def test_key_and_ident() -> None:
    track = make_track("SOLOMUN", "Somebodys Story", 7)
    assert track.key == "solomun|somebodys story"
    assert track.ident == (7, "solomun|somebodys story")
