import logging
import os
from pathlib import Path

import pytest

from tests.fakes import FakeHttp, channel_config
from tunebox.http import HttpError
from tunebox.logos import Logos, sniff

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
URL = "https://example.invalid/owr.png"


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (PNG, "png"),
        (JPG, "jpg"),
        (WEBP, "webp"),
        (SVG, "svg"),
        (b"  <svg></svg>", "svg"),
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
