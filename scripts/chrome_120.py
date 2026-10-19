"""Fetches Chrome for Testing 120, the engine of the car's browser, for the
browser checks: newer test browsers accept CSS and web APIs the car lacks.
The zip is pinned by its SHA-256 and unpacked under ~/.cache/tunebox. Prints
the path to pass as CHROME; run by hand, not part of the image.

    CHROME=$(.venv/bin/python scripts/chrome_120.py) CHROME_NO_SANDBOX=1 \\
        node web/tests/browser/layout.mjs http://127.0.0.1:8080
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "120.0.6099.109"
URL = (
    f"https://storage.googleapis.com/chrome-for-testing-public/{VERSION}/linux64/chrome-linux64.zip"
)
# Google publishes no hashes for these downloads; this one was taken from
# the file as served over https and pins it from then on.
SHA256 = "bcb22c5242aabf184c6fadd86ee58b3ae35739177edac9de3938ed33791d4ddf"
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
CACHE = CACHE_HOME / "tunebox" / f"chrome-{VERSION}"
BINARY = CACHE / "chrome-linux64" / "chrome"


def fetch(target: Path) -> None:
    digest = hashlib.sha256()
    with urllib.request.urlopen(URL, timeout=60) as response, target.open("wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != SHA256:
        sys.exit(f"chrome_120: {URL} does not match the pinned SHA-256; not unpacked")


def unpack(archive: Path) -> None:
    staging = Path(tempfile.mkdtemp(dir=CACHE.parent))
    try:
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                path = (staging / info.filename).resolve()
                if not path.is_relative_to(staging.resolve()):
                    sys.exit(f"chrome_120: {info.filename!r} points outside the folder")
                zipped.extract(info, staging)
                # zipfile drops the execute bits; the archive keeps them in
                # the high bits of external_attr.
                mode = info.external_attr >> 16
                if mode & 0o111:
                    path.chmod(mode & 0o777)
        shutil.rmtree(CACHE, ignore_errors=True)
        staging.rename(CACHE)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    if not BINARY.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=CACHE.parent) as folder:
            archive = Path(folder) / "chrome.zip"
            fetch(archive)
            unpack(archive)
    print(BINARY)


if __name__ == "__main__":
    main()
