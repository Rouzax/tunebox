import dataclasses
import logging
import os
import re
import threading
import time
from pathlib import Path

import pytest

from tests.fakes import FakeHttp, blob_store, channel_config
from tunebox.http import HttpError
from tunebox.logos import (
    GROUP_SMALL_PX,
    SMALL_PX,
    Logos,
    default_image_address,
    group_key,
    sniff,
)
from tunebox.png import Image, read, write

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
URL = "https://example.invalid/owr.png"
DEFAULT_URL = "https://example.invalid/default.jpg"
BIG_PNG = write(Image(300, 150, bytes((200, 30, 30, 255)) * 300 * 150))
GROUP_URL = "https://example.invalid/group.png"


def big_png(px: int) -> bytes:
    """A square PNG of px x px."""
    return write(Image(px, px, bytes((30, 30, 200, 255)) * px * px))


def png_size(body: bytes) -> tuple[int, int]:
    image = read(body)
    return image.width, image.height


def drain(logos: Logos) -> None:
    while logos.run_once():
        pass


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
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    channels = [channel_config(logo=URL)]
    logos.refresh(channels[0])
    logos.refresh(channels[0])
    assert http.urls == [URL]
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.file("owr.png") == tmp_path / "owr.png"


def test_the_logo_path_changes_with_the_file(tmp_path: Path) -> None:
    # Logos are cached for a year, so a replaced logo needs a new address.
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    before = logos.path_for("owr")
    os.utime(tmp_path / "owr.png", ns=(1, 1))
    assert logos.path_for("owr") != before


def test_a_changed_address_fetches_again_and_replaces_the_old_file(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )
    other = "https://example.invalid/owr.svg"
    Logos(tmp_path, FakeHttp(lambda _url: SVG), blob_store(tmp_path / "blobs")).refresh(
        channel_config(logo=other)
    )
    assert not (tmp_path / "owr.png").exists()
    later = Logos(tmp_path, FakeHttp(lambda _url: b""), blob_store(tmp_path / "blobs"))
    assert (later.path_for("owr") or "").startswith("/logos/owr.svg?v=")


def test_a_page_instead_of_an_image_is_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    logos = Logos(tmp_path, FakeHttp(lambda _url: b"<html>"), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert logos.path_for("owr") is None
    assert "logo_rejected" in caplog.text


def test_a_failed_download_keeps_the_old_logo(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )

    def fail(_url: str) -> bytes:
        raise HttpError("down")

    logos = Logos(tmp_path, fail, blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo="https://example.invalid/new.png"))
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")


def test_channels_without_a_logo_are_skipped(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    Logos(tmp_path, http, blob_store(tmp_path / "blobs")).refresh(channel_config())
    assert http.urls == []


@pytest.mark.parametrize("name", ["../owr.png", "owr.png/..", ".owr.tmp", "owr.url", "owr.exe", ""])
def test_file_refuses_other_names(tmp_path: Path, name: str) -> None:
    (tmp_path / "owr.url").write_text(URL, encoding="utf-8")
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    assert logos.file(name) is None


def test_an_unusable_folder_is_reported_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    logos = Logos(blocker / "logos", FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    logos.refresh(channel_config(logo=URL))
    assert [r.getMessage() for r in caplog.records].count("logos_unavailable") == 1


def test_a_large_png_gets_a_small_copy_for_the_station_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), blob_store(tmp_path / "blobs"))
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
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert http.urls == []
    assert logos.path_for("owr", small=True) is not None


def test_a_logo_that_is_small_already_has_no_copy(tmp_path: Path) -> None:
    tiny = write(Image(64, 64, bytes((0, 0, 0, 255)) * 64 * 64))
    logos = Logos(tmp_path, FakeHttp(lambda _url: tiny), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert logos.path_for("owr", small=True) is None


def test_a_replaced_logo_drops_or_renews_its_small_copy(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), blob_store(tmp_path / "blobs")).refresh(
        channel_config(logo=URL)
    )
    other = "https://example.invalid/owr.svg"
    logos = Logos(tmp_path, FakeHttp(lambda _url: SVG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=other))
    assert not (tmp_path / "small" / "owr.png").exists()
    assert logos.path_for("owr", small=True) is None


def test_an_unreadable_png_is_kept_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    broken = PNG[:8] + b"\x00\x00\x00\x0dIHDR" + b"\x00\x00\x02\x00" * 2 + b"\x08\x02\x00\x00\x00"
    logos = Logos(tmp_path, FakeHttp(lambda _url: broken), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.path_for("owr", small=True) is None
    assert "logo_not_shrunk" in caplog.text


def test_requested_logos_are_fetched_by_the_worker(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
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
    blobs = blob_store(tmp_path / "blobs")
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
    blobs = blob_store(tmp_path / "blobs")
    ref, _ = blobs.save(BIG_PNG)
    logos = Logos(tmp_path, FakeHttp(lambda _url: b""), blobs)
    logos.refresh(channel_config(logo=ref))
    before = (tmp_path / "owr.png").stat().st_mtime_ns
    logos.refresh(channel_config(logo=ref))
    assert (tmp_path / "owr.png").stat().st_mtime_ns == before


def test_a_blob_logo_replaces_a_downloaded_one_of_another_kind(tmp_path: Path) -> None:
    blobs = blob_store(tmp_path / "blobs")
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
        tmp_path, FakeHttp(lambda url: fetched.append(url) or b""), blob_store(tmp_path / "blobs")
    )
    logos.refresh(channel_config(logo="blob:" + "2" * 64))
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert "logo_missing" in [r.getMessage() for r in caplog.records]
    assert fetched == []


def test_a_blob_logo_gives_way_to_an_address_again(tmp_path: Path) -> None:
    blobs = blob_store(tmp_path / "blobs")
    ref, _ = blobs.save(SVG)
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blobs)
    logos.refresh(channel_config(logo=ref))
    logos.refresh(channel_config(logo=URL))
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert not (tmp_path / "owr.svg").exists()
    assert (tmp_path / "owr.url").read_text(encoding="utf-8") == URL


def test_a_refresh_leaves_the_blob_folder_alone(tmp_path: Path) -> None:
    blobs = blob_store(tmp_path / "blobs")
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
    blobs = blob_store(tmp_path / "blobs")
    ref, _ = blobs.save(PNG)
    stored = blobs.path(ref)
    assert stored is not None
    stored.write_bytes(b"")
    Logos(tmp_path, FakeHttp(lambda _url: b""), blobs).refresh(channel_config(logo=ref))
    assert not (tmp_path / "owr.png").exists()
    assert "logo_rejected" in caplog.text


def test_a_default_image_is_downloaded_beside_the_logo(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: JPG if url == DEFAULT_URL else PNG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
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
    logos = Logos(tmp_path, FakeHttp(lambda url: bodies[url]), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))
    logos.refresh(channel_config(default_image="https://example.invalid/other.png"))
    assert sorted(p.name for p in (tmp_path / "default").iterdir()) == ["owr.png", "owr.url"]


@pytest.mark.parametrize("value", ["/img/station.png", None, "data:image/png;base64,AAAA"])
def test_a_default_image_that_is_not_downloaded_removes_the_old_file(
    tmp_path: Path, value: str | None
) -> None:
    http = FakeHttp(lambda _url: JPG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
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
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
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
    logos = Logos(tmp_path, FakeHttp(lambda _url: JPG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", unlink)
    logos.refresh(channel_config(default_image=None))
    failed = [r for r in caplog.records if r.getMessage() == "logo_failed"]
    assert failed and all(getattr(r, "fields", {}).get("kind") == "default" for r in failed)


def test_forget_removes_everything_downloaded_for_a_channel(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: BIG_PNG if url == URL else JPG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL, default_image=DEFAULT_URL))
    logos.refresh(
        dataclasses.replace(channel_config(logo=URL, default_image=DEFAULT_URL), id="keep")
    )
    assert (tmp_path / "small" / "owr.png").exists()
    logos.forget("owr")
    logos.forget("owr")  # nothing left is fine
    while logos.run_once():
        pass
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
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", unlink)
    logos.forget("owr")
    assert logos.run_once()
    failed = [r for r in caplog.records if r.getMessage() == "logo_failed"]
    assert failed and all(getattr(r, "fields", {}).get("kind") == "forget" for r in failed)


def test_a_forget_queued_after_a_refresh_leaves_no_files(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: BIG_PNG if url == URL else JPG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    channel = channel_config(logo=URL, default_image=DEFAULT_URL)
    logos.request(channel)
    logos.forget(channel.id)
    assert logos.run_once() and logos.run_once()
    assert not logos.run_once()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    logos.request(channel)  # re-enabled: downloaded again
    assert logos.run_once()
    assert logos.path_for(channel.id) is not None


def test_forget_removes_a_stale_partial_file(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    for folder in (tmp_path, tmp_path / "small", tmp_path / "default"):
        folder.mkdir(exist_ok=True)
        (folder / ".owr.tmp").write_bytes(b"x")
    logos.forget("owr")
    assert logos.run_once()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_the_image_revision_changes_when_a_file_is_saved_or_removed(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: BIG_PNG if url == URL else JPG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    seen = [logos.revision()]
    logos.refresh(channel_config(logo=URL))
    seen.append(logos.revision())
    logos.refresh(channel_config(logo=URL, default_image=DEFAULT_URL))
    seen.append(logos.revision())
    logos.refresh(channel_config(logo=URL, default_image=None))
    seen.append(logos.revision())
    logos.forget("owr")
    logos.run_once()
    seen.append(logos.revision())
    assert len(set(seen)) == len(seen)


def test_an_unchanged_refresh_keeps_the_image_revision(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    before = logos.revision()
    logos.refresh(channel_config(logo=URL))
    assert logos.revision() == before


def test_a_store_that_fails_before_touching_a_file_keeps_the_image_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), blob_store(tmp_path / "blobs"))
    before = logos.revision()

    def refuse(_self: Path, _data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    logos.refresh(channel_config(logo=URL))
    assert logos.revision() == before


def test_the_image_revision_differs_between_instances(tmp_path: Path) -> None:
    blobs = blob_store(tmp_path / "blobs")
    first = Logos(tmp_path, FakeHttp(lambda _url: PNG), blobs)
    second = Logos(tmp_path, FakeHttp(lambda _url: PNG), blobs)
    assert first.revision() != second.revision()


def test_forget_all_except_removes_only_gone_channels_files(tmp_path: Path) -> None:
    http = FakeHttp(lambda url: BIG_PNG if url == URL else JPG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "blobs"))
    for channel_id in ("gone", "live"):
        config = dataclasses.replace(
            channel_config(logo=URL, default_image=DEFAULT_URL), id=channel_id
        )
        logos.refresh(config)
    (tmp_path / "README.txt").write_text("not a channel file", encoding="utf-8")
    (tmp_path / "Odd Name.png").write_bytes(PNG)
    logos.forget_all_except({"live"})
    while logos.run_once():
        pass
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == [
        "Odd Name.png",
        "README.txt",
        "default/live.jpg",
        "default/live.url",
        "live.png",
        "live.url",
        "small/live.png",
    ]


def test_forget_all_except_copes_with_a_missing_folder(tmp_path: Path) -> None:
    logos = Logos(tmp_path / "none", FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    logos.forget_all_except(set())
    assert not logos.run_once()


def test_a_cleared_logo_leaves_no_files(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))
    (tmp_path / ".owr.tmp").write_bytes(b"partial")
    assert logos.path_for("owr") is not None
    assert (tmp_path / "small" / "owr.png").exists()
    before = logos.revision()
    logos.refresh(channel_config(logo=None))
    assert logos.path_for("owr") is None
    assert logos.path_for("owr", small=True) is None
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert [name for name in left if "owr" in name] == []
    assert logos.revision() != before


def test_a_cleared_logo_that_cannot_be_removed_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(logo=URL))

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", unlink)
    logos.refresh(channel_config(logo=None))
    failed = [r for r in caplog.records if r.getMessage() == "logo_failed"]
    assert failed
    assert "cleared" in {getattr(r, "fields", {}).get("kind") for r in failed}
    assert all(getattr(r, "fields", {}).get("channel") == "owr" for r in failed)


def test_group_key_is_stable_and_safe() -> None:
    assert group_key("Tomorrowland") == group_key("Tomorrowland")
    assert re.fullmatch(r"g[0-9a-f]{16}", group_key("../x"))
    assert group_key("A") != group_key("B")


def test_a_group_image_is_downloaded_with_a_tile_copy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logos = Logos(tmp_path, FakeHttp(lambda _url: big_png(1000)), blob_store(tmp_path / "b"))
    key = group_key("G")
    before = logos.revision()
    logos.request_group(key, GROUP_URL)
    drain(logos)
    assert (logos.group_path_for(key) or "").startswith(f"/logos/groups/{key}.png?v=")
    small = logos.group_path_for(key, small=True) or ""
    assert small.startswith(f"/logos/groups/small/{key}.png?v=")
    tile = logos.group_file(f"{key}.png", small=True)
    assert tile is not None
    assert png_size(tile.read_bytes()) == (GROUP_SMALL_PX, GROUP_SMALL_PX)
    assert logos.group_file(f"{key}.png") == tmp_path / "groups" / f"{key}.png"
    assert logos.revision() != before
    # Group events name the key, never the group or a channel.
    fields = [getattr(r, "fields", {}) for r in caplog.records if r.name == "tunebox.logos"]
    assert fields and all(f.get("group") == key and "channel" not in f for f in fields)


def test_a_group_file_name_must_be_a_group_key(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG), blob_store(tmp_path / "b"))
    key = group_key("G")
    logos.request_group(key, GROUP_URL)
    drain(logos)
    assert logos.group_file(f"{key}.png") is not None
    assert logos.group_file(f"{key}.url") is None
    assert logos.group_file(f"../{key}.png") is None
    assert logos.group_file("owr.png") is None
    assert logos.group_file(f"{key}.jpg") is None
    assert logos.group_file(f"{key}.jpg", small=True) is None
    # PNG is small already, so no tile copy is made.
    assert logos.group_path_for(key, small=True) is None


def test_a_renamed_group_copies_instead_of_downloading(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: big_png(1000))
    logos = Logos(tmp_path, http, blob_store(tmp_path / "b"))
    old, new = group_key("Old"), group_key("New")
    logos.request_group(old, GROUP_URL)
    logos.request_group(new, GROUP_URL, copy_from=old)
    logos.forget_group(old)
    drain(logos)
    assert http.urls == [GROUP_URL]
    assert logos.group_path_for(new) is not None and logos.group_path_for(old) is None
    assert logos.group_path_for(new, small=True) is not None
    assert (tmp_path / "groups" / f"{new}.url").read_text(encoding="utf-8") == GROUP_URL
    left = sorted(p.name for p in (tmp_path / "groups").rglob("*") if p.is_file())
    assert left == [f"{new}.png", f"{new}.png", f"{new}.url"]


def test_a_rename_rebuilds_a_tile_older_than_its_image(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: big_png(1000)), blob_store(tmp_path / "b"))
    old, new = group_key("Old"), group_key("New")
    logos.request_group(old, GROUP_URL)
    drain(logos)
    # A tile left behind by a failed write: older than the image it stands for.
    image = tmp_path / "groups" / f"{old}.png"
    stale = tmp_path / "groups" / "small" / f"{old}.png"
    stale.write_bytes(b"stale tile")
    when = image.stat().st_mtime_ns - 10**9
    os.utime(stale, ns=(when, when))
    logos.request_group(new, GROUP_URL, copy_from=old)
    drain(logos)
    tile = logos.group_file(f"{new}.png", small=True)
    assert tile is not None
    assert png_size(tile.read_bytes()) == (GROUP_SMALL_PX, GROUP_SMALL_PX)


def test_a_rename_downloads_when_there_is_nothing_to_copy(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    logos = Logos(tmp_path, http, blob_store(tmp_path / "b"))
    old, new = group_key("Old"), group_key("New")
    logos.request_group(new, GROUP_URL, copy_from=old)
    other = "https://example.invalid/other.png"
    logos.request_group(old, other)
    logos.request_group(group_key("Third"), GROUP_URL, copy_from=old)
    drain(logos)
    # Nothing to copy at first, then a source from another address.
    assert http.urls == [GROUP_URL, other, GROUP_URL]


def test_a_group_image_from_an_upload_is_copied(tmp_path: Path) -> None:
    blobs = blob_store(tmp_path / "b")
    ref, _ = blobs.save(PNG)
    http = FakeHttp(lambda _url: b"")
    logos = Logos(tmp_path / "logos", http, blobs)
    key = group_key("G")
    logos.request_group(key, ref)
    drain(logos)
    assert http.urls == []
    assert (logos.group_path_for(key) or "").startswith(f"/logos/groups/{key}.png?v=")


def test_a_forgotten_group_leaves_no_files(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: big_png(1000)), blob_store(tmp_path / "b"))
    key = group_key("G")
    logos.request_group(key, GROUP_URL)
    drain(logos)
    (tmp_path / "groups" / f".{key}.tmp").write_bytes(b"partial")
    before = logos.revision()
    logos.forget_group(key)
    drain(logos)
    assert [p for p in (tmp_path / "groups").rglob("*") if p.is_file()] == []
    assert logos.revision() != before


def test_stale_group_files_are_swept(tmp_path: Path) -> None:
    groups = tmp_path / "groups"
    (groups / "small").mkdir(parents=True)
    gone, kept, tile_only = group_key("Gone"), group_key("Kept"), group_key("Tile")
    (groups / f"{gone}.png").write_bytes(b"x")
    (groups / f"{gone}.url").write_text(GROUP_URL, encoding="utf-8")
    (groups / f"{kept}.png").write_bytes(b"x")
    (groups / "small" / f".{tile_only}.tmp").write_bytes(b"x")
    (groups / "notes.txt").write_bytes(b"x")
    logos = Logos(tmp_path, FakeHttp(lambda _url: b""), blob_store(tmp_path / "b"))
    logos.forget_groups_except({kept})
    drain(logos)
    left = sorted(str(p.relative_to(groups)) for p in groups.rglob("*") if p.is_file())
    assert left == [f"{kept}.png", "notes.txt"]


def test_sweeping_groups_without_a_folder_is_fine(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: b""), blob_store(tmp_path / "b"))
    logos.forget_groups_except(set())
    assert not logos.run_once()


def test_a_group_image_without_a_logo_folder_says_so_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "file").write_bytes(b"x")
    logos = Logos(
        tmp_path / "file" / "logos", FakeHttp(lambda _url: PNG), blob_store(tmp_path / "b")
    )
    logos.request_group(group_key("A"), GROUP_URL)
    logos.request_group(group_key("B"), GROUP_URL)
    drain(logos)
    assert [r.getMessage() for r in caplog.records].count("logos_unavailable") == 1


def test_a_station_default_on_tunebox_leaves_no_partial_file(tmp_path: Path) -> None:
    logos = Logos(tmp_path, FakeHttp(lambda _url: JPG), blob_store(tmp_path / "blobs"))
    logos.refresh(channel_config(default_image=DEFAULT_URL))
    (tmp_path / "default" / ".owr.tmp").write_bytes(b"partial")
    logos.refresh(channel_config(default_image="/img/default.jpg"))
    assert [p.name for p in (tmp_path / "default").iterdir() if p.is_file()] == []
