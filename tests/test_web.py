import re
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "web"
PAGES = sorted(WEB.glob("*.html"))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_work_under_the_content_security_policy(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    # script-src 'self' and style-src 'self' refuse inline code and styles.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script"
    assert "<style" not in html, "inline style element"
    assert not re.search(r"\sstyle=", html), "style attribute"
    assert not re.search(r"\son[a-z]+=", html), "inline event handler"


def test_there_are_pages() -> None:
    assert {p.name for p in PAGES} >= {"index.html", "status.html", "check.html"}
