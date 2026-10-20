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
    assert {p.name for p in PAGES} >= {"index.html", "status.html", "check.html", "pin.html"}


def test_player_page_has_the_elements_the_scripts_bind_to() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    classes = {name for value in re.findall(r'class="([^"]*)"', html) for name in value.split()}
    for name in [
        "tunebox",
        "pin-screen",
        "car-daynight",
        "container-inner",
        "artwork",
        "car-station-logo",
        "current-channel",
        "car-title",
        "car-artist",
        "car-played",
        "history-list",
        "channel-groups",
        "viz-list",
        "volume-slider",
    ]:
        assert name in classes, name
    for needle in ['id="analyzer"', 'src="js/main.js"']:
        assert needle in html, needle


def test_manifest_icons_exist() -> None:
    import json

    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        assert (WEB / icon["src"]).is_file(), icon["src"]


def test_car_css_has_no_pawtunes_selectors_left() -> None:
    css = (WEB / "css" / "car.css").read_text(encoding="utf-8")
    assert ".pawtunes" not in css


def test_scripts_do_not_evaluate_strings() -> None:
    # The CSP has no 'unsafe-eval': eval and new Function throw, so a syntax
    # probe built on them reports "unsupported" in every browser.
    scripts = [*WEB.glob("*.js"), *(WEB / "js").glob("*.js")]
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        assert not re.search(r"\bnew Function\(|\beval\(", source), script.name


def test_the_pin_page_loads_only_its_own_script() -> None:
    html = (WEB / "pin.html").read_text(encoding="utf-8")
    assert re.findall(r'<script[^>]*src="([^"]+)"', html) == ["/js/pinpage.js"]
    assert 'class="pin-screen"' in html
