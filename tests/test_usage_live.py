from tunebox.usage import Live
from tunebox.usagecore import IDLE_S, MAX_LIVE, POLL_S
from tunebox.usagedb import SessionRow, VisitRow, Written

ID = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
AGENT = "Chrome 120, Linux"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def make() -> tuple[Live, Clock]:
    clock = Clock()
    return Live(clock), clock


def test_a_gap_under_idle_keeps_one_session() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += IDLE_S - 1
    live.heard(ID, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    assert closed == [] and len(open_rows) == 1
    assert open_rows[0][0].last_seen == clock.now


def test_a_gap_of_idle_or_more_starts_a_new_session() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += IDLE_S
    live.heard(ID, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    assert len(closed) == 1 and closed[0].last_seen == 1_000.0
    assert len(open_rows) == 1


def test_a_station_change_closes_the_device_s_other_session() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += 10
    live.heard(ID, "qmusic", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    assert [s.channel for s in closed] == ["owr"]
    assert [s.channel for s, _ in open_rows] == ["qmusic"]
    assert [row["channel"] for row in live.live()] == ["qmusic"]


def test_cookieless_players_do_not_close_each_other() -> None:
    live, _ = make()
    live.heard(None, "owr", "Default", AGENT, None)
    live.heard(None, "qmusic", "Default", AGENT, None)
    live.heard(None, "owr", "Default", "Safari 17, iOS", None)
    _, closed, open_rows = live.detach()
    assert closed == [] and len(open_rows) == 3


def test_live_leaves_out_idle_sessions_and_shows_the_last_ended() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += IDLE_S
    assert live.live() == []
    live.detach()
    ended = live.last_ended()
    assert ended is not None and ended["channel"] == "owr"


def test_live_rows_and_seconds() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    assert live.live() == [
        {
            "label": f"Unnamed device · {AGENT}",
            "channel": "owr",
            "stream": "Default",
            "seconds": POLL_S,
        }
    ]
    clock.now += 30
    live.heard(ID, "owr", "Default", AGENT, None)
    assert live.live()[0]["seconds"] == 30


def test_labels() -> None:
    live, _ = make()
    live.names = {ID: "Car"}
    live.heard(ID, "owr", "Default", AGENT, None)
    live.heard(OTHER, "qmusic", "Default", AGENT, None)
    live.heard(None, "npo", "Default", AGENT, None)
    labels = sorted(str(row["label"]) for row in live.live())
    assert labels == ["Car", f"Unidentified player · {AGENT}", f"Unnamed device · {AGENT}"]


def test_a_cookie_without_a_device_row_shows_as_unidentified_once_resolved() -> None:
    live, _ = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    session = open_rows[0][0]
    live.adopt(Written({id(session): (7, None)}, set(), set()), open_rows, closed)
    assert live.live()[0]["label"] == f"Unidentified player · {AGENT}"


def test_names_are_replaced_whole() -> None:
    live, _ = make()
    names = {ID: "Car"}
    live.names = names
    names[OTHER] = "Phone"
    assert live.names == {ID: "Car"}


def test_addresses_are_dropped_at_intake_while_off_and_stripped_when_switched_off() -> None:
    live, _ = make()
    live.heard(ID, "owr", "Default", AGENT, "198.51.100.7")
    assert live.detach()[2][0][0].address is None
    live.addresses = True
    live.visit(ID, AGENT, "198.51.100.7")
    live.heard(OTHER, "owr", "Default", AGENT, "198.51.100.7")
    live.addresses = False
    visits, _, open_rows = live.detach()
    assert [v.address for v in visits] == [None]
    assert all(s.address is None for s, _ in open_rows)


def test_switching_addresses_off_strips_pending_closed_sessions() -> None:
    live, clock = make()
    live.addresses = True
    assert live.addresses
    live.heard(ID, "owr", "Default", AGENT, "198.51.100.7")
    clock.now += 10
    live.heard(ID, "qmusic", "Default", AGENT, "198.51.100.7")
    assert live.live()[0]["address"] == "198.51.100.7"
    live.addresses = False
    assert "address" not in live.live()[0]
    _, closed, _ = live.detach()
    assert [s.address for s in closed] == [None]


def test_eviction_closes_unidentified_first_then_unnamed_then_named() -> None:
    live, clock = make()
    live.names = {ID: "Car"}
    live.heard(ID, "owr", "Default", AGENT, None)
    for i in range(MAX_LIVE - 1):
        clock.now += 0.001
        live.heard(f"{i:032x}", "owr", "Default", AGENT, None)
    clock.now += 0.001
    live.heard(None, "owr", "Default", "Firefox 132, Android", None)
    live.heard(None, "owr", "Default", "Safari 17, iOS", None)
    _, closed, open_rows = live.detach()
    assert any(s.cookie is None for s in closed)
    assert ID in {s.cookie for s, _ in open_rows}
    assert len(open_rows) == MAX_LIVE


def test_eviction_closes_the_longest_idle_unnamed_device() -> None:
    live, clock = make()
    for i in range(MAX_LIVE):
        clock.now += 0.001
        live.heard(f"{i:032x}", "owr", "Default", AGENT, None)
    clock.now += 0.001
    live.heard(f"{0:032x}", "owr", "Default", AGENT, None)
    live.heard(OTHER, "owr", "Default", AGENT, None)
    _, closed, _ = live.detach()
    assert [s.cookie for s in closed] == [f"{1:032x}"]


def test_eviction_reaches_named_devices_last() -> None:
    live, clock = make()
    live.names = {f"{i:032x}": f"Device {i}" for i in range(MAX_LIVE)}
    for i in range(MAX_LIVE):
        clock.now += 0.001
        live.heard(f"{i:032x}", "owr", "Default", AGENT, None)
    live.heard(OTHER, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    assert [s.cookie for s in closed] == [f"{0:032x}"]
    assert len(open_rows) == MAX_LIVE


def test_idle_sessions_are_closed_before_any_eviction() -> None:
    live, clock = make()
    live.names = {f"{i:032x}": f"Device {i}" for i in range(MAX_LIVE)}
    for i in range(MAX_LIVE):
        live.heard(f"{i:032x}", "owr", "Default", AGENT, None)
    clock.now += IDLE_S
    live.heard(None, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    assert len(closed) == MAX_LIVE
    assert [s.cookie for s, _ in open_rows] == [None]


def test_requeue_keeps_at_most_max_pending() -> None:
    live, _ = make()
    for _ in range(3):
        live.visit(ID, AGENT, None)
    visits, closed, _ = live.detach()
    dropped = live.requeue(visits * 4000, closed)
    assert dropped == 12_000 - 10_000


def test_requeue_puts_the_failed_batch_first_and_trims_the_oldest() -> None:
    live, clock = make()
    live.visit(ID, AGENT, None)
    failed, _, _ = live.detach()
    clock.now += 5
    live.visit(OTHER, AGENT, None)
    assert live.requeue(failed, []) == 0
    visits, _, _ = live.detach()
    assert [v.device for v in visits] == [ID, OTHER]
    old = [VisitRow(float(i), ID, AGENT, None) for i in range(10_000)]
    live.visit(OTHER, AGENT, None)
    session = SessionRow(None, ID, "owr", "Default", 0.5, 0.5, AGENT, None)
    assert live.requeue(old, [session]) == 2
    visits, closed, _ = live.detach()
    assert len(visits) + len(closed) == 10_000
    assert closed == [] and visits[0].at == 1.0 and visits[-1].device == OTHER


def test_forget_drops_open_and_pending_rows_of_the_device() -> None:
    live, _ = make()
    live.visit(ID, AGENT, None)
    live.heard(ID, "owr", "Default", AGENT, None)
    live.heard(OTHER, "owr", "Default", AGENT, None)
    live.forget(ID)
    visits, closed, open_rows = live.detach()
    assert visits == [] and closed == []
    assert [s.cookie for s, _ in open_rows] == [OTHER]


def test_forget_drops_pending_closed_sessions_and_the_last_ended() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += 10
    live.heard(ID, "qmusic", "Default", AGENT, None)
    live.close_all()
    live.forget(ID)
    assert live.detach()[1] == []
    assert live.last_ended() is None


def test_adopt_turns_the_next_write_into_an_update() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    session = open_rows[0][0]
    live.adopt(Written({id(session): (42, ID)}, {"2026-10-25"}, set()), open_rows, closed)
    assert (session.rowid, session.device, session.resolved) == (42, ID, True)
    clock.now += 10
    live.heard(ID, "owr", "Default", AGENT, None)
    _, _, open_rows = live.detach()
    assert [(s.rowid, seen) for s, seen in open_rows] == [(42, clock.now)]


def test_adopt_sets_the_rowid_of_closed_rows() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += IDLE_S
    _, closed, open_rows = live.detach()
    assert open_rows == []
    live.adopt(Written({id(closed[0]): (3, ID)}, set(), set()), open_rows, closed)
    assert (closed[0].rowid, closed[0].resolved) == (3, True)


def test_a_session_the_cap_dropped_is_never_written_again_but_still_counts() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    session = open_rows[0][0]
    live.adopt(Written({}, set(), {id(session)}), open_rows, closed)
    assert session.discarded and session.resolved and session.rowid is None
    clock.now += 10
    live.heard(ID, "owr", "Default", AGENT, None)
    assert live.detach()[2] == []
    assert len(live.live()) == 1
    _, intervals, _ = live.snapshot()
    assert len(intervals) == 1
    live.close_all()
    assert live.detach()[1] == []
    ended = live.last_ended()
    assert ended is not None and ended["channel"] == "owr"


def test_close_all_closes_every_open_session_at_its_last_seen() -> None:
    live, clock = make()
    live.heard(ID, "owr", "Default", AGENT, None)
    clock.now += 5
    live.heard(OTHER, "qmusic", "Low", AGENT, None)
    clock.now += 20
    live.close_all()
    assert live.live() == []
    _, closed, open_rows = live.detach()
    assert open_rows == []
    assert sorted((s.channel, s.last_seen) for s in closed) == [
        ("owr", 1_000.0),
        ("qmusic", 1_005.0),
    ]
    ended = live.last_ended()
    assert ended is not None
    assert ended == {
        "label": f"Unnamed device · {AGENT}",
        "channel": "qmusic",
        "stream": "Low",
        "seconds": POLL_S,
        "ended_at": 1_005.0,
    }


def test_snapshot_gives_open_and_pending_intervals_and_held_rowids() -> None:
    live, clock = make()
    live.visit(ID, AGENT, None)
    live.heard(ID, "owr", "Default", AGENT, None)
    live.heard(OTHER, "npo", "Default", AGENT, None)
    _, closed, open_rows = live.detach()
    by_cookie = {s.cookie: s for s, _ in open_rows}
    live.adopt(
        Written({id(by_cookie[ID]): (5, ID), id(by_cookie[OTHER]): (6, None)}, set(), set()),
        open_rows,
        closed,
    )
    clock.now += 30
    live.visit(OTHER, AGENT, None)
    live.heard(ID, "qmusic", "Default", AGENT, None)
    live.heard(None, "owr", "Default", AGENT, None)
    rowids, intervals, visits = live.snapshot()
    assert rowids == {5, 6}
    assert visits == [1_030.0]
    assert sorted((i.channel, i.start, i.end, i.device) for i in intervals) == [
        ("npo", 1_000.0, 1_010.0, None),
        ("owr", 1_000.0, 1_010.0, ID),
        ("owr", 1_030.0, 1_040.0, None),
        ("qmusic", 1_030.0, 1_040.0, None),
    ]
    assert live.memory_rowids() == rowids
    assert live.memory_intervals() == intervals


def test_set_last_ended_takes_a_stored_row() -> None:
    live, _ = make()
    assert live.last_ended() is None
    live.names = {ID: "Car"}
    live.set_last_ended(
        {
            "device": ID,
            "channel": "owr",
            "stream": "Default",
            "started_at": 100.0,
            "last_seen": 400.0,
            "agent": AGENT,
        }
    )
    assert live.last_ended() == {
        "label": "Car",
        "channel": "owr",
        "stream": "Default",
        "seconds": 300.0,
        "ended_at": 400.0,
    }
    live.set_last_ended(None)
    assert live.last_ended() is None


def test_an_older_close_does_not_replace_a_newer_last_ended() -> None:
    live, _ = make()
    live.set_last_ended(
        {
            "device": None,
            "channel": "npo",
            "stream": "Default",
            "started_at": 2_000.0,
            "last_seen": 3_000.0,
            "agent": AGENT,
        }
    )
    live.heard(ID, "owr", "Default", AGENT, None)
    live.close_all()
    ended = live.last_ended()
    assert ended is not None
    assert ended["channel"] == "npo" and ended["label"] == f"Unidentified player · {AGENT}"
