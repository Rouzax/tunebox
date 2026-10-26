import json
import urllib.parse

import pytest

from tests.fakes import FakeHttp, make_track
from tunebox.catalogs import Catalogs, Found, SearchError, Throttle
from tunebox.http import HttpError

COVER = "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg"
ARTIST_PHOTO = "https://cdn-images.dzcdn.net/images/artist/ok/1000x1000.jpg"
PLACEHOLDER = "https://cdn-images.dzcdn.net/images/artist//1000x1000.jpg"


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


def test_artist_photo_on_an_exact_name_skipping_placeholders() -> None:
    def respond(url: str) -> bytes:
        if "/search/artist" in url:
            photos = [
                {"name": "Jef Nice", "picture_xl": PLACEHOLDER},
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
