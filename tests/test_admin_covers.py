from collections.abc import Generator
from pathlib import Path

import pytest

from tests.fakes import COVER_JPG, make_track
from tests.test_admin import ORIGIN, World, json_of
from tunebox.admin import Request, Response
from tunebox.coversearch import PER_MINUTE

LINK = "https://cdn-images.dzcdn.net/images/cover/" + "a" * 32 + "/1000x1000-000000-80-0-0.jpg"
DEEZER = {"data": [{"artist": {"name": "A"}, "album": {"title": "X", "cover_xl": LINK}}]}
KEY = make_track("A", "B").key


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    made = World(tmp_path, covers=True)
    made.catalogue["/search?"] = DEEZER
    assert made.cover_store is not None
    made.cover_store.record_heard("owr", [make_track("A", "B", int(made.clock.now * 1000) - 5)])
    yield made
    made.close()


def drain(world: World) -> None:
    assert world.cover_service is not None
    while world.cover_service.run_once():
        pass


def search(
    world: World, csrf: str, kind: str = "cover", key: str = KEY, text: str = "a b"
) -> Response:
    body = {"kind": kind, "key": key, "artist": "A", "title": "B", "text": text}
    return world.call("POST", "/admin/api/covers/search", body, csrf=csrf)


def raw_upload(
    world: World,
    csrf: str | None,
    query: str,
    body: bytes,
    content_type: str = "image/jpeg",
    origin: str = ORIGIN,
) -> Response:
    headers = [("Origin", origin), ("Content-Type", content_type)]
    if csrf:
        headers.append(("X-Tunebox-CSRF", csrf))
    headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in world.cookies.items())))
    request = Request(
        "POST", "/admin/api/covers/upload", tuple(headers), body, "198.51.100.7", query
    )
    return world.admin.handle(request)


@pytest.mark.parametrize("path", ["covers", "covers/image/" + "a" * 32 + ".jpg"])
def test_reads_need_a_session(world: World, path: str) -> None:
    assert world.call("GET", f"/admin/api/{path}").status == 401


@pytest.mark.parametrize("path", ["covers/search", "covers/fix", "covers/fix/clear"])
def test_posts_need_the_token_and_the_origin(world: World, path: str) -> None:
    csrf = world.login()
    missing = world.call("POST", f"/admin/api/{path}", {})
    assert missing.status == 403 and json_of(missing)["error"] == "bad_csrf"
    foreign = world.call("POST", f"/admin/api/{path}", {}, csrf=csrf, origin="https://evil.invalid")
    assert foreign.status == 403 and json_of(foreign)["error"] == "bad_origin"


def test_an_upload_needs_the_token_and_the_origin(world: World) -> None:
    csrf = world.login()
    missing = raw_upload(world, None, f"key={KEY}", COVER_JPG)
    assert missing.status == 403 and json_of(missing)["error"] == "bad_csrf"
    foreign = raw_upload(world, csrf, f"key={KEY}", COVER_JPG, origin="https://evil.invalid")
    assert foreign.status == 403 and json_of(foreign)["error"] == "bad_origin"
    world.cookies.clear()
    assert raw_upload(world, csrf, f"key={KEY}", COVER_JPG).status == 401


def test_the_list_search_pick_and_undo(world: World) -> None:
    csrf = world.login()
    listed = json_of(world.call("GET", "/admin/api/covers?view=all"))
    assert listed["available"] is True and [r["key"] for r in listed["rows"]] == [KEY]
    choice = json_of(search(world, csrf))["choices"][0]
    picked = world.call("POST", "/admin/api/covers/fix", {"token": choice["token"]}, csrf=csrf)
    assert picked.status == 200 and json_of(picked)["key"] == KEY
    drain(world)
    (row,) = json_of(world.call("GET", "/admin/api/covers?view=fixed"))["rows"]
    assert row["state"] == "fix"
    image = world.call("GET", row["image"])
    assert image.status == 200 and ("Content-Security-Policy", "sandbox") in image.headers
    cleared = world.call("POST", "/admin/api/covers/fix/clear", {"key": KEY}, csrf=csrf)
    assert cleared.status == 204
    again = world.call("POST", "/admin/api/covers/fix/clear", {"key": KEY}, csrf=csrf)
    assert again.status == 404


def test_a_missing_row_image_is_not_found(world: World) -> None:
    world.login()
    assert world.call("GET", "/admin/api/covers/image/" + "b" * 32 + ".jpg").status == 404
    assert world.call("GET", "/admin/api/covers/image/..%2Ft.db").status == 404


def test_a_replayed_pick_changes_nothing(world: World) -> None:
    csrf = world.login()
    token = json_of(search(world, csrf))["choices"][0]["token"]
    for _ in range(2):
        assert (
            world.call("POST", "/admin/api/covers/fix", {"token": token}, csrf=csrf).status == 200
        )
    assert len(json_of(world.call("GET", "/admin/api/covers?view=fixed"))["rows"]) == 1


def test_a_stale_or_forged_pick_is_refused(world: World) -> None:
    csrf = world.login()
    token = json_of(search(world, csrf))["choices"][0]["token"]
    forged = world.call("POST", "/admin/api/covers/fix", {"token": token + "x"}, csrf=csrf)
    assert json_of(forged) == {"error": "stale_candidate"}
    world.clock.now += 3601
    # The session has idled out meanwhile; a new one does not revive the token.
    csrf = world.login()
    stale = world.call("POST", "/admin/api/covers/fix", {"token": token}, csrf=csrf)
    assert stale.status == 400 and json_of(stale) == {"error": "stale_candidate"}
    shapeless = world.call("POST", "/admin/api/covers/fix", {"token": 12}, csrf=csrf)
    assert shapeless.status == 400 and json_of(shapeless) == {"error": "bad_request"}


def test_a_link_off_deezer_is_refused(world: World) -> None:
    csrf = world.login()
    answer = search(world, csrf, "link", text="https://deezer.page.link/x")
    assert answer.status == 400 and json_of(answer) == {"error": "bad_link"}


def test_searches_are_paced(world: World) -> None:
    csrf = world.login()
    for _ in range(PER_MINUTE):
        assert search(world, csrf).status == 200
    refused = search(world, csrf)
    assert refused.status == 429 and json_of(refused)["error"] == "too_many_searches"
    assert ("Retry-After", str(json_of(refused)["retry_s"])) in refused.headers


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "photo", "key": KEY, "artist": "A", "title": "B", "text": "A"},
        {"kind": "other", "key": KEY, "artist": "A", "title": "B", "text": "A"},
        {"kind": "cover", "key": "", "artist": "A", "title": "B", "text": "A"},
        {"kind": "cover", "key": KEY, "artist": "A", "title": "B", "text": "x" * 301},
        {"kind": "cover", "key": KEY, "artist": "A", "title": "B", "text": "  "},
        {"kind": "cover", "key": KEY, "artist": "A", "title": "B"},
    ],
)
def test_a_search_that_does_not_fit_is_bad_request(world: World, body: dict[str, str]) -> None:
    csrf = world.login()
    answer = world.call("POST", "/admin/api/covers/search", body, csrf=csrf)
    assert answer.status == 400 and json_of(answer) == {"error": "bad_request"}


def test_a_search_that_does_not_fit_spends_no_pace(world: World) -> None:
    csrf = world.login()
    for _ in range(PER_MINUTE + 1):
        assert search(world, csrf, text=" ").status == 400
    assert search(world, csrf).status == 200


def test_an_upload_for_a_key_with_bars_and_colons(world: World) -> None:
    csrf = world.login()
    key = "a: b|c: d"
    query = "key=a%3A+b%7Cc%3A+d&artist=A%3A+B&title=C%3A+D"
    answer = raw_upload(world, csrf, query, COVER_JPG)
    assert answer.status == 200 and json_of(answer)["key"] == key


def test_an_upload_must_be_a_raster_cover_for_a_song(world: World) -> None:
    csrf = world.login()
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    assert raw_upload(world, csrf, f"key={KEY}", svg, "image/svg+xml").status == 415
    refused = raw_upload(world, csrf, f"key={KEY}", svg, "image/png")
    assert json_of(refused) == {"error": "not_a_cover"}
    photo = raw_upload(world, csrf, "key=photo%3Aa", COVER_JPG)
    assert json_of(photo) == {"error": "bad_request"}
    assert raw_upload(world, csrf, "", COVER_JPG).status == 400
    assert raw_upload(world, csrf, f"key={KEY}&key=x", COVER_JPG).status == 400


def test_an_upload_the_store_cannot_keep(world: World) -> None:
    csrf = world.login()
    world.free = 0
    full = raw_upload(world, csrf, f"key={KEY}", COVER_JPG)
    assert full.status == 507 and json_of(full) == {"error": "disk_low"}
    rows = json_of(world.call("GET", "/admin/api/covers?view=fixed"))["rows"]
    assert rows == []


def test_the_row_image_works_with_a_site_pin(world: World) -> None:
    csrf = world.login()
    answer = raw_upload(world, csrf, f"key={KEY}", COVER_JPG)
    assert answer.status == 200
    drain(world)
    world.pin.change("site", "123456", world.pin.status().revision)
    (row,) = json_of(world.call("GET", "/admin/api/covers?view=fixed"))["rows"]
    assert world.call("GET", row["image"]).status == 200


def test_an_orphaned_fix_can_be_cleared(world: World) -> None:
    csrf = world.login()
    gone = "agents of time|hearts collide"
    assert raw_upload(world, csrf, "key=agents+of+time%7Chearts+collide", COVER_JPG).status == 200
    rows = json_of(world.call("GET", "/admin/api/covers?view=fixed"))["rows"]
    assert [(r["key"], r["last_at"]) for r in rows] == [(gone, None)]
    assert world.call("POST", "/admin/api/covers/fix/clear", {"key": gone}, csrf=csrf).status == 204


def test_a_clear_without_a_key_is_bad_request(world: World) -> None:
    csrf = world.login()
    answer = world.call("POST", "/admin/api/covers/fix/clear", {"key": ""}, csrf=csrf)
    assert answer.status == 400 and json_of(answer) == {"error": "bad_request"}


@pytest.mark.parametrize(
    "query",
    ["view=nope", "after=12", "after=x:y", "after=12:", "q=" + "x" * 301, "a=1&b=2&c=3&d=4&e=5"],
)
def test_a_list_query_that_does_not_parse(world: World, query: str) -> None:
    world.login()
    assert world.call("GET", f"/admin/api/covers?{query}").status == 400


def test_a_list_page_after_a_cursor(world: World) -> None:
    world.login()
    first = json_of(world.call("GET", "/admin/api/covers?view=all&q=a"))
    assert [r["key"] for r in first["rows"]] == [KEY] and first["next"] is None
    heard = int(world.clock.now * 1000) - 5
    newer = json_of(world.call("GET", f"/admin/api/covers?view=all&after={heard + 1}:{KEY}"))
    assert [r["key"] for r in newer["rows"]] == [KEY]
    itself = json_of(world.call("GET", f"/admin/api/covers?view=all&after={heard}:{KEY}"))
    assert itself["rows"] == []


def test_without_the_panel_part_the_routes_are_not_found(tmp_path: Path) -> None:
    plain = World(tmp_path)
    try:
        plain.login()
        assert plain.call("GET", "/admin/api/covers").status == 404
    finally:
        plain.close()


def test_a_key_past_the_bound_is_bad_request(world: World) -> None:
    csrf = world.login()
    answer = world.call("POST", "/admin/api/covers/fix/clear", {"key": "x" * 1001}, csrf=csrf)
    assert answer.status == 400 and json_of(answer) == {"error": "bad_request"}
