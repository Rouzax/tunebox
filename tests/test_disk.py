from pathlib import Path

import pytest

from tunebox import disk


def test_room_needs_the_floor_left_after_the_write() -> None:
    assert disk.has_room(disk.MIN_FREE + 100, 100)
    assert not disk.has_room(disk.MIN_FREE + 99, 100)


def test_unknown_free_space_is_not_a_refusal() -> None:
    assert disk.has_room(None, 10**12)


def test_free_bytes_reads_the_file_system(tmp_path: Path) -> None:
    free = disk.free_bytes(tmp_path)
    assert free is not None and free > 0


def test_free_bytes_of_a_missing_folder_is_unknown(tmp_path: Path) -> None:
    assert disk.free_bytes(tmp_path / "absent") is None


def test_free_bytes_that_fail_are_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_path: object) -> object:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(disk.shutil, "disk_usage", broken)
    assert disk.free_bytes(tmp_path) is None
