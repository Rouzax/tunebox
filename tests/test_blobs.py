import errno
import hashlib
import os
from pathlib import Path

import pytest

from tunebox import blobs as blobs_module
from tunebox.blobs import BLOB_GRACE_S, MAX_BLOB, BlobError, Blobs, BlobStoreFull, BlobUnavailable
from tunebox.config import ChannelRow, ConfigState
from tunebox.disk import MIN_FREE

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
    assert blobs.collect(keep={kept}, older_than_s=86_400) == 1
    assert blobs.path(kept) and blobs.path(young) and blobs.path(old) is None


def test_collect_without_a_folder_removes_nothing(tmp_path: Path) -> None:
    assert Blobs(tmp_path / "nowhere").collect(set(), 0) == 0


def test_save_accepts_exactly_the_limit(tmp_path: Path) -> None:
    body = PNG + b"\x00" * (MAX_BLOB - len(PNG))
    assert len(body) == MAX_BLOB
    ref, _ = Blobs(tmp_path).save(body)
    assert Blobs(tmp_path).path(ref) is not None


def test_collect_removes_old_temp_files_only(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    blobs = Blobs(tmp_path, clock=lambda: clock[0])
    old, young = tmp_path / ".a.tmp", tmp_path / ".b.tmp"
    old.write_bytes(b"x")
    young.write_bytes(b"x")
    os.utime(old, (clock[0] - 2 * 86_400, clock[0] - 2 * 86_400))
    os.utime(young, (clock[0], clock[0]))
    assert blobs.collect(set(), 86_400) == 1
    assert not old.exists() and young.exists()


def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_self: Path, _data: bytes) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", full)
    with pytest.raises(BlobUnavailable):
        Blobs(tmp_path).save(PNG)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_rename_removes_the_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_self: Path, _target: Path) -> Path:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(BlobUnavailable):
        Blobs(tmp_path).save(PNG)
    assert list(tmp_path.iterdir()) == []


def test_saving_a_stored_image_again_restarts_its_grace(tmp_path: Path) -> None:
    clock = [10 * 86_400.0]
    blobs = Blobs(tmp_path, clock=lambda: clock[0])
    ref, _ = blobs.save(PNG)
    stored = blobs.path(ref)
    assert stored is not None
    os.utime(stored, (0, 0))
    assert blobs.save(PNG)[0] == ref
    assert blobs.collect(keep=set(), older_than_s=86_400) == 0
    assert stored.is_file()


def test_an_upload_to_a_read_only_folder_is_a_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_only(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(Path, "write_bytes", read_only)
    monkeypatch.setattr(Path, "unlink", read_only)
    with pytest.raises(BlobUnavailable) as raised:
        Blobs(tmp_path).save(PNG)
    assert raised.value.errno == errno.EROFS


@pytest.mark.parametrize("left", [b"", PNG[: len(PNG) // 2]])
def test_saving_an_image_again_repairs_a_short_file(tmp_path: Path, left: bytes) -> None:
    blobs = Blobs(tmp_path)
    digest = hashlib.sha256(PNG).hexdigest()
    (tmp_path / f"{digest}.png").write_bytes(left)
    ref, _ = blobs.save(PNG)
    stored = blobs.path(ref)
    assert stored is not None and stored.read_bytes() == PNG
    assert [p.name for p in tmp_path.iterdir()] == [stored.name]


def png(size: int) -> bytes:
    """A PNG-signed body of the given size (the store sniffs only the start)."""
    return PNG + b"\x00" * (size - len(PNG))


def test_an_upload_past_the_store_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blobs_module, "MAX_STORE", 3_000)
    store = Blobs(tmp_path, free_space=lambda: None)
    store.save(png(2_000))
    with pytest.raises(BlobStoreFull) as refused:
        store.save(png(1_500))
    assert refused.value.reason == "logo_store_full"


def test_a_stored_image_saves_again_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blobs_module, "MAX_STORE", 2_100)
    store = Blobs(tmp_path, free_space=lambda: None)
    body = png(2_000)
    first = store.save(body)
    assert store.save(body) == first


def test_an_upload_under_the_free_space_floor_is_refused(tmp_path: Path) -> None:
    store = Blobs(tmp_path, free_space=lambda: MIN_FREE + 100)
    with pytest.raises(BlobStoreFull) as refused:
        store.save(png(500))
    assert refused.value.reason == "disk_low"
    assert list(tmp_path.iterdir()) == []


def test_the_default_free_space_reads_the_nearest_existing_folder(tmp_path: Path) -> None:
    store = Blobs(tmp_path / "not" / "yet")
    assert store.save(PNG)[1] == "png"


def test_tidy_waits_while_the_database_is_young(tmp_path: Path) -> None:
    now = 10_000_000.0
    store = Blobs(tmp_path, clock=lambda: now, free_space=lambda: None)
    asked: list[int] = []

    def keep() -> set[str]:
        asked.append(1)
        return set()

    assert store.tidy(None, keep) is None
    assert store.tidy(now - BLOB_GRACE_S, keep) is None
    assert asked == []
    assert store.tidy(now - BLOB_GRACE_S - 1, keep) == 0
    assert asked == [1]
