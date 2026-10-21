import os
from pathlib import Path

import pytest

from tunebox.blobs import BlobError, Blobs
from tunebox.config import ChannelRow, ConfigState

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_save_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path)
    ref, kind = blobs.save(PNG)
    assert kind == "png" and ref.startswith("blob:")
    assert blobs.save(PNG) == (ref, kind)
    path = blobs.path(ref)
    assert path is not None and path.read_bytes() == PNG
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temp files left


@pytest.mark.parametrize("body", [b"", b"<html><script>x</script>", b"x" * 10])
def test_save_refuses_what_is_not_an_image(tmp_path: Path, body: bytes) -> None:
    with pytest.raises(BlobError):
        Blobs(tmp_path).save(body)


def test_save_refuses_a_body_over_the_limit(tmp_path: Path) -> None:
    with pytest.raises(BlobError):
        Blobs(tmp_path).save(PNG + b"\x00" * 2_000_000)


def test_path_refuses_what_is_not_a_reference(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path)
    for bad in ("blob:../x", "blob:" + "A" * 64, "https://x", "blob:" + "0" * 63):
        assert blobs.path(bad) is None


def test_path_of_an_absent_blob_is_none(tmp_path: Path) -> None:
    assert Blobs(tmp_path / "nowhere").path("blob:" + "1" * 64) is None


def test_missing_names_the_field(tmp_path: Path) -> None:
    blobs = Blobs(tmp_path)
    ref, _ = blobs.save(PNG)
    gone = "blob:" + "1" * 64
    rows = (
        ChannelRow("a", None, True, {"logo": ref}),
        ChannelRow("b", None, True, {"logo": gone}),
    )
    state = ConfigState({}, {}, (), rows)
    assert blobs.missing(state) == f"channels[1].logo: no uploaded logo {gone}"
    assert blobs.missing(ConfigState({}, {}, (), rows[:1])) is None


def test_collect_keeps_referenced_and_young_blobs(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    blobs = Blobs(tmp_path, clock=lambda: clock[0])
    kept, _ = blobs.save(PNG)
    young, _ = blobs.save(PNG + b"y")
    old, _ = blobs.save(PNG + b"o")
    for ref in (kept, old):
        path = blobs.path(ref)
        assert path is not None
        os.utime(path, (clock[0] - 2 * 86_400, clock[0] - 2 * 86_400))
    (tmp_path / ".partial.tmp").write_bytes(b"x")
    os.utime(tmp_path / ".partial.tmp", (0, 0))
    assert blobs.collect(keep={kept}, older_than_s=86_400) == 1
    assert blobs.path(kept) and blobs.path(young) and blobs.path(old) is None
    assert (tmp_path / ".partial.tmp").exists()


def test_collect_without_a_folder_removes_nothing(tmp_path: Path) -> None:
    assert Blobs(tmp_path / "nowhere").collect(set(), 0) == 0
