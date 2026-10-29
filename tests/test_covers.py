import itertools
import logging
import threading
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from tests.fakes import FakeFixes, cover_fix, make_track
from tunebox import covers as covers_module
from tunebox.catalogs import Found, SearchError
from tunebox.coverfiles import CoverFiles, SaveResult, name_for
from tunebox.coverfix import Fix
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
    def __init__(self, answer: bytes | Exception = JPG) -> None:
        self.answer = answer
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def service(
    finder: Finder,
    now: list[float],
    store: Store | None = None,
    fetch: Fetch | None = None,
    folder: Path | None = None,
    photos: Finder | None = None,
    fixes: FakeFixes | None = None,
    blobs: Path | None = None,
) -> CoverService:
    files = CoverFiles(folder or fresh_folder(), free=lambda: None, clock=lambda: now[0])
    return CoverService(
        store or Store(None),
        finder,
        files,
        fetch or Fetch(),
        clock=lambda: now[0],
        find_photo=photos,
        fixes=fixes,
        blob_path=(lambda ref: blob_file(blobs, ref)) if blobs else None,
    )


def blob_file(blobs: Path, ref: str) -> Path | None:
    path = blobs / f"{ref[5:]}.jpg"
    return path if path.exists() else None


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
    fetch_answer: bytes | Exception,
    before_replacement: Callable[[], None] = lambda: None,
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
    before_replacement()
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
    free: list[int | None] = [None]
    files = CoverFiles(tmp_path, free=lambda: free[0], clock=lambda: now[0])
    covers = CoverService(Store(None), finder, files, Fetch(), clock=lambda: now[0])
    covers.request(make_track(), NOW)
    covers.run_once()
    now[0] += 91 * DAY
    finder.result = Found(URL2, "album", "m2")
    free[0] = 1
    covers.request(make_track(), NOW)
    covers.run_once()
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL)
    assert covers.remote_link(make_track()) == URL


def test_a_replacement_whose_fetch_crashes_keeps_the_working_cover() -> None:
    covers, _, _ = replaced_after_expiry(RuntimeError("boom"))
    cover = covers.lookup(make_track())
    assert cover is not None and cover.url.startswith(LOCAL)
    assert covers.remote_link(make_track()) == URL


def test_a_replacement_the_folder_refuses_keeps_the_working_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_self: CoverFiles, _link: str, _body: bytes) -> SaveResult:
        return SaveResult.UNAVAILABLE

    def refuse() -> None:
        monkeypatch.setattr(CoverFiles, "save", unavailable)

    covers, _, _ = replaced_after_expiry(JPG, refuse)
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
    assert len(cleaned) == 1
    fields = getattr(cleaned[0], "fields", {})
    assert fields["links_known"] is known
    assert fields["trigger"] == "daily"


def test_the_cleaning_after_a_download_names_its_trigger_and_no_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("tunebox.coverfiles.MAX_COVERS", 1)
    monkeypatch.setattr("tunebox.coverfiles.LOW_WATER", 0)
    covers = service(Finder(Found(URL, "album", "m")), [1000.0], folder=tmp_path)
    caplog.set_level(logging.INFO)
    covers.request(make_track(), NOW)
    covers.run_once()
    cleaned = [r for r in caplog.records if r.getMessage() == "cover_cache_cleaned"]
    assert len(cleaned) == 1
    fields = getattr(cleaned[0], "fields", {})
    assert fields["trigger"] == "cap" and fields["links_known"] is None


PHOTO_URL = "https://example.invalid/images/artist/def/1000x1000-000000-80-0-0.jpg"
ALBUM = Found(URL, "album", "deezer: Fisher - Stay")
PHOTO = Found(PHOTO_URL, "artist", "deezer artist: Fisher")


def test_a_song_with_its_own_cover_gets_its_artists_photo() -> None:
    photos = Finder(PHOTO)
    covers = service(Finder(ALBUM), [0.0], photos=photos)
    track = make_track()
    covers.request(track, NOW)
    drain(covers)
    assert covers.photo(track) is None
    # The next request, as the channel makes on every poll, queues the photo.
    covers.request(track, NOW)
    drain(covers)
    photo = covers.photo(track)
    assert photo is not None and photo.startswith(f"/covers/{name_for(PHOTO_URL)}.jpg?v=")
    assert photos.calls == ["Stay"]
    cover = covers.lookup(track)
    assert cover is not None and cover.kind == "album" and cover.url != photo


def test_songs_by_the_same_artists_share_one_photo() -> None:
    photos = Finder(PHOTO)
    covers = service(Finder(ALBUM), [0.0], photos=photos)
    for title in ("Stay", "Losing It", "Stay"):
        track = make_track("FISHER", title)
        covers.request(track, NOW)
        drain(covers)
        covers.request(track, NOW)
        drain(covers)
    assert photos.calls == ["Stay"]
    assert covers.photo(make_track("FISHER", "Losing It")) == covers.photo(make_track())


def test_a_photo_waits_for_every_song() -> None:
    finder = Finder(ALBUM)
    photos = Finder(PHOTO)
    covers = service(finder, [0.0], photos=photos)
    covers.request(make_track("FISHER", "Stay"), NOW)
    drain(covers)
    covers.request(make_track("FISHER", "Stay"), NOW)
    covers.request(make_track("CHRIS LAKE", "Turn Off The Lights"), HISTORY)
    covers.run_once()
    assert finder.calls == ["Stay", "Turn Off The Lights"]
    assert photos.calls == []
    covers.run_once()
    assert photos.calls == ["Stay"]


def test_no_photo_beside_a_photo_or_for_history() -> None:
    photos = Finder(PHOTO)
    covers = service(Finder(PHOTO), [0.0], photos=photos)
    track = make_track()
    for _ in range(2):
        covers.request(track, NOW)
        drain(covers)
    assert photos.calls == []
    covers = service(Finder(ALBUM), [0.0], photos=photos)
    for _ in range(2):
        covers.request(track, HISTORY)
        drain(covers)
    assert photos.calls == []


def test_without_a_photo_source_nothing_is_queued() -> None:
    finder = Finder(ALBUM)
    covers = service(finder, [0.0])
    covers.request(make_track(), NOW)
    drain(covers)
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    assert covers.photo(make_track()) is None


def test_a_photo_search_that_fails_is_tried_again_later() -> None:
    now = [0.0]
    photos = Finder(SearchError("timed out"))
    covers = service(Finder(ALBUM), now, photos=photos)
    for _ in range(2):
        covers.request(make_track(), NOW)
        drain(covers)
    assert photos.calls == ["Stay"]
    covers.request(make_track(), NOW)
    assert not covers.run_once()
    photos.result = PHOTO
    now[0] += covers_module.TTL["error"]
    covers.request(make_track(), NOW)
    drain(covers)
    assert covers.photo(make_track()) is not None


FIX_URL = "https://cdn-images.dzcdn.net/images/cover/" + "f" * 32 + "/1000x1000-000000-80-0-0.jpg"
FIX_PHOTO = (
    "https://cdn-images.dzcdn.net/images/artist/" + "e" * 32 + "/1000x1000-000000-80-0-0.jpg"
)
SONG = make_track()


def test_a_cover_fix_is_fetched_and_shown_without_a_search() -> None:
    finder, fetch = Finder(Found(URL, "album", "deezer: Fisher - Stay")), Fetch()
    covers = service(finder, [0.0], fetch=fetch, fixes=FakeFixes(cover_fix(SONG.key, FIX_URL)))
    covers.request(SONG, NOW)
    drain(covers)
    assert finder.calls == []
    assert fetch.urls == [FIX_URL.replace("/1000x1000-", "/600x600-")]
    cover = covers.lookup(SONG)
    assert cover is not None and cover.kind == "album"
    assert cover.url.startswith(f"/covers/{name_for(FIX_URL)}.jpg?v=")
    assert covers.remote_link(SONG) == FIX_URL


def test_a_fix_older_than_every_ttl_is_never_searched() -> None:
    now = [0.0]
    store = Store(None)
    store.put_cover(CoverDecision(SONG.key, "cover", URL, "deezer: Fisher - Stay", 0.0))
    finder = Finder(Found(URL, "album", "x"))
    covers = service(finder, now, store=store, fixes=FakeFixes(cover_fix(SONG.key, FIX_URL)))
    now[0] = 400 * DAY
    covers.request(SONG, NOW)
    drain(covers)
    assert finder.calls == []


def test_a_photo_fix_replaces_an_automatic_artist_photo() -> None:
    fixes = FakeFixes(cover_fix(SONG.photo_key, FIX_PHOTO))
    covers = service(Finder(Found(URL, "artist", "deezer artist: Fisher")), [0.0], fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    cover = covers.lookup(SONG)
    assert cover is not None and cover.kind == "artist"
    assert cover.url.startswith(f"/covers/{name_for(FIX_PHOTO)}.jpg")
    assert covers.remote_link(SONG) == FIX_PHOTO


def test_no_photo_shows_no_image_for_an_artist_answer() -> None:
    fixes = FakeFixes(cover_fix(SONG.photo_key, None, "none"))
    covers = service(Finder(Found(URL, "artist", "deezer artist: Fisher")), [0.0], fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    assert covers.lookup(SONG) is None


def test_a_photo_fix_is_the_other_side_of_an_own_cover() -> None:
    photos = Finder(PHOTO)
    fixes = FakeFixes(cover_fix(SONG.photo_key, FIX_PHOTO))
    covers = service(Finder(ALBUM), [0.0], photos=photos, fixes=fixes)
    for _ in range(2):
        covers.request(SONG, NOW)
        drain(covers)
    photo = covers.photo(SONG)
    assert photo is not None and photo.startswith(f"/covers/{name_for(FIX_PHOTO)}.jpg")
    assert photos.calls == []


def test_an_evicted_photo_fix_on_an_artist_answer_is_fetched_again(tmp_path: Path) -> None:
    fetch = Fetch()
    fixes = FakeFixes(cover_fix(SONG.photo_key, FIX_PHOTO))
    covers = service(
        Finder(Found(URL, "artist", "deezer artist: Fisher")),
        [0.0],
        fetch=fetch,
        folder=tmp_path,
        fixes=fixes,
    )
    for _ in range(2):
        covers.request(SONG, NOW)
        drain(covers)
    (tmp_path / f"{name_for(FIX_PHOTO)}.jpg").unlink()
    covers.request(SONG, NOW)
    drain(covers)
    assert fetch.urls.count(FIX_PHOTO.replace("/1000x1000-", "/600x600-")) == 2
    assert covers.lookup(SONG) is not None


def test_a_photo_fix_is_fetched_for_an_unsearchable_song_at_its_priority() -> None:
    track = make_track("東京", "Stay")
    store = Store(None)
    store.put_cover(CoverDecision(track.key, "artist", URL, "deezer artist: x", 0.0))
    finder, fetch = Finder(), Fetch()
    fixes = FakeFixes(cover_fix(track.photo_key, FIX_PHOTO))
    covers = service(finder, [0.0], store=store, fetch=fetch, fixes=fixes)
    covers.request(track, HISTORY)
    covers.request(make_track("A", "New"), NOW)
    assert covers.run_once()
    assert finder.calls == ["New"] and fetch.urls == []
    drain(covers)
    assert fetch.urls == [FIX_PHOTO.replace("/1000x1000-", "/600x600-")]
    cover = covers.lookup(track)
    assert cover is not None and cover.url.startswith(f"/covers/{name_for(FIX_PHOTO)}.jpg")


def test_the_automatic_cover_stays_until_the_fix_is_in_the_store() -> None:
    fixes = FakeFixes()
    covers = service(Finder(ALBUM), [0.0], fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    fixes.map[SONG.key] = cover_fix(SONG.key, FIX_URL)
    covers.fix_set(SONG.key)
    cover = covers.lookup(SONG)
    assert cover is not None and cover.url.startswith(LOCAL) and cover.kind == "album"
    drain(covers)
    cover = covers.lookup(SONG)
    assert cover is not None and cover.url.startswith(f"/covers/{name_for(FIX_URL)}.jpg")


def test_the_automatic_photo_stays_until_the_photo_fix_is_in_the_store() -> None:
    fixes = FakeFixes()
    covers = service(Finder(ALBUM), [0.0], photos=Finder(PHOTO), fixes=fixes)
    for _ in range(2):
        covers.request(SONG, NOW)
        drain(covers)
    automatic = covers.photo(SONG)
    assert automatic is not None
    fixes.map[SONG.photo_key] = cover_fix(SONG.photo_key, FIX_PHOTO)
    assert covers.photo(SONG) == automatic
    artist = service(Finder(PHOTO), [0.0], fixes=fixes)
    fixes.map.clear()
    artist.request(SONG, NOW)
    drain(artist)
    fixes.map[SONG.photo_key] = cover_fix(SONG.photo_key, FIX_PHOTO)
    cover = artist.lookup(SONG)
    assert cover is not None and cover.kind == "artist"
    assert cover.url.startswith(f"/covers/{name_for(PHOTO_URL)}.jpg")


def test_a_failed_fix_fetch_never_becomes_a_decision() -> None:
    now = [0.0]
    store = Store(None)
    fetch = Fetch(HttpError("gone", 404))
    covers = service(
        Finder(), now, store=store, fetch=fetch, fixes=FakeFixes(cover_fix(SONG.key, FIX_URL))
    )
    covers.request(SONG, NOW)
    drain(covers)
    assert store.get_cover(SONG.key) is None
    assert FIX_URL not in (store.cover_links() or set())
    assert covers.fetch_failure(SONG.key) == "gone"
    # Paused: no new try within RETRY_S, one after it, and at once on a re-pick.
    covers.request(SONG, NOW)
    assert not covers.run_once()
    now[0] += covers_module.RETRY_S
    covers.request(SONG, NOW)
    assert covers.run_once()
    covers.fix_set(SONG.key)
    assert covers.run_once()
    assert len(fetch.urls) == 3


def test_a_search_error_while_a_fix_exists_keeps_the_fix_out_of_covers() -> None:
    store = Store(None)
    store.put_cover(CoverDecision(SONG.key, "cover", URL, "m", 0.0))
    fixes = FakeFixes()
    finder = Finder(SearchError("down"))
    now = [0.0]
    covers = service(finder, now, store=store, fixes=fixes)
    now[0] = 100 * DAY
    covers.request(SONG, NOW)  # queues a search: no fix yet
    fixes.map[SONG.key] = cover_fix(SONG.key, FIX_URL)
    covers.fix_set(SONG.key)
    drain(covers)
    decision = store.get_cover(SONG.key)
    assert decision is not None and decision.url == URL and decision.outcome == "error"


def test_a_fix_set_during_a_search_is_still_fetched() -> None:
    fixes = FakeFixes()
    fetch = Fetch()
    covers: CoverService | None = None

    class SettingFinder(Finder):
        def __call__(self, track: Track) -> Found | None:
            assert covers is not None
            fixes.map[track.key] = cover_fix(track.key, FIX_URL)
            covers.fix_set(track.key)
            return super().__call__(track)

    covers = service(SettingFinder(Found(URL, "album", "x")), [0.0], fetch=fetch, fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    assert FIX_URL.replace("/1000x1000-", "/600x600-") in fetch.urls
    cover = covers.lookup(SONG)
    assert cover is not None and cover.url.startswith(f"/covers/{name_for(FIX_URL)}")


def test_an_unsearchable_song_with_a_fix_is_fetched() -> None:
    track = make_track("東京", "Stay")
    covers = service(Finder(), [0.0], fixes=FakeFixes(cover_fix(track.key, FIX_URL)))
    covers.request(track, NOW)
    drain(covers)
    assert covers.lookup(track) is not None


def test_back_to_automatic_forgets_the_answer_and_searches_again() -> None:
    store = Store(None)
    finder = Finder(Found(URL, "album", "x"))
    fixes = FakeFixes()
    covers = service(finder, [0.0], store=store, fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    fixes.map[SONG.key] = cover_fix(SONG.key, FIX_URL)
    covers.fix_set(SONG.key)
    drain(covers)
    del fixes.map[SONG.key]
    covers.fix_cleared(SONG.key, SONG)
    assert store.get_cover(SONG.key) is None
    assert covers.lookup(SONG) is None
    drain(covers)
    assert finder.calls == ["Stay", "Stay"]


def test_back_to_automatic_without_a_heard_song_waits_for_the_next_play() -> None:
    finder = Finder(Found(URL, "album", "x"))
    covers = service(finder, [0.0], fixes=FakeFixes())
    covers.fix_cleared(SONG.key, None)
    assert not covers.run_once()


def test_an_upload_is_copied_from_its_blob_and_rebuilt(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    ref = "blob:" + "c" * 64
    (blobs / f"{'c' * 64}.jpg").write_bytes(JPG)
    fetch = Fetch()
    folder = tmp_path / "covers"
    covers = service(
        Finder(),
        [0.0],
        fetch=fetch,
        folder=folder,
        blobs=blobs,
        fixes=FakeFixes(cover_fix(SONG.key, ref, "upload")),
    )
    covers.request(SONG, NOW)
    drain(covers)
    assert fetch.urls == []
    assert (folder / f"{name_for(ref)}.jpg").read_bytes() == JPG
    assert covers.remote_link(SONG) is None
    (folder / f"{name_for(ref)}.jpg").unlink()
    covers.request(SONG, NOW)
    drain(covers)
    assert covers.lookup(SONG) is not None


def test_a_lost_blob_is_logged_once_and_reads_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [0.0]
    ref = "blob:" + "c" * 64
    covers = service(
        Finder(), now, blobs=tmp_path, fixes=FakeFixes(cover_fix(SONG.key, ref, "upload"))
    )
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            covers.request(SONG, NOW)
            drain(covers)
            now[0] += covers_module.RETRY_S
    assert [r.getMessage() for r in caplog.records] == ["cover_fix_blob_missing"]
    assert covers.fetch_failure(SONG.key) == "lost"


def test_a_fix_off_the_catalogue_hosts_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    fetch = Fetch()
    covers = service(
        Finder(),
        [0.0],
        fetch=fetch,
        fixes=FakeFixes(cover_fix(SONG.key, "https://example.invalid/x.jpg")),
    )
    with caplog.at_level(logging.WARNING):
        covers.request(SONG, NOW)
        drain(covers)
    assert fetch.urls == []
    (refused,) = [r for r in caplog.records if r.getMessage() == "cover_fix_refused"]
    assert getattr(refused, "fields", {}) == {"key": SONG.key, "why": "host"}
    assert covers.fetch_failure(SONG.key) == "refused"


def test_housekeeping_keeps_fixed_images_and_prunes_heard(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [40 * DAY]
    store = Store(tmp_path / "t.db")
    store.record_heard(
        "owr", [make_track("A", "Old", 1_000), make_track("B", "New", int(39 * DAY * 1000))]
    )
    folder = tmp_path / "covers"
    folder.mkdir()
    (folder / f"{name_for(FIX_URL)}.jpg").write_bytes(JPG)
    (folder / f"{name_for(URL)}.jpg").write_bytes(JPG)
    covers = service(
        Finder(), now, store=store, folder=folder, fixes=FakeFixes(cover_fix(SONG.key, FIX_URL))
    )
    with caplog.at_level(logging.INFO):
        covers.housekeep()
    assert (folder / f"{name_for(FIX_URL)}.jpg").exists()
    assert not (folder / f"{name_for(URL)}.jpg").exists()
    assert [s.key for s in store.heard_songs(0) or []] == ["b|new"]
    (cleaned,) = [r for r in caplog.records if r.getMessage() == "cover_cache_cleaned"]
    assert getattr(cleaned, "fields", {})["heard_removed"] == 1


def test_housekeeping_keeps_every_file_while_the_fixes_are_unknown(tmp_path: Path) -> None:
    folder = tmp_path / "covers"
    folder.mkdir()
    (folder / f"{name_for(URL)}.jpg").write_bytes(JPG)
    covers = service(
        Finder(),
        [0.0],
        store=Store(tmp_path / "t.db"),
        folder=folder,
        fixes=FakeFixes(known=False),
    )
    covers.housekeep()
    assert (folder / f"{name_for(URL)}.jpg").exists()


def test_a_fetch_whose_fix_was_cleared_while_it_waited_does_nothing() -> None:
    fetch = Fetch()
    fixes = FakeFixes(cover_fix(SONG.key, FIX_URL))
    covers = service(Finder(), [0.0], fetch=fetch, fixes=fixes)
    covers.request(SONG, NOW)
    fixes.map.clear()
    assert covers.run_once()
    assert fetch.urls == [] and not covers.run_once()


def test_going_back_to_automatic_forgets_the_fix_fetch_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetch = Fetch(HttpError("gone", 404))
    gone = cover_fix(SONG.key, FIX_URL)
    refused = cover_fix(SONG.key, "https://example.invalid/x.jpg")
    fixes = FakeFixes(gone)
    covers = service(Finder(), [0.0], fetch=fetch, fixes=fixes)

    def again(fix: Fix) -> None:
        """Clear the fix, then set the same value without fix_set."""
        fixes.map.clear()
        covers.fix_cleared(SONG.key, None)
        fixes.map[SONG.key] = fix
        covers.request(SONG, NOW)
        drain(covers)

    covers.request(SONG, NOW)
    drain(covers)
    again(gone)
    # No pause left from the earlier failure of the same value.
    assert len(fetch.urls) == 2
    with caplog.at_level(logging.WARNING):
        again(refused)
        again(refused)
    # Told once per fix: a cleared fix set again is told again.
    assert [r.getMessage() for r in caplog.records] == ["cover_fix_refused"] * 2


@pytest.mark.parametrize(
    ("answer", "free", "blocked", "reason"),
    [
        (b"<html></html>", [1 << 40], False, "not_image"),
        (RuntimeError("bug"), [1 << 40], False, "crashed"),
        (JPG, [1], False, "disk_low"),
        (JPG, [1 << 40, 1], False, "disk_low"),
        (JPG, [1 << 40], True, "unwritable"),
    ],
)
def test_a_fix_fetch_that_cannot_be_saved_pauses_with_its_reason(
    tmp_path: Path, answer: bytes | Exception, free: list[int], blocked: bool, reason: str
) -> None:
    # free: what the disk reports on each look, the last value repeating.
    looks = iter(free)
    folder = tmp_path / "covers"
    if blocked:
        (tmp_path / "file").write_bytes(b"")
        folder = tmp_path / "file" / "covers"
    files = CoverFiles(folder, free=lambda: next(looks, free[-1]), clock=lambda: 0.0)
    covers = CoverService(
        Store(None),
        Finder(),
        files,
        Fetch(answer),
        clock=lambda: 0.0,
        fixes=FakeFixes(cover_fix(SONG.key, FIX_URL)),
    )
    covers.request(SONG, NOW)
    drain(covers)
    assert covers.fetch_failure(SONG.key) == reason
    assert covers.lookup(SONG) is None
    covers.request(SONG, NOW)
    assert not covers.run_once()


def test_a_re_pick_during_a_failing_fetch_is_fetched_at_once() -> None:
    fixes = FakeFixes(cover_fix(SONG.key, FIX_URL))
    covers: CoverService | None = None

    class RePick(Fetch):
        def __call__(self, url: str) -> bytes:
            self.urls.append(url)
            if url == FIX_URL.replace("/1000x1000-", "/600x600-"):
                assert covers is not None
                fixes.map[SONG.key] = cover_fix(SONG.key, FIX_PHOTO)
                covers.fix_set(SONG.key)
                raise HttpError("busy", 503)
            return JPG

    fetch = RePick()
    covers = service(Finder(), [0.0], fetch=fetch, fixes=fixes)
    covers.request(SONG, NOW)
    drain(covers)
    covers.request(SONG, NOW)
    drain(covers)
    assert fetch.urls == [
        FIX_URL.replace("/1000x1000-", "/600x600-"),
        FIX_PHOTO.replace("/1000x1000-", "/600x600-"),
    ]
    cover = covers.lookup(SONG)
    assert cover is not None and cover.url.startswith(f"/covers/{name_for(FIX_PHOTO)}.jpg")
    assert covers.fetch_failure(SONG.key) is None
