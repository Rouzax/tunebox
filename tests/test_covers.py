from tests.fakes import make_track
from tunebox.catalogs import Found, SearchError
from tunebox.covers import HISTORY, NOW, Cover, CoverService
from tunebox.store import Store
from tunebox.tracks import Track

DAY = 86400.0
URL = "https://example.invalid/a.jpg"


class Finder:
    def __init__(self, result: Found | Exception | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(self, track: Track) -> Found | None:
        self.calls.append(track.title)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def service(finder: Finder, now: list[float], store: Store | None = None) -> CoverService:
    return CoverService(store or Store(None), finder, clock=lambda: now[0])


def drain(covers: CoverService) -> None:
    while covers.run_once():
        pass


def test_lookup_after_a_search() -> None:
    covers = service(Finder(Found(URL, "album", "deezer: Fisher - Stay")), [1000.0])
    track = make_track()
    assert covers.lookup(track) is None
    covers.request(track, NOW)
    assert covers.run_once()
    assert covers.lookup(track) == Cover(URL, "album")
    assert not covers.run_once()


def test_artist_photo_kind() -> None:
    covers = service(Finder(Found(URL, "artist", "deezer artist: Fisher")), [0.0])
    covers.request(make_track(), NOW)
    covers.run_once()
    assert covers.lookup(make_track()) == Cover(URL, "artist")


def test_unsearchable_tracks_are_never_queued() -> None:
    finder = Finder()
    covers = service(finder, [0.0])
    covers.request(make_track("", "E1700 16 Oct 2026 M19 VO"), NOW)
    covers.request(make_track("", "Some Title"), NOW)
    covers.request(make_track("FISHER", "(Intro)"), NOW)
    assert not covers.run_once()
    assert finder.calls == []


def test_now_goes_before_history_and_upgrades_a_queued_track() -> None:
    finder = Finder()
    covers = service(finder, [0.0])
    covers.request(make_track("A", "Old"), HISTORY)
    covers.request(make_track("B", "Other"), HISTORY)
    covers.request(make_track("C", "New"), NOW)
    covers.request(make_track("B", "Other"), NOW)
    drain(covers)
    assert finder.calls == ["New", "Other", "Old"]


def test_one_search_per_track() -> None:
    finder = Finder()
    covers = service(finder, [0.0])
    covers.request(make_track(), NOW)
    covers.request(make_track(), NOW)
    drain(covers)
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    assert finder.calls == ["Stay"]


def test_decisions_expire_by_outcome() -> None:
    now = [0.0]
    finder = Finder(None)
    covers = service(finder, now)
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] = 3 * DAY - 1
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    now[0] = 3 * DAY + 1
    covers.request(make_track(), NOW)
    assert covers.run_once()
    assert len(finder.calls) == 2


def test_search_errors_retry_after_ten_minutes() -> None:
    now = [0.0]
    covers = service(Finder(SearchError("down")), now)
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] = 599
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    now[0] = 601
    covers.request(make_track(), NOW)
    assert covers.run_once()


def test_an_expired_hit_is_still_shown_until_replaced() -> None:
    now = [0.0]
    covers = service(Finder(Found(URL, "album", "m")), now)
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] = 91 * DAY
    assert covers.lookup(make_track()) is not None


def test_decisions_persist_in_the_store() -> None:
    store = Store(None)
    first = service(Finder(Found(URL, "album", "m")), [0.0], store)
    first.request(make_track(), NOW)
    first.run_once()
    second = service(Finder(), [1.0], store)
    assert second.lookup(make_track()) is not None
    second.request(make_track(), NOW)
    assert not second.run_once()


def test_a_crashing_finder_does_not_stop_the_worker() -> None:
    covers = service(Finder(RuntimeError("bug")), [0.0])
    covers.request(make_track("A", "One"), NOW)
    covers.request(make_track("B", "Two"), NOW)
    assert covers.run_once()
    assert covers.run_once()
    assert covers.lookup(make_track("A", "One")) is None
