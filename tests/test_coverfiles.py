import os
from pathlib import Path

import pytest

from tunebox.coverfiles import (
    LOW_WATER,
    MAX_COVER,
    MAX_COVERS,
    CoverFiles,
    SaveResult,
    name_for,
    sized,
    sniff_cover,
)

JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x03" * 64
LINK = "https://example.invalid/images/cover/abc/1000x1000-000000-80-0-0.jpg"
OTHER = "https://example.invalid/images/cover/def/1000x1000-000000-80-0-0.jpg"


def files(tmp_path: Path, free: int | None = 10**12, now: list[float] | None = None) -> CoverFiles:
    clock = now or [1_000_000.0]
    return CoverFiles(tmp_path / "covers", free=lambda: free, clock=lambda: clock[0])


def test_sniff_takes_only_raster_images() -> None:
    assert sniff_cover(JPG) == "jpg"
    assert sniff_cover(PNG) == "png"
    assert sniff_cover(WEBP) == "webp"
    assert sniff_cover(b"<svg></svg>") is None
    assert sniff_cover(b"<!doctype html>") is None
    assert sniff_cover(b"") is None


def test_names_are_stable_hashes_of_the_link() -> None:
    assert name_for(LINK) == name_for(LINK)
    assert name_for(LINK) != name_for(OTHER)
    assert len(name_for(LINK)) == 32 and int(name_for(LINK), 16) >= 0


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (LINK, LINK.replace("/1000x1000-", "/600x600-")),
        (
            "https://example.invalid/image/thumb/Music/a/b.jpg/1000x1000bb.jpg",
            "https://example.invalid/image/thumb/Music/a/b.jpg/600x600bb.jpg",
        ),
        ("https://example.invalid/other.jpg", "https://example.invalid/other.jpg"),
    ],
)
def test_links_are_rewritten_to_600(link: str, expected: str) -> None:
    assert sized(link) == expected


def test_save_then_address_and_file(tmp_path: Path) -> None:
    store = files(tmp_path)
    assert store.path_for(LINK) is None and store.address_for(LINK) is None
    assert store.save(LINK, JPG) is SaveResult.SAVED
    path = store.path_for(LINK)
    assert path is not None and path.read_bytes() == JPG and path.suffix == ".jpg"
    address = store.address_for(LINK) or ""
    assert address.startswith(f"/covers/{name_for(LINK)}.jpg?v=")
    assert store.file(f"{name_for(LINK)}.jpg") == path
    assert not list((tmp_path / "covers").glob(".*"))  # no temporary file left


def test_unchanged_bytes_keep_the_address(tmp_path: Path) -> None:
    store = files(tmp_path)
    store.save(LINK, JPG)
    before = store.address_for(LINK)
    assert store.save(LINK, JPG) is SaveResult.UNCHANGED
    assert store.address_for(LINK) == before


def test_changed_bytes_or_kind_replace_the_file(tmp_path: Path) -> None:
    store = files(tmp_path)
    store.save(LINK, JPG)
    old = store.path_for(LINK)
    assert old is not None
    os.utime(old, ns=(1, 1))
    assert store.save(LINK, PNG) is SaveResult.SAVED
    new = store.path_for(LINK)
    assert new is not None and new.suffix == ".png" and not old.exists()


@pytest.mark.parametrize(
    ("body", "result"),
    [
        (b"<html></html>", SaveResult.NOT_IMAGE),
        (b"", SaveResult.NOT_IMAGE),
        (b"\xff\xd8\xff" + b"\x00" * MAX_COVER, SaveResult.TOO_LARGE),
    ],
)
def test_save_refuses_what_is_not_a_cover(tmp_path: Path, body: bytes, result: SaveResult) -> None:
    store = files(tmp_path)
    assert store.save(LINK, body) is result
    assert store.path_for(LINK) is None


def test_save_keeps_the_disk_floor(tmp_path: Path) -> None:
    store = files(tmp_path, free=268_435_456 + 10)
    assert store.save(LINK, JPG) is SaveResult.DISK_LOW
    assert store.path_for(LINK) is None


def test_an_unwritable_folder_is_a_result_not_an_exception(tmp_path: Path) -> None:
    (tmp_path / "covers").write_text("a file where the folder should be")
    assert files(tmp_path).save(LINK, JPG) is SaveResult.UNAVAILABLE


def test_touch_sets_atime_once_a_day_and_keeps_mtime(tmp_path: Path) -> None:
    now = [1_000_000.0]
    store = files(tmp_path, now=now)
    store.save(LINK, JPG)
    path = store.path_for(LINK)
    assert path is not None
    os.utime(path, ns=(5, 7))
    store.touch(path)
    assert path.stat().st_atime == 1_000_000.0 and path.stat().st_mtime_ns == 7
    now[0] += 3600
    store.touch(path)
    assert path.stat().st_atime == 1_000_000.0
    now[0] += 86_400
    store.touch(path)
    assert path.stat().st_atime == now[0]


@pytest.mark.parametrize(
    "name", ["..%2Ftunebox.db", "../tunebox.db", "x.svg", "x", f"{'a' * 32}.gif"]
)
def test_file_refuses_other_names(tmp_path: Path, name: str) -> None:
    store = files(tmp_path)
    store.save(LINK, JPG)
    (tmp_path / "tunebox.db").write_bytes(b"db")
    assert store.file(name) is None


def test_clean_removes_unreferenced_files_and_keeps_shared_ones(tmp_path: Path) -> None:
    store = files(tmp_path)
    store.save(LINK, JPG)
    store.save(OTHER, PNG)
    report = store.clean({name_for(LINK)})
    assert store.path_for(LINK) is not None and store.path_for(OTHER) is None
    assert report.files_removed == 1 and report.files == 1 and report.bytes == len(JPG)


def test_clean_enforces_the_cap_by_oldest_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tunebox.coverfiles.MAX_COVERS", 3 * len(JPG))
    monkeypatch.setattr("tunebox.coverfiles.LOW_WATER", 2 * len(JPG))
    store = files(tmp_path)
    links = [f"https://example.invalid/{i}/1000x1000-x.jpg" for i in range(4)]
    for i, link in enumerate(links):
        store.save(link, JPG[:-1] + bytes([i]))
        path = store.path_for(link)
        assert path is not None
        os.utime(path, ns=(i * 10**9, 1))  # link 0 played longest ago
    report = store.clean({name_for(link) for link in links})
    kept = [link for link in links if store.path_for(link) is not None]
    assert kept == links[2:]
    assert report.files_removed == 2


def test_clean_survives_a_file_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = files(tmp_path)
    store.save(LINK, JPG)
    store.save(OTHER, PNG)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".jpg":
            raise PermissionError("read-only")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    report = store.clean(set())
    assert report.files_removed == 1


def test_the_cap_constants_match_the_spec() -> None:
    assert (MAX_COVERS, LOW_WATER, MAX_COVER) == (500_000_000, 450_000_000, 2_000_000)


def test_free_reports_the_injected_free_space(tmp_path: Path) -> None:
    assert CoverFiles(tmp_path, free=lambda: 42).free() == 42
