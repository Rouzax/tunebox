import itertools
import logging
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import make_track
from tunebox import covers as covers_module
from tunebox.catalogs import Found, SearchError
from tunebox.coverfiles import CoverFiles, name_for
from tunebox.covers import HISTORY, NOW, CoverService
from tunebox.http import HttpError
from tunebox.store import CoverDecision, Store
from tunebox.tracks import Track

DAY = 86400.0
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64
URL = "https://example.invalid/images/cover/abc/1000x1000-000000-80-0-0.jpg"
LOCAL = f"/covers/{name_for(URL)}.jpg?v="


_scratch: list[Path] = []
_serial = itertools.count()


@pytest.fixture(autouse=True)
def scratch(tmp_path: Path) -> Generator[None]:
    """A folder under pytest's tmp_path for every service a test builds without one."""
    _scratch[:] = [tmp_path]
    yield
    _scratch.clear()


def fresh_folder() -> Path:
    return _scratch[0] / f"covers-{next(_serial)}"


class Finder:
    def __init__(self, result: Found | Exception | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(self, track: Track) -> Found | None:
        self.calls.append(track.title)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Fetch:
    def __init__(self, answer: bytes | HttpError = JPG) -> None:
        self.answer = answer
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if isinstance(self.answer, HttpError):
            raise self.answer
        return self.answer


def service(
    finder: Finder,
    now: list[float],
    store: Store | None = None,
    fetch: Fetch | None = None,
    folder: Path | None = None,
) -> CoverService:
    files = CoverFiles(folder or fresh_folder(), free=lambda: None, clock=lambda: now[0])
    return CoverService(store or Store(None), finder, files, fetch or Fetch(), clock=lambda: now[0])


def drain(covers: CoverService) -> None:
    while covers.run_once():
        pass


def test_lookup_after_a_search() -> None:
    covers = service(Finder(Found(URL, "album", "deezer: Fisher - Stay")), [1000.0])
    track = make_track()
    assert covers.lookup(track) is None
    covers.request(track, NOW)
    assert covers.run_once()
    cover = covers.lookup(track)
    assert cover is not None and cover.url.startswith(LOCAL) and cover.kind == "album"
    assert not covers.run_once()


def test_artist_photo_kind() -> None:
    covers = service(Finder(Found(URL, "artist", "deezer artist: Fisher")), [0.0])
    covers.request(make_track(), NOW)
    covers.run_once()
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL) and cover.kind == "artist"


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


def test_decisions_persist_in_the_store(tmp_path: Path) -> None:
    store = Store(None)
    first = service(Finder(Found(URL, "album", "m")), [0.0], store, folder=tmp_path)
    first.request(make_track(), NOW)
    first.run_once()
    second = service(Finder(), [1.0], store, folder=tmp_path)
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


def test_a_failed_research_keeps_the_expired_cover() -> None:
    now = [0.0]
    finder = Finder(Found(URL, "album", "m"))
    covers = service(finder, now)
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] = 91 * DAY
    finder.result = SearchError("quota")
    covers.request(make_track(), NOW)
    assert covers.run_once()
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL) and cover.kind == "album"
    now[0] = 91 * DAY + 599
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    now[0] = 91 * DAY + 601
    covers.request(make_track(), NOW)
    assert covers.run_once()


def test_names_without_latin_letters_are_never_queued() -> None:
    finder = Finder()
    covers = service(finder, [0.0])
    covers.request(make_track("ЛСП", "Монетка"), NOW)
    covers.request(make_track("!!!", "Heart Of Hearts"), NOW)
    assert not covers.run_once()


class CrashingFetch(Fetch):
    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        raise RuntimeError("bug")


def test_a_crashing_download_does_not_stop_the_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    fetch = CrashingFetch()
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch, folder=tmp_path)
    covers.request(make_track("A", "One"), NOW)
    covers.request(make_track("B", "Two"), NOW)
    assert covers.run_once()
    assert covers.run_once()
    assert len(fetch.urls) == 2 and covers.lookup(make_track("A", "One")) is None
    crashes = [r for r in caplog.records if r.getMessage() == "cover_download_crashed"]
    assert len(crashes) == 2 and crashes[0].levelno == logging.ERROR
    covers.request(make_track("A", "One"), NOW)
    assert not covers.run_once()  # waits ten minutes like any failed download
    now[0] += 601
    covers.request(make_track("A", "One"), NOW)
    assert covers.run_once()


def test_a_search_downloads_the_600_image_in_the_same_job(tmp_path: Path) -> None:
    fetch = Fetch()
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], fetch=fetch, folder=tmp_path)
    track = make_track()
    covers.request(track, NOW)
    assert covers.run_once()
    assert fetch.urls == [URL.replace("/1000x1000-", "/600x600-")]
    cover = covers.lookup(track)
    assert cover is not None and cover.url.startswith(LOCAL)
    assert covers.remote_link(track) == URL
    assert not covers.run_once()


def test_an_existing_decision_without_a_file_is_downloaded_lazily(tmp_path: Path) -> None:
    store = Store(None)
    store.put_cover(CoverDecision(make_track().key, "cover", URL, "m", 1000.0))
    finder, fetch = Finder(), Fetch()
    covers = service(finder, [1000.0], store=store, fetch=fetch, folder=tmp_path)
    assert covers.lookup(make_track()) is None
    covers.request(make_track(), NOW)
    covers.run_once()
    assert finder.calls == [] and len(fetch.urls) == 1
    assert covers.lookup(make_track()) is not None


def test_a_failing_download_is_tried_at_most_every_ten_minutes(tmp_path: Path) -> None:
    now = [1000.0]
    fetch = Fetch(HttpError("down"))
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch, folder=tmp_path)
    track = make_track()
    covers.request(track, NOW)
    covers.run_once()
    covers.request(track, NOW)
    assert not covers.run_once()
    now[0] += 601
    covers.request(track, NOW)
    assert covers.run_once()
    assert len(fetch.urls) == 2


def test_repeated_download_failures_turn_into_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    fetch = Fetch(HttpError("down", 503))
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch, folder=tmp_path)
    caplog.set_level(logging.INFO)
    covers.request(make_track(), NOW)
    covers.run_once()
    for _ in range(2):
        now[0] += 601
        covers.request(make_track(), NOW)
        covers.run_once()
    levels = [r.levelno for r in caplog.records if r.getMessage() == "cover_download_failed"]
    assert levels == [logging.INFO, logging.INFO, logging.WARNING]
    fetch.answer = JPG
    now[0] += 601
    covers.request(make_track(), NOW)
    covers.run_once()
    fetch.answer = HttpError("down", 503)
    for path in tmp_path.iterdir():
        path.unlink()
    covers.request(make_track(), NOW)
    covers.run_once()
    levels = [r.levelno for r in caplog.records if r.getMessage() == "cover_download_failed"]
    assert levels[-1] == logging.INFO  # a success starts the count again


def test_a_dead_link_is_searched_again_and_the_old_file_stays(tmp_path: Path) -> None:
    now = [1000.0]
    fetch = Fetch()
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch, folder=tmp_path)
    track = make_track()
    covers.request(track, NOW)
    covers.run_once()
    now[0] += 91 * DAY  # the cover decision has expired
    fetch.answer = HttpError("gone", 404)
    covers.request(track, NOW)
    covers.run_once()  # re-search returns the same link; its download is gone
    assert covers.lookup(track) is not None  # the earlier file still serves
    now[0] += 601
    covers.request(track, NOW)
    assert covers.run_once()  # outcome error: searched again after ten minutes


def test_a_page_instead_of_an_image_counts_as_a_dead_link(tmp_path: Path) -> None:
    store = Store(None)
    covers = service(
        Finder(Found(URL, "album", "m")),
        [1000.0],
        store=store,
        fetch=Fetch(b"<html></html>"),
        folder=tmp_path,
    )
    covers.request(make_track(), NOW)
    covers.run_once()
    decision = store.get_cover(make_track().key)
    assert decision is not None and decision.outcome == "error" and decision.url == URL


def test_an_oversized_answer_counts_as_a_dead_link(tmp_path: Path) -> None:
    store = Store(None)
    fetch = Fetch(HttpError("too big", too_large=True))
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], store, fetch, tmp_path)
    covers.request(make_track(), NOW)
    covers.run_once()
    decision = store.get_cover(make_track().key)
    assert decision is not None and decision.outcome == "error" and decision.url == URL


def test_housekeeping_keeps_every_file_when_the_links_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(None)
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], store, folder=tmp_path)
    covers.request(make_track(), NOW)
    covers.run_once()
    monkeypatch.setattr(store, "cover_links", lambda: None)
    covers.housekeep()
    assert (tmp_path / f"{name_for(URL)}.jpg").exists()


def test_housekeeping_still_applies_the_cap_when_the_links_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tunebox.coverfiles.MAX_COVERS", 1)
    monkeypatch.setattr("tunebox.coverfiles.LOW_WATER", 0)
    store = Store(None)
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], store, folder=tmp_path)
    covers.request(make_track(), NOW)
    covers.run_once()
    monkeypatch.setattr(store, "cover_links", lambda: None)
    covers.housekeep()
    assert not (tmp_path / f"{name_for(URL)}.jpg").exists()


def test_an_unwritable_folder_counts_as_a_failed_download(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    now = [1000.0]
    fetch = Fetch()
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch, folder=blocker / "covers")
    covers.request(make_track(), NOW)
    covers.run_once()
    assert covers.lookup(make_track()) is None
    covers.request(make_track(), NOW)
    assert not covers.run_once()  # waits ten minutes like any failed download


def test_a_deleted_file_gives_no_cover_and_is_fetched_again(tmp_path: Path) -> None:
    fetch = Fetch()
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], fetch=fetch, folder=tmp_path)
    covers.request(make_track(), NOW)
    covers.run_once()
    for path in tmp_path.iterdir():
        path.unlink()
    assert covers.lookup(make_track()) is None
    covers.request(make_track(), NOW)
    assert covers.run_once()
    assert len(fetch.urls) == 2 and covers.lookup(make_track()) is not None


def test_a_full_disk_skips_the_download_and_warns_once_an_hour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    files = CoverFiles(tmp_path, free=lambda: 1, clock=lambda: now[0])
    covers = CoverService(
        Store(None), Finder(Found(URL, "album", "m")), files, Fetch(), clock=lambda: now[0]
    )
    caplog.set_level(logging.DEBUG)
    for title in ("A", "B"):
        covers.request(make_track(title=title), NOW)
        covers.run_once()
    warnings = [
        r
        for r in caplog.records
        if r.getMessage() == "cover_disk_low" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert covers.lookup(make_track(title="A")) is None


def test_housekeeping_prunes_old_decisions_and_their_files(tmp_path: Path) -> None:
    now = [1000.0]
    store = Store(None)
    covers = service(Finder(Found(URL, "album", "m")), now, store=store, folder=tmp_path)
    covers.request(make_track(), NOW)
    covers.run_once()
    shared = "https://example.invalid/shared/1000x1000-x.jpg"
    store.put_cover(CoverDecision("x|one", "cover", shared, "m", now[0] + 400 * DAY))
    store.put_cover(CoverDecision("x|two", "cover", shared, "m", now[0] + 10 * DAY))
    (tmp_path / f"{name_for(shared)}.jpg").write_bytes(JPG)
    now[0] += 366 * DAY
    covers.housekeep()
    assert store.get_cover(make_track().key) is None
    assert covers.remote_link(make_track()) is None
    assert not (tmp_path / f"{name_for(URL)}.jpg").exists()
    assert (tmp_path / f"{name_for(shared)}.jpg").exists()  # still named by x|one


def test_the_worker_housekeeps_at_start_and_survives_a_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    covers = service(Finder(), [1000.0])
    stop = threading.Event()
    calls: list[int] = []

    def crash() -> None:
        calls.append(1)
        stop.set()
        raise RuntimeError("bug")

    monkeypatch.setattr(covers, "housekeep", crash)
    covers.run_forever(stop)
    assert calls == [1]
    assert any(r.getMessage() == "cover_housekeeping_crashed" for r in caplog.records)


def test_the_worker_housekeeps_again_once_idle_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finder = Finder()
    covers = service(finder, [1000.0])
    stop = threading.Event()
    calls: list[int] = []

    def housekeep() -> None:
        calls.append(len(finder.calls))
        if len(calls) == 2:
            stop.set()

    monkeypatch.setattr(covers, "housekeep", housekeep)
    monkeypatch.setattr(covers_module, "HOUSEKEEP_S", 0)
    covers.request(make_track(), NOW)
    covers.run_forever(stop)
    assert calls == [0, 1]  # at start, then after the queued search


def test_full_disk_skips_do_not_count_toward_the_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    free = [1]
    files = CoverFiles(tmp_path, free=lambda: free[0], clock=lambda: now[0])
    fetch = Fetch()
    covers = CoverService(
        Store(None), Finder(Found(URL, "album", "m")), files, fetch, clock=lambda: now[0]
    )
    caplog.set_level(logging.INFO)
    covers.request(make_track(), NOW)
    covers.run_once()
    for _ in range(2):
        now[0] += 601
        covers.request(make_track(), NOW)
        covers.run_once()
    assert fetch.urls == []
    free[0] = 10**12
    fetch.answer = HttpError("down", 503)
    now[0] += 601
    covers.request(make_track(), NOW)
    covers.run_once()
    levels = [r.levelno for r in caplog.records if r.getMessage() == "cover_download_failed"]
    assert levels == [logging.INFO]


def test_failed_downloads_are_remembered_for_a_bounded_number_of_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(covers_module, "MEMORY", 2)
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], fetch=Fetch(HttpError("x", 503)))
    for title in ("A", "B", "C"):
        covers.request(make_track(title=title), NOW)
        covers.run_once()
    assert list(covers._failed) == [  # pyright: ignore[reportPrivateUsage]
        make_track(title="B").key,
        make_track(title="C").key,
    ]


def test_a_queued_job_still_runs_after_housekeeping_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finder = Finder()
    covers = service(finder, [1000.0])
    stop = threading.Event()
    covers.request(make_track(), NOW)

    def crash() -> None:
        raise RuntimeError("bug")

    def run_once() -> bool:
        ran = real_run_once()
        stop.set()
        return ran

    real_run_once = covers.run_once
    monkeypatch.setattr(covers, "housekeep", crash)
    monkeypatch.setattr(covers, "run_once", run_once)
    covers.run_forever(stop)
    assert finder.calls == [make_track().title]


def test_a_download_whose_decision_was_pruned_does_nothing(tmp_path: Path) -> None:
    now = [1000.0]
    store = Store(None)
    fetch = Fetch()
    covers = service(Finder(), now, store=store, fetch=fetch, folder=tmp_path)
    track = make_track()
    store.put_cover(CoverDecision(track.key, "cover", URL, "m", now[0]))
    covers.request(track, NOW)
    now[0] += 366 * DAY
    covers.housekeep()
    assert covers.run_once()
    assert fetch.urls == [] and list(tmp_path.iterdir()) == []


def test_a_download_that_crosses_the_cap_cleans_in_the_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("tunebox.coverfiles.MAX_COVERS", len(JPG) + len(JPG) // 2)
    monkeypatch.setattr("tunebox.coverfiles.LOW_WATER", len(JPG))
    caplog.set_level(logging.INFO)
    urls = {"A": URL, "B": URL.replace("abc", "def")}

    class ByTitle(Finder):
        def __call__(self, track: Track) -> Found | None:
            return Found(urls[track.title], "album", "m")

    covers = service(ByTitle(), [1000.0], folder=tmp_path)
    covers.request(make_track(title="A"), NOW)
    assert covers.run_once()
    assert len(list(tmp_path.glob("*.jpg"))) == 1
    assert not [r for r in caplog.records if r.getMessage() == "cover_cache_cleaned"]
    covers.request(make_track(title="B"), NOW)
    assert covers.run_once()
    assert len(list(tmp_path.glob("*.jpg"))) == 1
    cleaned = [r for r in caplog.records if r.getMessage() == "cover_cache_cleaned"]
    assert len(cleaned) == 1


def test_a_low_disk_skips_the_fetch_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    files = CoverFiles(tmp_path, free=lambda: 1, clock=lambda: now[0])
    fetch = Fetch()
    covers = CoverService(
        Store(None), Finder(Found(URL, "album", "m")), files, fetch, clock=lambda: now[0]
    )
    caplog.set_level(logging.DEBUG)
    for title in ("A", "B"):
        covers.request(make_track(title=title), NOW)
        covers.run_once()
    assert fetch.urls == []
    warnings = [
        r
        for r in caplog.records
        if r.getMessage() == "cover_disk_low" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if r.getMessage() == "cover_download_failed"]
    covers.request(make_track(title="A"), NOW)
    assert not covers.run_once()  # the retry pause is set


URL2 = URL.replace("abc", "def")


def replaced_after_expiry(
    fetch_answer: bytes | HttpError,
) -> tuple[CoverService, list[float], Fetch]:
    now = [1000.0]
    finder = Finder(Found(URL, "album", "m"))
    fetch = Fetch()
    covers = service(finder, now, fetch=fetch)
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] += 91 * DAY
    finder.result = Found(URL2, "album", "m2")
    fetch.answer = fetch_answer
    covers.request(make_track(), NOW)
    assert covers.run_once()
    return covers, now, fetch


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(HttpError("gone", 404), id="gone"),
        pytest.param(HttpError("down", 503), id="unreachable"),
        pytest.param(b"<html>", id="not-an-image"),
        pytest.param(b"\xff\xd8\xff" + b"\x00" * 3_000_000, id="too-large"),
    ],
)
def test_a_failed_replacement_download_keeps_the_working_cover(answer: bytes | HttpError) -> None:
    covers, now, _ = replaced_after_expiry(answer)
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL)
    assert covers.remote_link(make_track()) == URL
    now[0] += 599
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    now[0] += 2
    covers.request(make_track(), NOW)
    assert covers.run_once()  # searched again after the error pause


def test_a_replacement_that_cannot_be_written_keeps_the_working_cover(tmp_path: Path) -> None:
    now = [1000.0]
    finder = Finder(Found(URL, "album", "m"))
    free = [None]
    files = CoverFiles(tmp_path, free=lambda: free[0], clock=lambda: now[0])
    covers = CoverService(Store(None), finder, files, Fetch(), clock=lambda: now[0])
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] += 91 * DAY
    finder.result = Found(URL2, "album", "m2")
    free[0] = 1  # type: ignore[call-overload]
    covers.request(make_track(), NOW)
    covers.run_once()
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL)
    assert covers.remote_link(make_track()) == URL


def test_a_successful_replacement_switches_to_the_new_cover() -> None:
    covers, _, fetch = replaced_after_expiry(JPG)
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(f"/covers/{name_for(URL2)}.jpg")
    assert covers.remote_link(make_track()) == URL2
    assert len(fetch.urls) == 2  # downloaded once, not again after storing


class Flaky(Fetch):
    crashing = True

    def __call__(self, url: str) -> bytes:
        if self.crashing:
            self.urls.append(url)
            raise RuntimeError("bug")
        return super().__call__(url)


def test_crashes_do_not_count_toward_the_warning(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    fetch = Flaky(HttpError("down", 503))
    covers = service(Finder(Found(URL, "album", "m")), now, fetch=fetch)
    caplog.set_level(logging.INFO)
    covers.request(make_track(), NOW)
    covers.run_once()
    for _ in range(3):
        now[0] += 601
        covers.request(make_track(), NOW)
        covers.run_once()
    assert len(fetch.urls) == 4
    fetch.crashing = False
    now[0] += 601
    covers.request(make_track(), NOW)
    covers.run_once()
    levels = [r.levelno for r in caplog.records if r.getMessage() == "cover_download_failed"]
    assert levels == [logging.INFO]


@pytest.mark.parametrize("known", [True, False])
def test_the_cleaning_log_says_whether_the_links_were_known(
    known: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(None)
    covers = service(Finder(), [1000.0], store)
    if not known:
        monkeypatch.setattr(store, "cover_links", lambda: None)
    caplog.set_level(logging.INFO)
    covers.housekeep()
    cleaned = [r for r in caplog.records if r.getMessage() == "cover_cache_cleaned"]
    assert len(cleaned) == 1 and getattr(cleaned[0], "fields", {})["links_known"] is known
