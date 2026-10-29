import json
import logging
from pathlib import Path
from typing import cast

import pytest

from tests.fakes import COVER_JPG, FakeHttp, blob_store, fetch_cover, make_track
from tests.test_imagemeta import GPS, phone_jpeg
from tunebox.catalogs import Catalogs
from tunebox.coverfiles import MAX_COVER, CoverFiles, name_for
from tunebox.coverfix import CoverFixes, View
from tunebox.coverpanel import IMAGE_PATH, CoverPanel, NotACover, StaleCandidate
from tunebox.covers import CoverService
from tunebox.coversearch import CoverSearch
from tunebox.settings import Settings
from tunebox.store import Store

LINK = "https://cdn-images.dzcdn.net/images/cover/" + "c" * 32 + "/1000x1000-000000-80-0-0.jpg"


class Panel:
    """A CoverPanel on real pieces in tmp_path, with a catalogue that
    answers one Deezer cover and a fetch that answers a small JPEG."""

    def __init__(self, tmp_path: Path) -> None:
        self.clock = [1_000.0]
        self.settings = Settings(tmp_path / "t.db")
        self.store = Store(tmp_path / "t.db")
        self.files = CoverFiles(tmp_path / "covers", free=lambda: None)
        self.blobs = blob_store(tmp_path / "blobs")
        self.fixes = CoverFixes(self.settings)
        self.service = CoverService(
            self.store,
            lambda _t: None,
            self.files,
            fetch_cover,
            clock=lambda: self.clock[0],
            fixes=self.fixes,
            blob_path=self.blobs.path,
        )
        answer = {
            "data": [{"artist": {"name": "A"}, "album": {"title": "X", "cover_xl": LINK}}],
            "results": [],
        }
        catalogs = Catalogs(FakeHttp(lambda _url: json.dumps(answer).encode()), 0, 0)
        self.search = CoverSearch(catalogs, lambda: b"k" * 32, clock=lambda: self.clock[0])
        self.panel = CoverPanel(
            self.fixes,
            self.service,
            self.store,
            self.files,
            self.blobs,
            self.search,
            clock=lambda: self.clock[0],
        )

    def token(self, key: str = "a|b") -> str:
        choices = self.panel.search("cover", key, "A", "B", "a b")["choices"]
        return cast("list[dict[str, str]]", choices)[0]["token"]

    def rows(self, view: View = "fixed") -> list[dict[str, object]]:
        listing = self.panel.listing(view, "", None)
        return cast("list[dict[str, object]]", listing["rows"])

    def drain(self) -> None:
        while self.service.run_once():
            pass

    def close(self) -> None:
        self.settings.close()


def test_pick_sets_the_fix_and_fetches_it(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    made = Panel(tmp_path)
    made.store.record_heard("owr", [make_track("A", "B", 900_000)])
    token = made.token()
    with caplog.at_level(logging.INFO):
        fix = made.panel.pick(token)
    assert (fix.key, fix.value, fix.source) == ("a|b", LINK, "deezer")
    assert made.fixes.get("a|b") == fix
    assert made.rows()[0]["state"] == "fetching"
    made.drain()
    row = made.rows()[0]
    assert row["state"] == "fix"
    assert row["image"] == f"{IMAGE_PATH}{name_for(LINK)}.jpg"
    assert row["fix"] == {"key": "a|b", "label": fix.label, "source": "deezer", "set_at": 1_000.0}
    assert row["channels"] == ["owr"] and row["last_at"] == 900_000
    fields = [getattr(r, "fields", {}) for r in caplog.records if r.getMessage() == "cover_fix_set"]
    assert fields == [{"key": "a|b", "source": "deezer"}]
    made.close()


def test_a_stale_or_forged_pick_is_refused(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    token = made.token()
    with pytest.raises(StaleCandidate):
        made.panel.pick(token + "x")
    made.clock[0] += 3601
    with pytest.raises(StaleCandidate):
        made.panel.pick(token)
    assert made.fixes.all() == []
    made.close()


def test_clear_goes_back_to_automatic(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    made = Panel(tmp_path)
    made.store.record_heard("owr", [make_track("A", "B", 900_000)])
    made.panel.pick(made.token())
    with caplog.at_level(logging.INFO):
        assert made.panel.clear("a|b") is True
    assert made.fixes.get("a|b") is None
    assert made.panel.clear("a|b") is False
    fields = [
        getattr(r, "fields", {}) for r in caplog.records if r.getMessage() == "cover_fix_cleared"
    ]
    assert fields == [{"key": "a|b"}]
    # The heard song is looked up again: a search, not a fetch of the old fix.
    assert made.rows("all")[0]["state"] == "looking"
    made.close()


def test_an_upload_is_stored_as_a_blob_and_copied_into_the_cover_store(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    fix = made.panel.upload("a|b", "A", "B", COVER_JPG)
    assert (fix.key, fix.source, fix.artist, fix.title) == ("a|b", "upload", "A", "B")
    assert fix.value is not None and made.blobs.path(fix.value) is not None
    assert made.files.path_for(fix.value) is None
    made.drain()
    assert made.files.path_for(fix.value) is not None
    made.close()


def test_an_upload_keeps_no_camera_metadata(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    fix = made.panel.upload("a|b", "A", "B", phone_jpeg())
    assert fix.value is not None
    blob = made.blobs.path(fix.value)
    assert blob is not None and GPS not in blob.read_bytes()
    made.drain()
    cover = made.files.path_for(fix.value)
    assert cover is not None and cover.read_bytes() == blob.read_bytes()
    made.close()


def test_an_upload_must_be_a_raster_cover_for_a_song(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    with pytest.raises(NotACover):
        made.panel.upload("a|b", "A", "B", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(NotACover):
        made.panel.upload("a|b", "A", "B", b"\xff\xd8\xff" + b"\x00" * MAX_COVER)
    with pytest.raises(NotACover):
        made.panel.upload("a|b", "A", "B", b"\xff\xd8\xff\xe1\x00\x40Exif")
    with pytest.raises(ValueError):
        made.panel.upload("photo:a", "A", "", COVER_JPG)
    assert made.fixes.all() == []
    made.close()


def test_row_images_are_cover_store_files_only(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    made.files.save(LINK, COVER_JPG)
    name = f"{name_for(LINK)}.jpg"
    assert made.panel.image(name) == (tmp_path / "covers" / name).resolve()
    assert made.panel.image("../t.db") is None
    made.close()


def test_the_list_says_when_it_cannot_be_read(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    memory = CoverPanel(made.fixes, made.service, Store(None), made.files, made.blobs, made.search)
    listing = memory.listing("all", "", None)
    assert listing["available"] is False and listing["rows"] == []
    assert made.panel.listing("all", "", None)["available"] is True
    made.close()


def test_the_page_cursor_is_ms_and_key(tmp_path: Path) -> None:
    made = Panel(tmp_path)
    made.store.record_heard("owr", [make_track("A", f"Song {n}", 900_000 + n) for n in range(101)])
    listing = made.panel.listing("all", "", None)
    assert listing["next"] == f"900001:{make_track('A', 'Song 1').key}"
    assert listing["totals"] == {"songs": 101, "cover": 0, "artist": 0, "nothing": 0, "fixed": 0}
    made.close()
