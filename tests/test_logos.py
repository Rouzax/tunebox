import logging
import os
import threading
from pathlib import Path

import pytest

from tests.fakes import FakeHttp, channel_config
from tunebox.http import HttpError
from tunebox.logos import SMALL_PX, Logos, sniff
from tunebox.png import Image, read, write

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
URL = "https://example.invalid/owr.png"
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
    logos = Logos(tmp_path, http)
    channels = [channel_config(logo=URL)]
    logos.fetch_all(channels)
    logos.fetch_all(channels)
    assert http.urls == [URL]
    assert (tmp_path / "owr.png").read_bytes() == PNG
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.file("owr.png") == tmp_path / "owr.png"


def test_the_logo_path_changes_with_the_file(tmp_path: Path) -> None:
    # Logos are cached for a year, so a replaced logo needs a new address.
    logos = Logos(tmp_path, FakeHttp(lambda _url: PNG))
    logos.fetch_all([channel_config(logo=URL)])
    before = logos.path_for("owr")
    os.utime(tmp_path / "owr.png", ns=(1, 1))
    assert logos.path_for("owr") != before


def test_a_changed_address_fetches_again_and_replaces_the_old_file(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG)).fetch_all([channel_config(logo=URL)])
    other = "https://example.invalid/owr.svg"
    Logos(tmp_path, FakeHttp(lambda _url: SVG)).fetch_all([channel_config(logo=other)])
    assert not (tmp_path / "owr.png").exists()
    later = Logos(tmp_path, FakeHttp(lambda _url: b""))
    assert (later.path_for("owr") or "").startswith("/logos/owr.svg?v=")


def test_a_page_instead_of_an_image_is_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    logos = Logos(tmp_path, FakeHttp(lambda _url: b"<html>"))
    logos.fetch_all([channel_config(logo=URL)])
    assert logos.path_for("owr") is None
    assert "logo_rejected" in caplog.text


def test_a_failed_download_keeps_the_old_logo(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: PNG)).fetch_all([channel_config(logo=URL)])

    def fail(_url: str) -> bytes:
        raise HttpError("down")

    logos = Logos(tmp_path, fail)
    logos.fetch_all([channel_config(logo="https://example.invalid/new.png")])
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")


def test_channels_without_a_logo_are_skipped(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    Logos(tmp_path, http).fetch_all([channel_config()])
    assert http.urls == []


@pytest.mark.parametrize("name", ["../owr.png", "owr.png/..", ".owr.tmp", "owr.url", "owr.exe", ""])
def test_file_refuses_other_names(tmp_path: Path, name: str) -> None:
    (tmp_path / "owr.url").write_text(URL, encoding="utf-8")
    assert Logos(tmp_path, FakeHttp(lambda _url: PNG)).file(name) is None


def test_an_unusable_folder_is_reported_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    logos = Logos(blocker / "logos", FakeHttp(lambda _url: PNG))
    logos.fetch_all([channel_config(logo=URL)])
    assert "logos_unavailable" in caplog.text


def test_a_large_png_gets_a_small_copy_for_the_station_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logos = Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG))
    logos.fetch_all([channel_config(logo=URL)])
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
    logos = Logos(tmp_path, http)
    logos.fetch_all([channel_config(logo=URL)])
    assert http.urls == []
    assert logos.path_for("owr", small=True) is not None


def test_a_logo_that_is_small_already_has_no_copy(tmp_path: Path) -> None:
    tiny = write(Image(64, 64, bytes((0, 0, 0, 255)) * 64 * 64))
    logos = Logos(tmp_path, FakeHttp(lambda _url: tiny))
    logos.fetch_all([channel_config(logo=URL)])
    assert logos.path_for("owr", small=True) is None


def test_a_replaced_logo_drops_or_renews_its_small_copy(tmp_path: Path) -> None:
    Logos(tmp_path, FakeHttp(lambda _url: BIG_PNG)).fetch_all([channel_config(logo=URL)])
    other = "https://example.invalid/owr.svg"
    logos = Logos(tmp_path, FakeHttp(lambda _url: SVG))
    logos.fetch_all([channel_config(logo=other)])
    assert not (tmp_path / "small" / "owr.png").exists()
    assert logos.path_for("owr", small=True) is None


def test_an_unreadable_png_is_kept_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    broken = PNG[:8] + b"\x00\x00\x00\x0dIHDR" + b"\x00\x00\x02\x00" * 2 + b"\x08\x02\x00\x00\x00"
    logos = Logos(tmp_path, FakeHttp(lambda _url: broken))
    logos.fetch_all([channel_config(logo=URL)])
    assert (logos.path_for("owr") or "").startswith("/logos/owr.png?v=")
    assert logos.path_for("owr", small=True) is None
    assert "logo_not_shrunk" in caplog.text


def test_fetching_stops_when_tunebox_stops(tmp_path: Path) -> None:
    http = FakeHttp(lambda _url: PNG)
    stop = threading.Event()
    stop.set()
    Logos(tmp_path, http).fetch_all([channel_config(logo=URL)], stop)
    assert http.urls == []
