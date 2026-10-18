"""Fetches the Material Symbols Rounded icons listed in scripts/icons.txt
from Google's repository at a pinned commit, stores each SVG and the
licence, builds icons.svg (one <symbol> per icon) and records the hashes
in web/vendor/SHA256SUMS. Run by hand when the list changes; not part of
the image."""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

COMMIT = "737e3324305806514d7909874fa1818ae1808232"
BASE = f"https://raw.githubusercontent.com/google/material-design-icons/{COMMIT}"
VENDOR = Path(__file__).resolve().parent.parent / "web" / "vendor"
FOLDER = VENDOR / f"material-symbols-rounded-{COMMIT[:7]}"
SVG = "{http://www.w3.org/2000/svg}"
_NAME = re.compile(r"^[a-z0-9_]+$")


def fetch(path: str) -> bytes:
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=20) as response:
        return response.read()


def wanted() -> list[tuple[str, bool]]:
    icons: list[tuple[str, bool]] = []
    for line in (Path(__file__).with_name("icons.txt")).read_text(encoding="utf-8").splitlines():
        words = line.split("#")[0].split()
        if not words:
            continue
        if not _NAME.match(words[0]) or words[1:] not in ([], ["fill"]):
            sys.exit(f"icons.txt: cannot read {line!r}")
        icons.append((words[0], words[1:] == ["fill"]))
    return icons


def main() -> int:
    FOLDER.mkdir(parents=True, exist_ok=True)
    for old in FOLDER.glob("*.svg"):
        old.unlink()
    (FOLDER / "LICENSE").write_bytes(fetch("LICENSE"))
    symbols: list[str] = []
    for name, fill in wanted():
        forms = [("", name)] + ([("_fill1", f"{name}-fill")] if fill else [])
        for suffix, symbol_id in forms:
            body = fetch(f"symbols/web/{name}/materialsymbolsrounded/{name}{suffix}_24px.svg")
            (FOLDER / f"{name}{suffix}_24px.svg").write_bytes(body)
            # A pinned file from Google's repository, read by a dev-only
            # script; ElementTree resolves no external entities.
            root = ET.fromstring(body)
            paths = "".join(f'<path d="{p.attrib["d"]}"/>' for p in root.iter(f"{SVG}path"))
            view = root.attrib["viewBox"]
            symbols.append(f'<symbol id="{symbol_id}" viewBox="{view}">{paths}</symbol>')
    sprite = '<svg xmlns="http://www.w3.org/2000/svg">' + "".join(symbols) + "</svg>\n"
    (FOLDER / "icons.svg").write_text(sprite, encoding="utf-8")
    sums = VENDOR / "SHA256SUMS"
    lines = sums.read_text(encoding="utf-8").splitlines()
    keep = [line for line in lines if f" {FOLDER.name}/" not in line]
    for file in sorted(FOLDER.iterdir()):
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        keep.append(f"{digest}  {FOLDER.name}/{file.name}")
    sums.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
