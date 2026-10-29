import json
import logging
from collections.abc import Callable

import pytest

from tests.fakes import FakeHttp
from tunebox import crypto
from tunebox.catalogs import Catalogs
from tunebox.coversearch import (
    BUDGET_S,
    CLOCK_SKEW_S,
    OFFERS,
    PER_MINUTE,
    PURPOSE,
    TOKEN_S,
    BadLink,
    CoverSearch,
    NothingToSearch,
    Pick,
    SearchKind,
    album_id,
)
from tunebox.pacer import Busy

SECRET = b"k" * 32


def link(kind: str, n: int) -> str:
    return f"https://cdn-images.dzcdn.net/images/{kind}/{n:032x}/1000x1000-000000-80-0-0.jpg"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def searcher(
    respond: Callable[[str], object], clock: Clock | None = None, step_s: float = 0.0
) -> tuple[CoverSearch, FakeHttp]:
    """step_s: how far the clock moves on every catalogue call."""
    clock = clock or Clock()

    def answer(url: str) -> bytes:
        clock.now += step_s
        return json.dumps(respond(url)).encode()

    http = FakeHttp(answer)
    return CoverSearch(Catalogs(http, 0, 0), lambda: SECRET, clock=clock, monotonic=clock), http


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("https://www.deezer.com/album/655089251", "655089251"),
        (
            "  https://deezer.com/en/album/655089251?utm_source=deezer&utm_content=album  ",
            "655089251",
        ),
        ("https://www.deezer.com/nl/album/1", "1"),
        ("https://www.deezer.com/album/1234567890123", None),
        ("https://www.deezer.com/album/١٢٣", None),
        ("http://www.deezer.com/album/655089251", None),
        ("https://deezer.page.link/AbCdEf", None),
        ("https://www.deezer.com.evil.invalid/album/1", None),
        ("https://www2.deezer.com/album/1", None),
        ("https://www.deezer.com/track/655089251", None),
        ("https://www.deezer.com/EN/album/1", None),
        ("https://www.deezer.com/album/1#x", None),
        ("", None),
    ],
)
def test_album_links(text: str, found: str | None) -> None:
    assert album_id(text) == found


def test_a_bad_link_spends_no_search() -> None:
    search, http = searcher(lambda _url: {})
    for _ in range(PER_MINUTE + 1):
        with pytest.raises(BadLink):
            search.search("link", "a|b", "A", "B", "https://deezer.page.link/x")
    assert http.urls == []


def test_cover_candidates_mix_both_catalogues_and_sign_each() -> None:
    def respond(url: str) -> object:
        if "itunes" in url:
            return {
                "results": [
                    {
                        "artistName": "A",
                        "collectionName": f"I{n}",
                        "artworkUrl100": f"https://is1-ssl.mzstatic.com/image/thumb/{n}/100x100bb.jpg",
                    }
                    for n in range(10)
                ]
            }
        return {
            "data": [
                {"artist": {"name": "A"}, "album": {"title": f"D{n}", "cover_xl": link("cover", n)}}
                for n in range(10)
            ]
        }

    search, _ = searcher(respond)
    answer = search.search("cover", "a|b", "A", "B", "a b")
    assert len(answer.choices) == OFFERS and answer.unanswered == []
    assert [c.source for c in answer.choices].count("itunes") == 6
    pick = search.redeem(answer.choices[0].token)
    assert pick == Pick("a|b", link("cover", 0), "Deezer: A - D0", "deezer", "A", "B", 1_000.0)


def test_links_off_the_catalogue_hosts_are_never_offered() -> None:
    answer = {
        "data": [
            {
                "artist": {"name": "A"},
                "album": {"title": "X", "cover_xl": "https://example.invalid/x.jpg"},
            }
        ]
    }
    search, _ = searcher(lambda url: answer if "deezer" in url else {"results": []})
    assert search.search("cover", "a|b", "A", "B", "a b").choices == []


def test_a_catalogue_that_fails_is_named_and_the_other_still_answers() -> None:
    def respond(url: str) -> object:
        if "itunes" in url:
            return {
                "results": [
                    {
                        "artistName": "A",
                        "collectionName": "I",
                        "artworkUrl100": "https://is1-ssl.mzstatic.com/x/100x100bb.jpg",
                    }
                ]
            }
        return {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}

    search, _ = searcher(respond)
    answer = search.search("cover", "a|b", "A", "B", "a b")
    assert answer.unanswered == ["deezer"] and [c.source for c in answer.choices] == ["itunes"]


def test_a_photo_search_checks_five_and_stops_at_the_budget() -> None:
    artists = {
        "data": [
            {
                "id": n + 1,
                "name": "Jessica",
                "nb_fan": 1000 - n,
                "picture_xl": link("artist", n),
                "picture_medium": link("artist", n),
            }
            for n in range(25)
        ]
    }

    def respond(url: str) -> object:
        if "/search/artist" in url:
            return artists
        # The first artist's picture (image id 0) is one of its release
        # covers: _IMAGE_ID reads the same id from artist and cover links.
        if "/artist/1/albums" in url:
            return {"data": [{"cover_xl": link("cover", 0)}]}
        return {"data": []}

    search, http = searcher(respond)
    answer = search.search("photo", "photo:jessica", "JESSICA", "", "JESSICA")
    assert len(answer.choices) == 10
    assert len(http.urls) == 1 + 5
    assert [c.release for c in answer.choices] == [True] + [False] * 4 + [None] * 5
    assert answer.no_photo is not None
    none = search.redeem(answer.no_photo)
    assert none is not None and none.link is None and none.source == "none"

    slow, slow_http = searcher(respond, step_s=BUDGET_S / 2)
    cut = slow.search("photo", "photo:jessica", "JESSICA", "", "JESSICA")
    # Two calls fill the budget: the artist search and one release check.
    assert len(slow_http.urls) == 2
    assert [c.release is not None for c in cut.choices[:2]] == [True, False]


def test_tokens_expire_and_cannot_be_forged() -> None:
    clock = Clock()
    search, _ = searcher(
        lambda _url: {
            "data": [
                {"artist": {"name": "A"}, "album": {"title": "X", "cover_xl": link("cover", 1)}}
            ],
            "results": [],
        },
        clock=clock,
    )
    token = search.search("cover", "a|b", "A", "B", "a").choices[0].token
    assert search.redeem(token) is not None
    payload, _, mac = token.rpartition(".")
    assert search.redeem(payload.replace("a|b", "c|d") + "." + mac) is None
    clock.now += TOKEN_S + 1
    assert search.redeem(token) is None


def test_searches_are_paced(caplog: pytest.LogCaptureFixture) -> None:
    search, _ = searcher(lambda _url: {"data": [], "results": []})
    with caplog.at_level(logging.DEBUG):
        for _ in range(PER_MINUTE):
            search.search("cover", "a|b", "A", "B", "a")
        with pytest.raises(Busy) as used_up:
            search.search("cover", "a|b", "A", "B", "a")
    assert str(used_up.value) == "too_many_searches"
    logged = [getattr(r, "fields", {}) for r in caplog.records if r.getMessage() == "cover_search"]
    assert logged[-1]["outcome"] == "busy" and logged[0]["kind"] == "cover"


def test_a_search_must_fit_its_key() -> None:
    search, _ = searcher(lambda _url: {})
    with pytest.raises(ValueError):
        search.search("photo", "a|b", "A", "", "A")
    with pytest.raises(ValueError):
        search.search("cover", "photo:a", "A", "", "A")


def test_an_album_link_offers_that_album_as_a_link_fix() -> None:
    album = {"title": "Collide", "cover_xl": link("cover", 7), "artist": {"name": "A"}}

    def respond(url: str) -> object:
        if url.endswith("/album/655089251"):
            return album
        if url.endswith("/album/1"):
            return {"error": {"type": "DataException", "message": "no data", "code": 800}}
        return {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}

    search, http = searcher(respond)
    answer = search.search("link", "a|b", "A", "B", "https://www.deezer.com/album/655089251")
    assert [c.album for c in answer.choices] == ["Collide"] and answer.unanswered == []
    assert len(http.urls) == 1
    pick = search.redeem(answer.choices[0].token)
    assert pick is not None and pick.source == "link" and pick.link == link("cover", 7)
    # No such album is an answer; a refusal is a catalogue that did not answer.
    missing = search.search("link", "a|b", "A", "B", "https://www.deezer.com/album/1")
    assert missing.choices == [] and missing.unanswered == []
    refused = search.search("link", "a|b", "A", "B", "https://www.deezer.com/album/2")
    assert refused.choices == [] and refused.unanswered == ["deezer"]


def test_a_throttle_wait_past_the_budget_sends_nothing() -> None:
    def respond(url: str) -> object:
        if "itunes" in url:
            return {"results": []}
        return {
            "data": [
                {"artist": {"name": "A"}, "album": {"title": "X", "cover_xl": link("cover", 1)}}
            ]
        }

    # Another iTunes call ran just before (the worker's, or an earlier
    # search's): the next may start only well after this search's budget.
    clock = Clock()
    http = FakeHttp(lambda url: json.dumps(respond(url)).encode())
    search = CoverSearch(
        Catalogs(http, 0, BUDGET_S * 10), lambda: SECRET, clock=clock, monotonic=clock
    )
    search.search("cover", "a|b", "A", "B", "a")
    asked = len(http.urls)
    answer = search.search("cover", "a|b", "A", "B", "a")
    assert [u for u in http.urls[asked:] if "itunes" in u] == []
    assert answer.unanswered == ["itunes"] and len(answer.choices) == 1


@pytest.mark.parametrize(
    ("kind", "key", "artist", "text"),
    [
        ("cover", "a|b", "A", "   "),
        ("cover", "a|b", "A", ""),
        ("photo", "photo:a", " ", " "),
    ],
)
def test_blank_text_spends_no_search(kind: SearchKind, key: str, artist: str, text: str) -> None:
    search, http = searcher(lambda _url: {"data": [], "results": []})
    for _ in range(PER_MINUTE + 1):
        with pytest.raises(NothingToSearch):
            search.search(kind, key, artist, "B", text)
    assert http.urls == []
    search.search("cover", "a|b", "A", "B", "a")


def test_a_token_from_slightly_ahead_still_counts() -> None:
    def token(issued_at: float) -> str:
        payload = {
            "k": "a|b",
            "l": link("cover", 1),
            "b": "x",
            "s": "deezer",
            "a": "A",
            "t": "",
            "i": issued_at,
        }
        return crypto.sign(SECRET, json.dumps(payload), PURPOSE)

    search, _ = searcher(lambda _url: {})
    assert search.redeem(token(1_000.0 + CLOCK_SKEW_S - 1)) is not None
    assert search.redeem(token(1_000.0 + CLOCK_SKEW_S + 1)) is None


def test_a_cover_search_out_of_budget_names_the_catalogue_it_skipped() -> None:
    def respond(url: str) -> object:
        return {
            "data": [
                {"artist": {"name": "A"}, "album": {"title": "X", "cover_xl": link("cover", 1)}}
            ]
        }

    search, http = searcher(respond, step_s=BUDGET_S)
    answer = search.search("cover", "a|b", "A", "B", "a b")
    assert len(http.urls) == 1 and answer.unanswered == ["itunes"]
    assert [c.source for c in answer.choices] == ["deezer"]


def test_deeply_nested_json_counts_as_a_catalogue_that_did_not_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    itunes = {
        "results": [{"artistName": "A", "collectionName": "I", "artworkUrl100": link("cover", 2)}]
    }

    def answer(url: str) -> bytes:
        return json.dumps(itunes).encode() if "itunes" in url else b"[" * 100_000

    clock = Clock()
    search = CoverSearch(
        Catalogs(FakeHttp(answer), 0, 0), lambda: SECRET, clock=clock, monotonic=clock
    )
    found = search.search("cover", "a|b", "A", "B", "a b")
    assert found.unanswered == ["deezer"]
    assert [c.source for c in found.choices] == ["itunes"]
    assert "search_failed" in caplog.messages


def test_an_image_both_catalogues_offer_is_shown_once() -> None:
    same = link("cover", 3)

    def respond(url: str) -> object:
        if "itunes" in url:
            return {"results": [{"artistName": "A", "collectionName": "I", "artworkUrl100": same}]}
        return {"data": [{"artist": {"name": "A"}, "album": {"title": "D", "cover_xl": same}}]}

    search, _ = searcher(respond)
    assert [c.album for c in search.search("cover", "a|b", "A", "B", "a").choices] == ["D"]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[1]",
        json.dumps({"k": "a|b", "l": None, "b": "x", "s": "none", "a": "A", "t": "", "i": 1000}),
        json.dumps(
            {
                "k": "a|b",
                "l": "https://example.invalid/x.jpg",
                "b": "x",
                "s": "deezer",
                "a": "A",
                "t": "",
                "i": 1000,
            }
        ),
        json.dumps(
            {
                "k": "a|b",
                "l": link("cover", 1),
                "b": "x",
                "s": "elsewhere",
                "a": "A",
                "t": "",
                "i": 1000,
            }
        ),
        json.dumps(
            {
                "k": "a|b",
                "l": link("cover", 1),
                "b": "x",
                "s": "deezer",
                "a": "A",
                "t": "",
                "i": 2000,
            }
        ),
        # "No photo" carries no link, and only "No photo" may lack one.
        json.dumps(
            {
                "k": "photo:a",
                "l": link("artist", 1),
                "b": "x",
                "s": "none",
                "a": "A",
                "t": "",
                "i": 1000,
            }
        ),
        json.dumps(
            {"k": "photo:a", "l": None, "b": "x", "s": "deezer", "a": "A", "t": "", "i": 1000}
        ),
    ],
)
def test_a_signed_token_still_has_to_hold_a_pick(payload: str) -> None:
    search, _ = searcher(lambda _url: {})
    assert search.redeem(crypto.sign(SECRET, payload, PURPOSE)) is None


@pytest.mark.parametrize("field", ["k", "b", "a", "t", "l"])
def test_a_token_with_a_lone_surrogate_is_no_pick(field: str) -> None:
    fields: dict[str, object] = {
        "k": "a|b",
        "l": link("cover", 1),
        "b": "x",
        "s": "deezer",
        "a": "A",
        "t": "",
        "i": 1000,
    }
    fields[field] = f"{fields[field]}\ud800"
    search, _ = searcher(lambda _url: {})
    # json.dumps writes the surrogate as an escape, so the token signs.
    assert search.redeem(crypto.sign(SECRET, json.dumps(fields), PURPOSE)) is None


def test_a_release_check_that_fails_leaves_the_mark_unknown() -> None:
    artists = {"data": [{"id": 1, "name": "A", "nb_fan": 1, "picture_xl": link("artist", 1)}]}

    def respond(url: str) -> object:
        return artists if "/search/artist" in url else {"error": {"code": 4}}

    search, _ = searcher(respond)
    answer = search.search("photo", "photo:a", "A", "", "A")
    assert [c.release for c in answer.choices] == [None] and answer.unanswered == []
