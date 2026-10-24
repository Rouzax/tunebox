import dataclasses
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from tests.fakes import FakeHttp, channel_config
from tunebox.blobs import Blobs
from tunebox.http import HttpError
from tunebox.logos import SMALL_PX, Logos, default_image_address, sniff
from tunebox.png import Image, read, write

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
URL = "https://example.invalid/owr.png"
DEFAULT_URL = "https://example.invalid/default.jpg"
BIG_PNG = write(Image(300, 150, bytes((200, 30, 30, 255)) * 300 * 150))


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (PNG, "png"),
        (JPG, "jpg"),
        (WEBP, "webp"),
        (SVG, "svg"),
        (b"  <svg></svg>", "svg"),
        (b"\xef\xbb\xbf<svg></svg>", "svg"),
        (b"<!-- made by hand -->\n<svg></svg>", "svg"),
        (b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN">\n<svg></svg>', "svg"),
        (b"<!-- page -->\n<html><body><svg></svg></body></html>", None),
        (b"<!doctype html><html>", None),
        (b"", None),
        (b"RIFF\x10\x00\x00\x00WAVE", None),
    ],
)
def test_sniff(body: bytes, kind: str | None) -> None:
    assert sniff(body) == kind


def test_fetch_saves_once_and_remembers_the_address(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    channels = [channel_config(logo=URL)]
    logos.refresh(channels[0])
    logos.refresh(channels[0])
    assert http.urls == [URL]
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.file("owr.png") == tmp_path / "owr.png"


def test_the_logo_path_changes_with_the_file(tmp_path: Path) -> None:
    # Logos are cached for a year, so a replaced logo needs a new address.
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    before = logos.path_for("owr")
    os.utime(tmp_path / "owr.png", ns=(1, 1))
    assert logos.path_for("owr") != before


def test_a_changed_address_fetches_again_and_replaces_the_old_file(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )
    other = "https://example.invalid/owr.svg"
    Logos(tmp_path, FakeHttp(lambda _url: SVG), Blobs(tmp_path / "blobs")).refresh(
        channel_config(logo=other)
    )
    assert not (tmp_path / "owr.png").exists()
    later = Logos(tmp_path, FakeHttp(lambda _url: b""), Blobs(tmp_path / "blobs"))
    assert (later.path_for("owr") or "").startswith("/logos/owr.svg?v=")


def test_a_page_instead_of_an_image_is_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    logos = Logos(tmp_path, FakeHttp(lambda _url: b"<html>"), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert logos.path_for("owr") is None
    assert "logo_rejected" in caplog.text


def test_a_failed_download_keeps_the_old_logo(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )

    def fail(_url: str) -> bytes:
        raise HttpError("down")

    logos = Logos(tmp_path, fail, Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo="https://example.invalid/new.png"))
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")


def test_channels_without_a_logo_are_skipped(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    Logos(tmp_path, http, Blobs(tmp_path / "blobs")).refresh(channel_config())
    assert http.urls == []


@pytest.mark.parametrize("name", ["../owr.png", "owr.png/..", ".owr.tmp", "owr.url", "owr.exe", ""])
def test_file_refuses_other_names(tmp_path: Path, name: str) -> None:
    (tmp_path / "owr.url").write_text(URL, encoding="utf-8")
    assert Logos(tmp_path, FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs")).file(name) is None


def test_an_unusable_folder_is_reported_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    logos = Logos(blocker / "logos", FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    logos.refresh(channel_config(logo=URL))
    assert [r.getMessage() for r in caplog.records].count("logos_unavailable") == 1


def test_a_large_png_gets_a_small_copy_for_the_station_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    small = read((tmp_path / "small" / "owr.png").read_bytes())
    assert (small.width, small.height) == (SMALL_PX, SMALL_PX // 2)
    assert (logos.path_for("owr", small=True) or "").startswith("/logos/small/owr.png?v=")
    assert logos.file("owr.png", small=True) == tmp_path / "small" / "owr.png"
    assert (tmp_path / "owr.png").read_bytes() == BIG_PNG
    assert "logo_shrunk" in caplog.text


def test_logos_saved_before_get_their_small_copy_without_a_download(tmp_path: Path) -> None:
    (tmp_path / "owr.png").write_bytes(BIG_PNG)
    (tmp_path / "owr.url").write_text(URL, encoding="utf-8")
    http = FakeHttp(lambda _url: BIG_PNG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert http.urls == []
    assert logos.path_for("owr", small=True) is not None


def test_a_logo_that_is_small_already_has_no_copy(tmp_path: Path) -> None:
    tiny = write(Image(64, 64, bytes((0, 0, 0, 255)) * 64 * 64))
    logos = Logos(tmp_path, FakeHttp(lambda _url: tiny), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert logos.path_for("owr", small=True) is None


def test_a_replaced_logo_drops_or_renews_its_small_copy(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), Blobs(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )
    other = "https://example.invalid/owr.svg"
    logos = Logos(tmp_path, FakeHttp(lambda _url: SVG), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=other))
    assert not (tmp_path / "small" / "owr.png").exists()
    assert logos.path_for("owr", small=True) is None


def test_an_unreadable_png_is_kept_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    broken = PNG[:8] + b"\x00\x00\x00\x0dIHDR" + b"\x00\x00\x02\x00" * 2 + b"\x08\x02\x00\x00\x00"
    logos = Logos(tmp_path, FakeHttp(lambda _url: broken), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.path_for("owr", small=True) is None
    assert "logo_not_shrunk" in caplog.text


def test_requested_logos_are_fetched_by_the_worker(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    logos.request(channel_config(logo=URL))
    stop = threading.Event()
    worker = threading.Thread(target=logos.run_forever, args=(stop,))
    worker.start()
    deadline = time.monotonic() + 5
    while logos.path_for("owr") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(5)
    assert http.urls == [URL]
    assert not worker.is_alive()


def test_a_blob_logo_is_copied_like_a_download(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path / "blobs")
    ref, _ = blobs.save(BIG_PNG)
    fetched: list[str] = []
    logos = Logos(tmp_path, FakeHttp(lambda url: fetched.append(url) or b""), blobs)
    logos.refresh(channel_config(logo=ref))
    assert (tmp_path / "owr.png").read_bytes() == BIG_PNG
    assert (tmp_path / "owr.url").read_text(encoding="utf-8") == ref
    assert (tmp_path / "small" / "owr.png").is_file()
    assert logos.path_for("owr", small=True) is not None
    assert fetched == []


def test_a_blob_logo_is_copied_once(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path / "blobs")
    ref, _ = blobs.save(BIG_PNG)
    logos = Logos(tmp_path, FakeHttp(lambda _url: b""), blobs)
    logos.refresh(channel_config(logo=ref))
    before = (tmp_path / "owr.png").stat().st_mtime_ns
    logos.refresh(channel_config(logo=ref))
    assert (tmp_path / "owr.png").stat().st_mtime_ns == before


def test_a_blob_logo_replaces_a_downloaded_one_of_another_kind(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path / "blobs")
    ref, kind = blobs.save(SVG)
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blobs)
    logos.refresh(channel_config(logo=URL))
    logos.refresh(channel_config(logo=ref))
    assert kind == "svg"
    assert not (tmp_path / "owr.png").exists()
    assert (tmp_path / "owr.svg").read_bytes() == SVG


def test_a_missing_blob_keeps_the_old_logo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "owr.png").write_bytes(PNG)
    fetched: list[str] = []
    logos = Logos(
        tmp_path, FakeHttp(lambda url: fetched.append(url) or b""), Blobs(tmp_path / "blobs")
    )
    logos.refresh(channel_config(logo="blob:" + "2" * 64))
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert "logo_missing" in [r.getMessage() for r in caplog.records]
    assert fetched == []


def test_a_blob_logo_gives_way_to_an_address_again(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path / "blobs")
    ref, _ = blobs.save(SVG)
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blobs)
    logos.refresh(channel_config(logo=ref))
    logos.refresh(channel_config(logo=URL))
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert not (tmp_path / "owr.svg").exists()
    assert (tmp_path / "owr.url").read_text(encoding="utf-8") == URL


def test_a_refresh_leaves_the_blob_folder_alone(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path / "blobs")
    ref, _ = blobs.save(BIG_PNG)
    other, _ = blobs.save(SVG)
    stray = tmp_path / "blobs" / ".stray.tmp"
    stray.write_bytes(b"x")
    before = sorted(p.name for p in (tmp_path / "blobs").iterdir())
    logos = Logos(tmp_path, FakeHttp(lambda _url: JPG), blobs)
    logos.refresh(channel_config(logo=ref))
    logos.refresh(channel_config(logo=URL))
    logos.refresh(channel_config(logo=other))
    logos.refresh(channel_config())
    assert sorted(p.name for p in (tmp_path / "blobs").iterdir()) == before
    assert logos.file("blobs.png") is None


def test_a_blob_that_is_not_its_kind_is_not_copied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    blobs = Blobs(tmp_path / "blobs")
    ref, _ = blobs.save(PNG)
    stored = blobs.path(ref)
    assert stored is not None
    stored.write_bytes(b"")
    Logos(tmp_path, FakeHttp(lambda _url: b""), blobs).refresh(channel_config(logo=ref))
    assert not (tmp_path / "owr.png").exists()
    assert "logo_rejected" in caplog.text


def test_a_default_image_is_downloaded_beside_the_logo(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: JPG if url == DEFAULT_URL else PNG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    channel = channel_config(logo=URL, default_image=DEFAULT_URL)
    logos.refresh(channel)
    logos.refresh(channel)
    assert sorted(http.urls) == sorted([URL, DEFAULT_URL])
    assert (tmp_path / "default" / "owr.jpg").read_bytes() == JPG
    assert (tmp_path / "default" / "owr.url").read_text(encoding="utf-8") == DEFAULT_URL
    assert (logos.path_for("owr", default=True) or "").startswith("/logos/default/owr.jpg?v=")
    assert logos.file("owr.jpg", default=True) == tmp_path / "default" / "owr.jpg"
    assert logos.file("owr.jpg") is None


def test_a_changed_default_image_replaces_the_old_file(tmp_path: Path) -> None:
    bodies = {DEFAULT_URL: JPG, "https://example.invalid/other.png": PNG}
    logos = Logos(tmp_path, FakeHttp(lambda url: bodies[url]), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))
    logos.refresh(channel_config(default_image="https://example.invalid/other.png"))
    assert sorted(p.name for p in (tmp_path / "default").iterdir()) == ["owr.png", "owr.url"]


@pytest.mark.parametrize("value", ["/img/station.png", None, "data:image/png;base64,AAAA"])
def test_a_default_image_that_is_not_downloaded_removes_the_old_file(
    tmp_path: Path, value: str | None
) -> None:
    http = FakeHttp(lambda _url: JPG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))
    assert logos.path_for("owr", default=True) is not None
    logos.refresh(channel_config(default_image=value))
    assert logos.path_for("owr", default=True) is None
    assert not (tmp_path / "default" / "owr.url").exists()
    assert http.urls == [DEFAULT_URL]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.invalid/d.jpg", "/logos/default/owr.jpg?v=1"),
        ("/img/station.png", "/img/station.png"),
        ("//elsewhere.invalid/d.jpg", None),
        ("http://example.invalid/d.jpg", None),
        ("data:image/png;base64,AAAA", None),
        ("img/station.png", None),
        (None, None),
    ],
)
def test_default_image_address(value: str | None, expected: str | None) -> None:
    assert default_image_address(value, "/logos/default/owr.jpg?v=1") == expected


def test_an_https_default_image_not_yet_downloaded_has_no_address() -> None:
    assert default_image_address("https://example.invalid/d.jpg", None) is None


def test_an_unusable_default_image_is_not_fetched_and_warned_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    http = FakeHttp(lambda _url: PNG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    channel = channel_config(default_image="http://example.invalid/d.jpg")
    with caplog.at_level(logging.WARNING):
        logos.refresh(channel)
        logos.refresh(channel)
    assert http.urls == []
    warnings = [r for r in caplog.records if r.getMessage() == "default_image_unusable"]
    assert len(warnings) == 1


def test_a_failure_removing_a_default_image_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: JPG), Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", unlink)
    logos.refresh(channel_config(default_image=None))
    failed = [r for r in caplog.records if r.getMessage() == "logo_failed"]
    assert failed and all(getattr(r, "fields", {}).get("kind") == "default" for r in failed)


def test_forget_removes_everything_downloaded_for_a_channel(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: BIG_PNG if url == URL else JPG)
    logos = Logos(tmp_path, http, Blobs(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL, default_image=DEFAULT_URL))
    logos.refresh(
        dataclasses.replace(channel_config(logo=URL, default_image=DEFAULT_URL), id="keep")
    )
    assert (tmp_path / "small" / "owr.png").exists()
    logos.forget("owr")
    logos.forget("owr")  # nothing left is fine
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == [
        "default/keep.jpg",
        "default/keep.url",
        "keep.png",
        "keep.url",
        "small/keep.png",
    ]


def test_forget_logs_what_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), Blobs(tmp_path / "blobs"))

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", unlink)
    logos.forget("owr")
    failed = [r for r in caplog.records if r.getMessage() == "logo_failed"]
    assert failed and all(getattr(r, "fields", {}).get("kind") == "forget" for r in failed)
