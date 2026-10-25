import sqlite3
import threading
from datetime import UTC
from pathlib import Path
from typing import cast

import pytest

from tunebox import usagedb as u
from tunebox.migrate import SchemaTooNew
from tunebox.usagedb import SessionRow, VisitRow

ID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
AGENT = "Chrome 120, Linux"
DAY0 = 1_792_800_000.0  # 2026-10-24 00:00 UTC


@pytest.fixture
def db(tmp_path: Path):
    conn = u.open_writer(tmp_path / "usage.db")
    yield conn
    conn.close()


def session(
    cookie: str | None = ID, start: float = DAY0 + 100, last: float = DAY0 + 700
) -> SessionRow:
    return SessionRow(None, cookie, "owr", "Default", start, last, AGENT, None)


def sessions_of(page: dict[str, object] | None) -> list[dict[str, object]]:
    assert page is not None
    return cast(list[dict[str, object]], page["sessions"])


def test_the_writer_turns_on_secure_delete_and_wal(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    conn = u.open_writer(tmp_path / "usage.db")
    conn.execute("UPDATE schema_version SET version = 99")
    conn.close()
    with pytest.raises(SchemaTooNew):
        u.open_writer(tmp_path / "usage.db")


def test_a_visit_creates_the_device_and_a_session_of_a_known_id_is_stored_under_it(
    db: sqlite3.Connection,
) -> None:
    u.write_batch(db, [VisitRow(DAY0 + 50, ID, AGENT, None)], [], set(), UTC, DAY0 + 60)
    s = session()
    written = u.write_batch(db, [], [s], set(), UTC, DAY0 + 800)
    rowid, device = written.rowids[id(s)]
    assert device == ID
    assert db.execute("SELECT played FROM devices WHERE id = ?", (ID,)).fetchone()[0] == 1
    assert db.execute("SELECT device FROM listens WHERE id = ?", (rowid,)).fetchone()[0] == ID


def test_a_session_of_an_unknown_id_is_stored_unidentified(db: sqlite3.Connection) -> None:
    s = session(OTHER)
    written = u.write_batch(db, [], [s], set(), UTC, DAY0 + 800)
    assert written.rowids[id(s)][1] is None
    assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 0


def test_an_open_session_with_a_rowid_is_updated_not_inserted(db: sqlite3.Connection) -> None:
    s = session()
    rowid = u.write_batch(db, [], [s], set(), UTC, DAY0 + 800).rowids[id(s)][0]
    s.rowid, s.last_seen = rowid, DAY0 + 900
    u.write_batch(db, [], [s], set(), UTC, DAY0 + 950)
    assert db.execute("SELECT count(*), max(last_seen) FROM listens").fetchone() == (1, DAY0 + 900)


def test_caps_drop_and_count_past_the_limit(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "DAILY_CAP", 2)
    visits = [VisitRow(DAY0 + i, f"{i:032x}", AGENT, None) for i in range(4)]
    u.write_batch(db, visits, [], set(), UTC, DAY0 + 10)
    assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 2
    dropped = dict(db.execute("SELECT key, value FROM daily WHERE metric = 'dropped'").fetchall())
    assert dropped["devices"] == 2


def test_named_devices_are_exempt_from_the_listens_cap(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "DAILY_CAP", 1)
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    rows = [session(OTHER), session(None), session(ID)]
    u.write_batch(db, [], rows, {ID}, UTC, DAY0 + 800)
    devices = [r[0] for r in db.execute("SELECT device FROM listens ORDER BY id")]
    assert devices == [None, ID]  # the second unidentified one was over the cap


def test_roll_up_replaces_the_day_and_keeps_dropped(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0 + 50, ID, AGENT, None)], [session()], set(), UTC, DAY0 + 800)
    db.execute("INSERT INTO daily VALUES ('2026-10-24', 'dropped', 'visits', 3)")
    assert u.roll_up(db, "2026-10-24", UTC) == 1
    assert u.roll_up(db, "2026-10-24", UTC) == 1
    rows = dict(
        ((m, k), v)
        for m, k, v in db.execute("SELECT metric, key, value FROM daily WHERE day = '2026-10-24'")
    )
    assert rows[("seconds_channel", "owr")] == 600.0
    assert rows[("visits", "-")] == 1
    assert rows[("peak", "-")] == 1
    assert rows[("dropped", "visits")] == 3


def test_a_session_across_midnight_is_split(db: sqlite3.Connection) -> None:
    u.write_batch(db, [], [session(start=DAY0 - 300, last=DAY0 + 300)], set(), UTC, DAY0 + 400)
    u.roll_up(db, "2026-10-23", UTC)
    u.roll_up(db, "2026-10-24", UTC)
    got = dict(
        db.execute("SELECT day, value FROM daily WHERE metric = 'seconds_channel'").fetchall()
    )
    assert got == {"2026-10-23": 300.0, "2026-10-24": 300.0}


def test_retention_deletes_old_rows_and_stale_unnamed_devices_only(db: sqlite3.Connection) -> None:
    u.write_batch(
        db,
        [VisitRow(DAY0, ID, AGENT, None), VisitRow(DAY0, OTHER, AGENT, None)],
        [session()],
        set(),
        UTC,
        DAY0,
    )
    u.rename_device(db, ID, "Car")
    deleted = u.retention(db, DAY0 + 10_000, threading.Event())
    assert deleted >= 3
    assert [r[0] for r in db.execute("SELECT id FROM devices")] == [ID]
    assert db.execute("SELECT count(*) FROM listens").fetchone()[0] == 0


def test_retention_stops_between_batches(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "BATCH", 1)
    u.write_batch(db, [VisitRow(DAY0 + i, ID, AGENT, None) for i in range(3)], [], set(), UTC, DAY0)
    stop = threading.Event()
    stop.set()
    u.retention(db, DAY0 + 10_000, stop)
    assert db.execute("SELECT count(*) FROM visits").fetchone()[0] >= 2


def test_clear_addresses_and_delete_device(db: sqlite3.Connection) -> None:
    s = session()
    s.address = "198.51.100.7"
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, "198.51.100.7")], [s], set(), UTC, DAY0 + 800)
    u.clear_addresses(db)
    assert db.execute("SELECT count(*) FROM visits WHERE address IS NOT NULL").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM listens WHERE address IS NOT NULL").fetchone()[0] == 0
    u.roll_up(db, "2026-10-24", UTC)
    assert u.delete_device(db, ID) is True
    assert u.delete_device(db, ID) is False
    left = db.execute(
        "SELECT (SELECT count(*) FROM visits) + (SELECT count(*) FROM listens)"
        " + (SELECT count(*) FROM daily WHERE metric = 'seconds_device' AND key = ?)",
        (ID,),
    ).fetchone()[0]
    assert left == 0


def test_rows_after_leaves_out_ids_memory_holds(db: sqlite3.Connection) -> None:
    a, b = session(), session(start=DAY0 + 1000, last=DAY0 + 1100)
    written = u.write_batch(db, [], [a, b], set(), UTC, DAY0 + 1200)
    intervals, _visits = u.rows_after(db, DAY0, {written.rowids[id(a)][0]})
    assert [i.start for i in intervals] == [DAY0 + 1000]


def test_reader_is_read_only(db: sqlite3.Connection, tmp_path: Path) -> None:
    reader = u.open_reader(tmp_path / "usage.db")
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM devices")
    reader.close()


def test_device_page_pages_by_started_at(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    rows = [session(start=DAY0 + i * 100, last=DAY0 + i * 100 + 50) for i in range(60)]
    u.write_batch(db, [], rows, set(), UTC, DAY0 + 7000)
    first = sessions_of(u.device_page(db, ID, None, DAY0 - 1))
    assert len(first) == 50
    cursor = cast(float, first[-1]["started_at"])
    second = sessions_of(u.device_page(db, ID, cursor, DAY0 - 1))
    assert len(second) == 10
    assert cursor == DAY0 + 1000 and second[0]["started_at"] == DAY0 + 900


def test_written_names_every_day_a_stored_row_touches(db: sqlite3.Connection) -> None:
    visit = VisitRow(DAY0 + 86_400 * 3, ID, AGENT, None)
    long = session(start=DAY0 - 300, last=DAY0 + 86_400 + 5)
    written = u.write_batch(db, [visit], [long], set(), UTC, DAY0 + 86_400 * 3)
    assert written.days == {"2026-10-23", "2026-10-24", "2026-10-25", "2026-10-27"}


def test_a_dropped_session_is_not_written_and_touches_no_day(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "DAILY_CAP", 0)
    s = session(OTHER)
    written = u.write_batch(db, [], [s], set(), UTC, DAY0 + 800)
    assert written.rowids == {} and written.days == set()
    dropped = dict(db.execute("SELECT key, value FROM daily WHERE metric = 'dropped'").fetchall())
    assert dropped == {"listens": 1}


def test_the_cap_counters_start_again_on_a_new_day(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "DAILY_CAP", 1)
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    u.write_batch(db, [VisitRow(DAY0 + 10, OTHER, AGENT, None)], [], set(), UTC, DAY0 + 10)
    u.write_batch(db, [VisitRow(DAY0 + 86_400, OTHER, AGENT, None)], [], set(), UTC, DAY0 + 86_400)
    assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 2
    assert u.get_meta(db, "cap_day", "") == "2026-10-25"
    assert u.get_meta(db, "cap_devices", "") == "1"
    dropped = db.execute("SELECT day, key, value FROM daily WHERE metric = 'dropped'").fetchall()
    assert dropped == [("2026-10-24", "devices", 1.0), ("2026-10-24", "visits", 1.0)]


def test_a_later_visit_moves_last_seen_and_agent_but_not_first_seen(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    u.write_batch(
        db, [VisitRow(DAY0 + 60, ID, "Firefox 140, Android", None)], [], set(), UTC, DAY0 + 60
    )
    row = db.execute("SELECT first_seen, last_seen, agent FROM devices").fetchone()
    assert row == (DAY0, DAY0 + 60, "Firefox 140, Android")


def test_meta_round_trips_with_a_default(db: sqlite3.Connection) -> None:
    assert u.get_meta(db, "retention_days", "365") == "365"
    u.set_meta(db, "retention_days", "30")
    u.set_meta(db, "retention_days", "40")
    assert u.get_meta(db, "retention_days", "365") == "40"


def test_rename_and_named_devices(db: sqlite3.Connection) -> None:
    u.write_batch(
        db,
        [VisitRow(DAY0, ID, AGENT, None), VisitRow(DAY0, OTHER, AGENT, None)],
        [],
        set(),
        UTC,
        DAY0,
    )
    assert u.rename_device(db, ID, "Car") is True
    assert u.rename_device(db, "0" * 32, "Nobody") is False
    assert u.named_devices(db) == {ID: "Car"}
    assert u.rename_device(db, ID, None) is True
    assert u.named_devices(db) == {}


def test_retention_keeps_a_device_whose_sessions_are_recent(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    u.write_batch(
        db, [], [session(start=DAY0 + 100, last=DAY0 + 20_000)], set(), UTC, DAY0 + 20_000
    )
    u.retention(db, DAY0 + 10_000, threading.Event())
    assert [r[0] for r in db.execute("SELECT id FROM devices")] == [ID]
    assert db.execute("SELECT count(*) FROM listens").fetchone()[0] == 1


def test_retention_deletes_the_seconds_of_a_stale_device_only(db: sqlite3.Connection) -> None:
    u.write_batch(
        db,
        [VisitRow(DAY0, ID, AGENT, None), VisitRow(DAY0, OTHER, AGENT, None)],
        [],
        set(),
        UTC,
        DAY0,
    )
    db.execute("INSERT INTO daily VALUES ('2026-10-24', 'seconds_device', ?, 5)", (ID,))
    db.execute("INSERT INTO daily VALUES ('2026-10-24', 'seconds_device', ?, 7)", (OTHER,))
    db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (DAY0 + 20_000, OTHER))
    u.retention(db, DAY0 + 10_000, threading.Event())
    keys = [r[0] for r in db.execute("SELECT key FROM daily WHERE metric = 'seconds_device'")]
    assert keys == [OTHER]


def test_delete_device_leaves_other_devices_alone(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "BATCH", 2)
    visits = [VisitRow(DAY0 + i, d, AGENT, None) for i in range(5) for d in (ID, OTHER)]
    u.write_batch(db, visits, [session(ID), session(OTHER)], set(), UTC, DAY0 + 800)
    assert u.delete_device(db, ID) is True
    assert db.execute("SELECT count(*) FROM visits WHERE device = ?", (ID,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM visits WHERE device = ?", (OTHER,)).fetchone()[0] == 5
    assert db.execute("SELECT count(*) FROM listens").fetchone()[0] == 1
    assert [r[0] for r in db.execute("SELECT id FROM devices")] == [OTHER]


def test_clear_addresses_runs_in_batches(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "BATCH", 2)
    u.write_batch(
        db, [VisitRow(DAY0 + i, ID, AGENT, "2001:db8::1") for i in range(5)], [], set(), UTC, DAY0
    )
    u.clear_addresses(db)
    assert db.execute("SELECT count(*) FROM visits WHERE address IS NULL").fetchone()[0] == 5


def test_rows_after_gives_visit_times_and_widened_intervals(db: sqlite3.Connection) -> None:
    visits = [VisitRow(DAY0 - 5, ID, AGENT, None), VisitRow(DAY0 + 5, ID, AGENT, None)]
    one_poll = session(start=DAY0 + 100, last=DAY0 + 100)
    old = session(start=DAY0 - 900, last=DAY0 - 800)
    u.write_batch(db, visits, [one_poll, old], set(), UTC, DAY0 + 200)
    intervals, times = u.rows_after(db, DAY0, set())
    assert times == [DAY0 + 5]
    assert [(i.start, i.end, i.device) for i in intervals] == [(DAY0 + 100, DAY0 + 110, ID)]


def test_devices_page_lists_played_devices_with_their_month(db: sqlite3.Connection) -> None:
    visits = [VisitRow(DAY0, ID, AGENT, None), VisitRow(DAY0 + 5, OTHER, AGENT, None)]
    u.write_batch(db, visits, [], set(), UTC, DAY0 + 5)
    u.rename_device(db, ID, "Car")
    rows = [session(start=DAY0 - 1000, last=DAY0 - 900), session(start=DAY0 + 100, last=DAY0 + 400)]
    u.write_batch(db, [], rows, set(), UTC, DAY0 + 500)
    devices, never_played = u.devices_page(db, DAY0)
    assert never_played == 1
    assert devices == [
        {
            "id": ID,
            "name": "Car",
            "agent": AGENT,
            "first_seen": DAY0,
            "last_seen": DAY0,
            "seconds_30d": 300.0,
        }
    ]


def test_devices_page_orders_by_last_seen_and_counts_a_silent_device_as_zero(
    db: sqlite3.Connection,
) -> None:
    visits = [VisitRow(DAY0, ID, AGENT, None), VisitRow(DAY0 + 5, OTHER, AGENT, None)]
    u.write_batch(db, visits, [session(ID), session(OTHER)], set(), UTC, DAY0 + 800)
    devices, never_played = u.devices_page(db, DAY0 + 10_000)
    assert never_played == 0
    assert [(d["id"], d["seconds_30d"]) for d in devices] == [(OTHER, 0.0), (ID, 0.0)]


def test_device_page_shows_sessions_and_top_stations(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    a = session(start=DAY0 + 100, last=DAY0 + 400)
    a.address = "198.51.100.7"
    b = SessionRow(None, ID, "npo", "Low", DAY0 + 500, DAY0 + 600, AGENT, None)
    c = SessionRow(None, ID, "npo", "Low", DAY0 - 5000, DAY0 - 4000, AGENT, None)
    written = u.write_batch(db, [], [a, b, c], set(), UTC, DAY0 + 700)
    page = u.device_page(db, ID, None, DAY0)
    assert page is not None
    assert (page["id"], page["name"], page["agent"]) == (ID, None, AGENT)
    assert page["first_seen"] == DAY0 and page["last_seen"] == DAY0
    assert page["sessions"] == [
        {
            "id": written.rowids[id(b)][0],
            "channel": "npo",
            "stream": "Low",
            "started_at": DAY0 + 500,
            "last_seen": DAY0 + 600,
            "seconds": 100.0,
            "address": None,
        },
        {
            "id": written.rowids[id(a)][0],
            "channel": "owr",
            "stream": "Default",
            "started_at": DAY0 + 100,
            "last_seen": DAY0 + 400,
            "seconds": 300.0,
            "address": "198.51.100.7",
        },
        {
            "id": written.rowids[id(c)][0],
            "channel": "npo",
            "stream": "Low",
            "started_at": DAY0 - 5000,
            "last_seen": DAY0 - 4000,
            "seconds": 1000.0,
            "address": None,
        },
    ]
    assert page["top"] == [["owr", 300.0], ["npo", 100.0]]
    assert u.device_page(db, OTHER, None, DAY0) is None


def test_daily_reads_over_two_rolled_up_days(db: sqlite3.Connection) -> None:
    u.write_batch(db, [VisitRow(DAY0 - 50, ID, AGENT, None)], [], set(), UTC, DAY0)
    rows = [
        session(start=DAY0 - 1000, last=DAY0 - 400),
        SessionRow(None, None, "npo", "Low", DAY0 - 900, DAY0 - 800, AGENT, None),
        session(start=DAY0 + 100, last=DAY0 + 300),
    ]
    u.write_batch(db, [], rows, set(), UTC, DAY0 + 400)
    u.roll_up(db, "2026-10-23", UTC)
    u.roll_up(db, "2026-10-24", UTC)
    assert u.daily_series(db, "seconds_channel", "2026-10-23", "2026-10-24") == {
        "2026-10-23": 700.0,
        "2026-10-24": 200.0,
    }
    assert u.daily_series(db, "seconds_channel", "2026-10-24", "2026-10-24") == {
        "2026-10-24": 200.0
    }
    assert u.daily_tops(db, "seconds_channel", "2026-10-23", "2026-10-24", 5) == [
        ("owr", 800.0),
        ("npo", 100.0),
    ]
    assert u.daily_tops(db, "seconds_channel", "2026-10-23", "2026-10-24", 1) == [("owr", 800.0)]
    assert u.daily_peak(db, "2026-10-23", "2026-10-24") == (2, "2026-10-23")
    assert u.daily_peak(db, "2026-10-25", "2026-10-26") == (0, None)


def test_last_ended_is_the_newest_session(db: sqlite3.Connection) -> None:
    assert u.last_ended(db) is None
    u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [], set(), UTC, DAY0)
    rows = [
        session(start=DAY0 + 100, last=DAY0 + 900),
        session(OTHER, start=DAY0 + 200, last=DAY0 + 300),
    ]
    u.write_batch(db, [], rows, set(), UTC, DAY0 + 1000)
    assert u.last_ended(db) == {
        "device": ID,
        "channel": "owr",
        "stream": "Default",
        "started_at": DAY0 + 100,
        "last_seen": DAY0 + 900,
        "agent": AGENT,
    }


def test_a_failed_batch_writes_nothing(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_self: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(u._Caps, "save", broken)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(sqlite3.OperationalError):
        u.write_batch(db, [VisitRow(DAY0, ID, AGENT, None)], [session()], set(), UTC, DAY0 + 800)
    assert not db.in_transaction
    assert (
        db.execute(
            "SELECT (SELECT count(*) FROM devices) + (SELECT count(*) FROM listens)"
        ).fetchone()[0]
        == 0
    )


def test_retention_deletes_stale_devices_across_batches(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "BATCH", 1)
    visits = [VisitRow(DAY0, f"{i:032x}", AGENT, None) for i in range(3)]
    u.write_batch(db, visits, [], set(), UTC, DAY0)
    assert u.retention(db, DAY0 + 10, threading.Event()) == 6
    assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 0
