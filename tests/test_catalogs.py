import json
import urllib.parse
from collections.abc import Callable

import pytest

from tests.fakes import FakeHttp, make_track
from tunebox.catalogs import Catalogs, Found, Offer, SearchError, Throttle, is_release_cover
from tunebox.http import HttpError

COVER = "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg"
ARTIST_PHOTO = "https://cdn-images.dzcdn.net/images/artist/ok/1000x1000.jpg"
PLACEHOLDER = "https://cdn-images.dzcdn.net/images/artist//1000x1000.jpg"
# The same grey outline, named by the md5 of an empty string.
BLANK = "https://cdn-images.dzcdn.net/images/artist/d41d8cd98f00b204e9800998ecf8427e/1000x1000-000000-80-0-0.jpg"


def deezer_song(artist: str, title: str, cover: str = COVER, album: str = "") -> dict[str, object]:
    return {
        "title": title,
        "artist": {"name": artist},
        "album": {"cover_xl": cover, "title": album},
    }


def itunes_song(artist: str, title: str, art: str) -> dict[str, object]:
    return {"artistName": artist, "trackName": title, "artworkUrl100": art}


def body(payload: object) -> bytes:
    return json.dumps(payload).encode()


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def catalogs(http: FakeHttp) -> Catalogs:
    return Catalogs(http, deezer_interval=0, itunes_interval=0)


def test_deezer_hit_on_the_first_query() -> None:
    http = FakeHttp(lambda _url: body({"data": [deezer_song("Fisher", "Stay")]}))
    found = catalogs(http).find(make_track("FISHER", "Stay"))
    assert found == Found(COVER, "album", "deezer: Fisher - Stay")
    assert len(http.urls) == 1
    assert query(http.urls[0])["q"] == 'artist:"FISHER" track:"Stay"'


def test_deezer_retries_without_brackets() -> None:
    def respond(url: str) -> bytes:
        if '"Houdini"' in query(url).get("q", ""):
            return body({"data": [deezer_song("Dua Lipa", "Houdini")]})
        return body({"data": []})

    http = FakeHttp(respond)
    found = catalogs(http).find(make_track("DUA LIPA", "Houdini (Adam Port Remix)"))
    assert found is not None
    assert [query(u)["q"] for u in http.urls] == [
        'artist:"DUA LIPA" track:"Houdini (Adam Port Remix)"',
        'artist:"DUA LIPA" track:"Houdini"',
    ]


def test_itunes_after_deezer_with_large_artwork() -> None:
    def respond(url: str) -> bytes:
        if "itunes.apple.com" in url:
            art = "https://is1-ssl.mzstatic.com/image/thumb/x/cover.jpg/100x100bb.jpg"
            return body({"results": [itunes_song("Adriatique", "Palm Of Your Hands", art)]})
        return body({"data": []})

    found = catalogs(FakeHttp(respond)).find(make_track("ADRIATIQUE", "Palm Of Your Hands"))
    assert found is not None
    assert found.url.endswith("/1000x1000bb.jpg")
    assert found.match == "itunes: Adriatique - Palm Of Your Hands"


@pytest.mark.parametrize("placeholder", [PLACEHOLDER, BLANK])
def test_artist_photo_on_an_exact_name_skipping_placeholders(placeholder: str) -> None:
    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            photos = [
                {"name": "Jef Nice", "nb_fan": 9, "picture_xl": placeholder},
                {"name": "Jef Nice", "picture_xl": ARTIST_PHOTO},
            ]
            return body({"data": photos})
        return body({"data": [], "results": []})

    found = catalogs(FakeHttp(respond)).find(make_track("JEF NICE", "When Le Soleil Takes Over"))
    assert found == Found(ARTIST_PHOTO, "artist", "deezer artist: Jef Nice")


def test_nothing_found_is_a_miss() -> None:
    http = FakeHttp(lambda _url: body({"data": [], "results": []}))
    assert catalogs(http).find(make_track()) is None


def test_upstream_failure_without_a_find_is_an_error() -> None:
    def respond(url: str) -> bytes:
        if "deezer" in url:
            raise HttpError("connection refused")
        return body({"results": []})

    with pytest.raises(SearchError, match="connection refused"):
        catalogs(FakeHttp(respond)).find(make_track())


def test_deezer_error_body_counts_as_a_failure() -> None:
    def respond(url: str) -> bytes:
        if "deezer" in url:
            error = {"type": "Exception", "message": "Quota limit exceeded", "code": 4}
            return body({"error": error})
        return body({"results": []})

    with pytest.raises(SearchError, match="Quota"):
        catalogs(FakeHttp(respond)).find(make_track())


def test_failure_does_not_hide_a_later_find() -> None:
    def respond(url: str) -> bytes:
        if "api.deezer.com/search?" in url:
            raise HttpError("timed out")
        if "itunes" in url:
            art = "https://example.invalid/100x100bb.jpg"
            return body({"results": [itunes_song("Fisher", "Stay", art)]})
        return body({"data": []})

    found = catalogs(FakeHttp(respond)).find(make_track())
    assert found is not None
    assert found.match.startswith("itunes:")


def test_throttle_spaces_calls() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(3.0, clock=lambda: now[0], sleep=sleep)
    throttle.wait()
    throttle.wait()
    assert slept == [3.0]


def test_artist_photo_needs_a_comparable_name() -> None:
    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            return body({"data": [{"name": "Ленинград", "picture_xl": ARTIST_PHOTO}]})
        return body({"data": [], "results": []})

    assert catalogs(FakeHttp(respond)).find(make_track("ЛСП", "Monetka")) is None


def test_a_duet_sent_as_one_name_is_searched_by_its_first_artist() -> None:
    true = "https://example.invalid/images/cover/true/1000x1000-000000-80-0-0.jpg"
    acoustic = "https://example.invalid/images/cover/acoustic/1000x1000-000000-80-0-0.jpg"

    def respond(url: str) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        # Deezer's artist: filter finds nothing for these at the time of
        # writing; only the loose queries answer.
        if query.startswith("artist:"):
            return body({"data": []})
        if query == "AVICII Wake Me Up":
            return body({"data": [deezer_song("Avicii", "Wake Me Up", true, "True")]})
        if "ALOE BLACC" in query:
            return body(
                {
                    "data": [
                        deezer_song(
                            "Aloe Blacc", "Wake Me Up (Acoustic)", acoustic, "Lift Your Spirit"
                        )
                    ]
                }
            )
        return body({"data": [], "results": []})

    http = FakeHttp(respond)
    found = catalogs(http).find(make_track("AVICII & ALOE BLACC", "Wake Me Up"))
    assert found is not None and found.url == true


def test_a_single_name_keeps_one_loose_query() -> None:
    http = FakeHttp(lambda _url: body({"data": [], "results": []}))
    catalogs(http).find(make_track("FISHER", "Stay"))
    loose = [url for url in http.urls if "api.deezer.com/search?" in url and "artist%3A" not in url]
    assert len(loose) == 1


def test_deezer_reads_the_album_to_prefer_the_own_release() -> None:
    mix = "https://example.invalid/images/cover/mix/1000x1000-000000-80-0-0.jpg"
    own = "https://example.invalid/images/cover/own/1000x1000-000000-80-0-0.jpg"

    def respond(url: str) -> bytes:
        if "api.deezer.com/search?" in url:
            return body(
                {
                    "data": [
                        deezer_song("Tchami", "Adieu (Original Mix)", mix, "Poolside Miami 2017"),
                        deezer_song("Tchami", "Adieu (Radio Edit)", own, "Adieu (Radio Edit)"),
                    ]
                }
            )
        return body({"data": [], "results": []})

    found = catalogs(FakeHttp(respond)).find(make_track("TCHAMI", "Adieu"))
    assert found is not None and found.url == own


def test_itunes_reads_the_collection_to_prefer_the_own_release() -> None:
    mix = "https://example.invalid/mix/100x100bb.jpg"
    own = "https://example.invalid/own/100x100bb.jpg"

    def respond(url: str) -> bytes:
        if "itunes.apple.com" in url:
            return body(
                {
                    "results": [
                        dict(
                            itunes_song("Tchami", "Adieu (Original Mix)", mix),
                            collectionName="Poolside Miami 2017",
                        ),
                        dict(
                            itunes_song("Tchami", "Adieu (Radio Edit)", own),
                            collectionName="Adieu - Single",
                        ),
                    ]
                }
            )
        return body({"data": []})

    found = catalogs(FakeHttp(respond)).find(make_track("TCHAMI", "Adieu"))
    assert found is not None and found.url == own.replace("100x100bb", "1000x1000bb")


def test_itunes_falls_back_to_the_full_name_of_a_band_with_an_ampersand() -> None:
    http = FakeHttp(lambda _url: body({"data": [], "results": []}))
    catalogs(http).find(make_track("YEARS & YEARS", "King"))
    terms = [query(url)["term"] for url in http.urls if "itunes.apple.com" in url]
    assert terms == ["YEARS King", "YEARS & YEARS King"]


def test_artist_photo_asks_deezer_only_and_the_song_before_the_name() -> None:
    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            return body({"data": [{"name": "Jef Nice", "picture_xl": ARTIST_PHOTO}]})
        return body({"data": []})

    http = FakeHttp(respond)
    found = catalogs(http).artist_photo(make_track("JEF NICE", "When Le Soleil Takes Over"))
    assert found == Found(ARTIST_PHOTO, "artist", "deezer artist: Jef Nice")
    assert all("api.deezer.com" in url for url in http.urls)
    assert "/search/artist" in http.urls[-1]
    assert not any("/search/artist" in url for url in http.urls[:-1])


def test_artist_photo_alone_tells_a_failure_from_a_miss() -> None:
    def refused(_url: str) -> bytes:
        raise HttpError("connection refused")

    with pytest.raises(SearchError, match="connection refused"):
        catalogs(FakeHttp(refused)).artist_photo(make_track())
    assert catalogs(FakeHttp(lambda _url: body({"data": []}))).artist_photo(make_track()) is None


def test_artist_photo_from_the_profile_with_the_most_fans() -> None:
    duplicate = "https://example.invalid/images/artist/dup/1000x1000-000000-80-0-0.jpg"

    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            profiles = [
                {"name": "Route 94", "nb_fan": 1690, "picture_xl": duplicate},
                {"name": "Route 94", "nb_fan": 335901, "picture_xl": ARTIST_PHOTO},
                {"name": "Route 8", "nb_fan": 999999, "picture_xl": duplicate},
            ]
            return body({"data": profiles})
        return body({"data": [], "results": []})

    found = catalogs(FakeHttp(respond)).artist_photo(make_track("ROUTE 94", "4Get The Girl"))
    assert found == Found(ARTIST_PHOTO, "artist", "deezer artist: Route 94")


def test_artist_photo_comes_from_the_artist_of_the_song() -> None:
    # Deezer has several artists named Rafael; a name search ranks others
    # first ("Rafa'EL" reads as "rafael" too). The song names its own.
    own = "https://example.invalid/images/artist/own/1000x1000-000000-80-0-0.jpg"
    other = "https://example.invalid/images/artist/other/1000x1000-000000-80-0-0.jpg"

    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            return body({"data": [{"name": "Rafa'EL", "nb_fan": 60, "picture_xl": other}]})
        if "api.deezer.com/search?" in url and "artist%3A" not in url:
            song = deezer_song(
                "Rafael", "Gotta Be Cool (Luch Remix)", COVER, "Gotta Be Cool (Luch Remix)"
            )
            song["artist"] = {"name": "Rafael", "picture_xl": own}
            return body({"data": [song]})
        return body({"data": [], "results": []})

    found = catalogs(FakeHttp(respond)).artist_photo(
        make_track("RAFAEL", "Gotta Be Cool (Luch Remix)")
    )
    assert found == Found(own, "artist", "deezer artist: Rafael")


@pytest.mark.parametrize("placeholder", [PLACEHOLDER, BLANK])
def test_artist_photo_falls_back_to_the_name_when_the_song_has_no_photo(
    placeholder: str,
) -> None:
    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            return body({"data": [{"name": "Jef Nice", "picture_xl": ARTIST_PHOTO}]})
        if "api.deezer.com/search?" in url and "artist%3A" not in url:
            song = deezer_song("Jef Nice", "When Le Soleil Takes Over")
            song["artist"] = {"name": "Jef Nice", "picture_xl": placeholder}
            return body({"data": [song]})
        return body({"data": [], "results": []})

    found = catalogs(FakeHttp(respond)).artist_photo(
        make_track("JEF NICE", "When Le Soleil Takes Over")
    )
    assert found == Found(ARTIST_PHOTO, "artist", "deezer artist: Jef Nice")


def test_artist_photo_is_of_the_first_named_artist() -> None:
    first = "https://example.invalid/images/artist/first/1000x1000-000000-80-0-0.jpg"
    second = "https://example.invalid/images/artist/second/1000x1000-000000-80-0-0.jpg"

    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            return body({"data": [{"name": "Ben Böhmer", "picture_xl": first}]})
        if "api.deezer.com/search?" in url and "artist%3A" not in url:
            song = deezer_song("Spencer Brown", "Phosphene", COVER, "Phosphene")
            song["artist"] = {"name": "Spencer Brown", "picture_xl": second}
            return body({"data": [song]})
        return body({"data": [], "results": []})

    track = make_track("BEN BOHMER, SPENCER BROWN", "Phosphene")
    found = catalogs(FakeHttp(respond)).artist_photo(track)
    assert found == Found(first, "artist", "deezer artist: Ben Böhmer")


def dz(kind: str, image_id: str) -> str:
    return f"https://cdn-images.dzcdn.net/images/{kind}/{image_id}/1000x1000-000000-80-0-0.jpg"


SINGLE = "d3c17a0458e85ebdef6bd9605b673b4b"
EYES = "f056445307a66a4aafa3cdfa726f99f7"
PORTRAIT = "0123456789abcdef0123456789abcdef"


def kotier(
    song_picture: str, profile_picture: str, albums: list[str] | None
) -> Callable[[str], bytes]:
    """Deezer as it answers for Kotiēr: no photo, release covers instead.
    albums None: the release list cannot be had."""

    def respond(url: str) -> bytes:
        if "/artist/97185152/albums" in url:
            if albums is None:
                raise HttpError("connection refused")
            return body({"data": [{"cover_xl": dz("cover", a)} for a in albums]})
        if "/search/artist" in url:
            profile = {"id": 97185152, "name": "Kotiēr", "nb_fan": 322}
            return body({"data": [profile | {"picture_xl": profile_picture}]})
        if "api.deezer.com/search?" in url and "artist%3A" not in url:
            song = deezer_song("Kotiēr", "Smalltown Boy", dz("cover", SINGLE), "Smalltown Boy")
            song["artist"] = {"id": 97185152, "name": "Kotiēr", "picture_xl": song_picture}
            return body({"data": [song]})
        return body({"data": [], "results": []})

    return respond


def test_a_songs_artist_picture_that_is_its_cover_is_no_photo() -> None:
    respond = kotier(dz("artist", SINGLE), dz("artist", PORTRAIT), [SINGLE, EYES])
    found = catalogs(FakeHttp(respond)).artist_photo(make_track("KOTIER", "Smalltown Boy"))
    # The name search finds the profile, whose picture is a real photo.
    assert found == Found(dz("artist", PORTRAIT), "artist", "deezer artist: Kotiēr")


def test_a_profile_picture_that_is_a_release_cover_is_no_photo() -> None:
    respond = kotier(dz("artist", SINGLE), dz("artist", EYES), [SINGLE, EYES])
    track = make_track("KOTIER", "Smalltown Boy")
    assert catalogs(FakeHttp(respond)).artist_photo(track) is None


def test_a_songs_artist_picture_from_another_release_is_no_photo() -> None:
    respond = kotier(dz("artist", EYES), dz("artist", EYES), [SINGLE, EYES])
    track = make_track("KOTIER", "Smalltown Boy")
    assert catalogs(FakeHttp(respond)).artist_photo(track) is None


def test_a_real_photo_is_kept_after_the_release_check() -> None:
    respond = kotier(dz("artist", PORTRAIT), dz("artist", EYES), [SINGLE, EYES])
    http = FakeHttp(respond)
    found = catalogs(http).artist_photo(make_track("KOTIER", "Smalltown Boy"))
    assert found == Found(dz("artist", PORTRAIT), "artist", "deezer artist: Kotiēr")
    assert query(next(u for u in http.urls if "/albums" in u))["limit"] == "100"


def test_an_unanswered_release_check_is_a_failure_not_a_miss() -> None:
    respond = kotier(dz("artist", PORTRAIT), dz("artist", PORTRAIT), None)
    with pytest.raises(SearchError, match="connection refused"):
        catalogs(FakeHttp(respond)).artist_photo(make_track("KOTIER", "Smalltown Boy"))


def test_a_cover_search_falls_back_to_a_real_photo_only() -> None:
    # No song found anywhere: find() shows the artist's photo, never a cover
    # standing in for one.
    def respond(url: str) -> bytes:
        if "/artist/97185152/albums" in url:
            return body({"data": [{"cover_xl": dz("cover", EYES)}]})
        if "/search/artist" in url:
            profile = {"id": 97185152, "name": "Kotiēr", "picture_xl": dz("artist", EYES)}
            return body({"data": [profile]})
        return body({"data": [], "results": []})

    assert catalogs(FakeHttp(respond)).find(make_track("KOTIER", "Unreleased ID")) is None


def test_an_artists_releases_are_asked_once_per_search() -> None:
    # The song's picture is a release cover, so the name search follows and
    # finds the same artist.
    http = FakeHttp(kotier(dz("artist", EYES), dz("artist", PORTRAIT), [SINGLE, EYES]))
    found = catalogs(http).artist_photo(make_track("KOTIER", "Smalltown Boy"))
    assert found == Found(dz("artist", PORTRAIT), "artist", "deezer artist: Kotiēr")
    assert sum("/albums" in url for url in http.urls) == 1


def test_deezer_offers_ask_the_loose_query_once() -> None:
    answer = {
        "data": [
            {
                "title": "Collide",
                "artist": {"name": "Agents of Time"},
                "album": {"title": "Collide", "cover_xl": COVER, "cover_medium": COVER + "?m"},
            },
            {"title": "No cover", "artist": {"name": "X"}, "album": {"title": "Y"}},
        ]
    }
    http = FakeHttp(lambda _url: body(answer))
    failures: list[str] = []
    offers = catalogs(http).deezer_offers("agents of time hearts collide", failures)
    assert offers == [Offer(COVER, COVER + "?m", "Agents of Time", "Collide", "deezer")]
    assert failures == [] and len(http.urls) == 1
    assert "q=agents+of+time+hearts+collide" in http.urls[0]


def test_an_album_by_id_a_missing_one_and_a_refusal() -> None:
    album = {"title": "Collide", "cover_xl": COVER, "cover_medium": COVER, "artist": {"name": "A"}}

    def respond(url: str) -> bytes:
        if url.endswith("/655089251"):
            return body(album)
        if url.endswith("/3"):
            return body({"error": "unreadable"})
        if url.endswith("/1"):
            return body({"error": {"type": "DataException", "message": "no data", "code": 800}})
        return body({"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}})

    found = catalogs(FakeHttp(respond))
    assert found.deezer_album("655089251", []) == Offer(COVER, COVER, "A", "Collide", "deezer")
    missing: list[str] = []
    assert found.deezer_album("1", missing) is None and missing == []
    refused: list[str] = []
    assert found.deezer_album("2", refused) is None and refused
    odd: list[str] = []
    assert found.deezer_album("3", odd) is None and odd


def test_itunes_offers_store_the_large_link_and_show_a_small_one() -> None:
    art = "https://is1-ssl.mzstatic.com/image/thumb/x/cover.jpg/100x100bb.jpg"
    answer = {"results": [{"artistName": "A", "collectionName": "C", "artworkUrl100": art}]}
    offers = catalogs(FakeHttp(lambda _url: body(answer))).itunes_offers("a", [])
    assert offers == [
        Offer(
            art.replace("100x100bb", "1000x1000bb"),
            art.replace("100x100bb", "300x300bb"),
            "A",
            "C",
            "itunes",
        )
    ]


def test_a_budgeted_wait_never_starts_past_the_budget() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(3.0, clock=lambda: now[0], sleep=sleep)
    assert throttle.wait(lambda: 1.0)
    assert not throttle.wait(lambda: 3.0) and slept == []
    assert throttle.wait(lambda: 3.5) and slept == [3.0]
    assert not throttle.wait(lambda: 0.0)


def test_a_call_out_of_budget_is_not_sent_and_counts_as_failed() -> None:
    http = FakeHttp(lambda _url: body({"data": []}))
    failures: list[str] = []
    assert catalogs(http).deezer_offers("x", failures, lambda: 0.0) == []
    assert http.urls == [] and failures


def test_artists_most_fans_first_without_placeholders() -> None:
    answer = {
        "data": [
            {"id": 1, "name": "Jessica", "nb_fan": 10, "picture_xl": dz("artist", "a" * 32)},
            {"id": 2, "name": "Jessica", "nb_fan": 900, "picture_xl": dz("artist", "b" * 32)},
            {"id": 3, "name": "Jessica", "nb_fan": 5000, "picture_xl": PLACEHOLDER},
        ]
    }
    found = catalogs(FakeHttp(lambda _url: body(answer))).deezer_artists("Jessica", [])
    assert [o.artist_id for o in found] == ["2", "1"]


def test_release_covers_and_the_cover_rule() -> None:
    answer = {"data": [{"cover_xl": dz("cover", SINGLE)}, {"cover_xl": ""}]}
    covers = catalogs(FakeHttp(lambda _url: body(answer))).release_covers("7", [])
    assert covers is not None and covers == frozenset({SINGLE})
    assert is_release_cover(dz("artist", SINGLE), covers)
    assert not is_release_cover(dz("artist", "b" * 32), covers)
    assert not is_release_cover(PLACEHOLDER, covers)
    failures: list[str] = []
    down = catalogs(FakeHttp(lambda _url: body({"error": {"code": 4}})))
    assert down.release_covers("7", failures) is None and failures


def test_base_addresses_can_point_elsewhere() -> None:
    http = FakeHttp(lambda _url: b'{"data": [], "results": []}')
    found = Catalogs(
        http,
        0,
        0,
        deezer="http://catalogue.invalid/deezer",
        itunes="http://catalogue.invalid/itunes/search",
    )
    found.deezer_offers("x", [])
    found.itunes_offers("x", [])
    assert [u.split("?")[0] for u in http.urls] == [
        "http://catalogue.invalid/deezer/search",
        "http://catalogue.invalid/itunes/search",
    ]
