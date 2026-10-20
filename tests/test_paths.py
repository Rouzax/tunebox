from pathlib import Path

from tunebox.paths import inside


def test_inside_resolves_files_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b.js").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    root = root.resolve()
    assert inside(root, "a/b.js") == root / "a" / "b.js"
    assert inside(root, "a/../a/b.js") == root / "a" / "b.js"
    assert inside(root, "../outside.txt") is None
    assert inside(root, "a") is None  # a folder is not a file
    assert inside(root, "missing.js") is None
    assert inside(root, "a/b.js\x00") is None
