import pytest

from tests.fakes import OWR_RULES, RULES, fixture_rows, make_track, owr_top30
from tunebox.tracks import MAX_TITLE, RawEntry, Rules, clean, norm, reclean


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


def test_non_latin_names_keep_distinct_keys() -> None:
    first = make_track("ЛСП", "Монетка")
    second = make_track("ЛЕНИНГРАД", "Лабутены")
    assert first.key != second.key
    assert first.key != "|"


def test_titles_are_cut_before_rules_run() -> None:
    track = clean(RawEntry("A", "x" * 1000, 1), Rules(), 1)
    assert len(track.full_title) == MAX_TITLE


@pytest.mark.parametrize(
    ("sent", "shown"),
    [
        # UTF-8 read as Windows-1252 somewhere upstream (Radio 538).
        ("TI\u00c3\u2039STO & AVA MAX", "TI\u00cbSTO & AVA MAX"),
        ("AD\u00c3\u2030LA", "AD\u00c9LA"),
        # Bytes Windows-1252 leaves undefined arrive as control characters.
        ("\u00c3\u0081NGEL", "\u00c1NGEL"),
        # Correct text stays: it does not decode as UTF-8 a second time.
        ("MEES SALOM\u00c9", "MEES SALOM\u00c9"),
        ("S\u00c3O PAULO", "S\u00c3O PAULO"),
        ("BEYONC\u00c9", "BEYONC\u00c9"),
    ],
)
def test_text_encoded_twice_is_repaired(sent: str, shown: str) -> None:
    track = make_track(sent, sent)
    assert track.artist == shown
    assert track.title == shown


def test_owr_chart_positions_become_badges() -> None:
    by_title = {e.title: clean(e, OWR_RULES, 1) for e in owr_top30()}
    sad = by_title["#8 Sad Girls"]
    assert (sad.badge, sad.title, sad.full_title) == ("#8", "Sad Girls", "#8 Sad Girls")
    assert sad.key == make_track("DAVID GUETTA, BEBE REXHA", "Sad Girls").key
    positions = [t for t in by_title.values() if t.full_title.startswith("#")]
    assert len(positions) == 23
    assert all(t.badge == t.full_title.split()[0] for t in positions)
    assert all(not t.title.startswith("#") for t in positions)


def test_owr_bracketed_labels_become_badges() -> None:
    by_title = {e.title: clean(e, OWR_RULES, 1) for e in owr_top30()}
    jungle = by_title["Jungle [Top 30 Throwback]"]
    assert (jungle.badge, jungle.title) == ("Top 30 Throwback", "Jungle")
    last = by_title["Happiness Is So Sad [Last Week's #1]"]
    assert (last.badge, last.title) == ("Last Week's #1", "Happiness Is So Sad")


def test_owr_recaps_and_chart_hour_ids_are_station_ids() -> None:
    ids = [clean(e, OWR_RULES, 1) for e in owr_top30() if not e.artist]
    assert len(ids) == 7
    assert all(t.kind == "station_id" for t in ids)
    assert all(t.kind == "track" for e in owr_top30() if e.artist for t in [clean(e, OWR_RULES, 1)])


def test_other_titles_keep_no_badge() -> None:
    for entry in owr_top30():
        track = clean(entry, OWR_RULES, 1)
        if not entry.title.startswith("#") and "[" not in entry.title:
            assert track.badge is None, entry
            assert track.title == clean(entry, RULES, 1).title, entry


def test_a_badge_and_played_by_together() -> None:
    raw = RawEntry("ALEX WANN", "#3 Set Me Free (Played by Alex Wann)", 1)
    track = clean(raw, OWR_RULES, 1)
    assert (track.badge, track.title, track.played_by) == ("#3", "Set Me Free", "Alex Wann")


def test_a_title_that_is_only_a_badge_stays_whole() -> None:
    track = clean(RawEntry("X", "[Top 30 Throwback]", 1), OWR_RULES, 1)
    assert (track.badge, track.title) == (None, "[Top 30 Throwback]")


def test_reclean_reads_a_track_with_new_rules() -> None:
    old = clean(RawEntry("DAVID GUETTA, BEBE REXHA,", "#8 Sad Girls", 5), RULES, 5)
    assert old.badge is None
    new = reclean(old, OWR_RULES)
    assert (new.badge, new.title, new.started_at) == ("#8", "Sad Girls", 5)
    assert new.artists == old.artists
    assert reclean(new, RULES) == old
    jingle = clean(RawEntry("", "RECAP 20-11", 5), RULES, 5)
    assert jingle.kind == "track"
    assert reclean(jingle, OWR_RULES).kind == "station_id"
