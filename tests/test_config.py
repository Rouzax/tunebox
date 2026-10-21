import re
from pathlib import Path
from typing import Any

import pytest

from tunebox import config
from tunebox.config import (
    AdminConfig,
    ChannelRow,
    ConfigError,
    ConfigState,
    PlayerConfig,
    StreamConfig,
    load,
    parse,
    state_from_toml,
    validate_state,
)

EXAMPLE = Path(__file__).parent.parent / "tunebox.example.toml"


def minimal(**channel: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "owr",
        "name": "One World Radio",
        "stream_url": "https://example.invalid/stream.aac",
        "source": {"type": "triton", "mount": "OWR_INTERNATIONAL"},
    }
    return {"channels": [{**base, **channel}]}


def test_example_config_loads() -> None:
    config = load(EXAMPLE)
    assert [c.id for c in config.channels] == ["owr", "daybreak", "anthems"]
    owr = config.channels[0]
    assert owr.delay_s == 7
    assert owr.pawtunes_delay_s == 28
    assert owr.source.settings == {"mount": "OWR_INTERNATIONAL"}
    assert owr.rules.station_id is not None
    assert [s.name for s in owr.streams] == ["High", "Standard", "Compatible"]
    assert [s.name for s in config.channels[2].streams] == ["Standard", "Compatible"]
    assert all(c.always and c.group == "Tomorrowland" for c in config.channels)
    assert config.player == PlayerConfig()
    assert config.server.port == 8080
    assert config.server.data_dir == EXAMPLE.parent


def test_defaults_without_rules_are_neutral(tmp_path: Path) -> None:
    channel = parse(minimal(), tmp_path).channels[0]
    assert channel.rules.station_id is None
    assert channel.rules.played_by is None
    assert channel.rules.artist_separator == ","
    assert channel.history_size == 50
    assert channel.default_image is None


def test_channel_overrides_a_default(tmp_path: Path) -> None:
    data = minimal(delay_s=9.5)
    data["defaults"] = {"pawtunes_delay_s": 30}
    channel = parse(data, tmp_path).channels[0]
    assert channel.delay_s == 9.5
    assert channel.pawtunes_delay_s == 30


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (minimal(id="Bad Id"), "channels[0].id"),
        (minimal(stream_url="ftp://x"), "channels[0].stream_url"),
        (minimal(source={"type": "icecast"}), "channels[0].source.type"),
        (minimal(source={"type": "triton"}), "channels[0].source.mount"),
        (minimal(rules={"station_id": "("}), "channels[0].rules.station_id"),
        (minimal(rules={"played_by": "Played by"}), "channels[0].rules.played_by"),
        (minimal(rules={"artist_separator": ""}), "channels[0].rules.artist_separator"),
        (minimal(history_size=0), "channels[0].history_size"),
        (minimal(delay_s="7"), "channels[0].delay_s"),
        (minimal(colour="red"), "channels[0].colour: unknown key"),
        ({**minimal(), "server": {"port": 70000}}, "server.port"),
    ],
)
def test_invalid_config_names_the_key(data: dict[str, Any], message: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse(data, tmp_path)


def test_duplicate_channel_ids(tmp_path: Path) -> None:
    data = minimal()
    data["channels"].append(dict(data["channels"][0]))
    with pytest.raises(ConfigError, match="duplicate"):
        parse(data, tmp_path)


def test_missing_or_broken_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing\.toml"):
        load(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("[server\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"broken\.toml"):
        load(broken)


def test_stream_url_becomes_one_default_stream(tmp_path: Path) -> None:
    channel = parse(minimal(), tmp_path).channels[0]
    assert channel.streams == (
        StreamConfig("Default", "https://example.invalid/stream.aac", None, None),
    )
    assert channel.stream_url == "https://example.invalid/stream.aac"


def streams_channel(*streams: object) -> dict[str, Any]:
    data = minimal()
    del data["channels"][0]["stream_url"]
    data["channels"][0]["streams"] = list(streams)
    return data


def test_streams_keep_their_order(tmp_path: Path) -> None:
    data = streams_channel(
        {"name": "High", "url": "https://example.invalid/a.aac", "codec": "aac", "kbps": 256},
        {"name": "Compatible", "url": "https://example.invalid/a.mp3", "codec": "mp3"},
    )
    channel = parse(data, tmp_path).channels[0]
    assert channel.streams == (
        StreamConfig("High", "https://example.invalid/a.aac", "aac", 256),
        StreamConfig("Compatible", "https://example.invalid/a.mp3", "mp3", None),
    )
    assert channel.stream_url == "https://example.invalid/a.aac"


GOOD_STREAM = {"name": "High", "url": "https://example.invalid/a.aac"}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"channels": [{**minimal()["channels"][0], "streams": [GOOD_STREAM]}]}, "not both"),
        (streams_channel(), "channels[0].streams"),
        (
            {
                "channels": [
                    {k: v for k, v in minimal()["channels"][0].items() if k != "stream_url"}
                ]
            },
            "channels[0].streams: required",
        ),
        (streams_channel("x"), "channels[0].streams[0]: expected a table"),
        (
            streams_channel({**GOOD_STREAM, "url": "http://example.invalid/a"}),
            "channels[0].streams[0].url",
        ),
        (streams_channel(GOOD_STREAM, GOOD_STREAM), "channels[0].streams[1].name: duplicate"),
        (streams_channel({**GOOD_STREAM, "name": ""}), "channels[0].streams[0].name"),
        (streams_channel({**GOOD_STREAM, "codec": "flac"}), "channels[0].streams[0].codec"),
        (streams_channel({**GOOD_STREAM, "kbps": 0}), "channels[0].streams[0].kbps"),
        (streams_channel({**GOOD_STREAM, "kbps": True}), "channels[0].streams[0].kbps"),
        (streams_channel({**GOOD_STREAM, "bitrate": 1}), "channels[0].streams[0].bitrate"),
        (minimal(logo="http://example.invalid/x.png"), "channels[0].logo"),
        (minimal(group=""), "channels[0].group"),
        (minimal(always="yes"), "channels[0].always"),
        ({**minimal(), "player": {"latitude": 52.0}}, "player: give latitude and longitude"),
        ({**minimal(), "player": {"latitude": 91, "longitude": 4}}, "player.latitude"),
        ({**minimal(), "player": {"latitude": 52, "longitude": "4"}}, "player.longitude"),
        ({**minimal(), "player": {"default_channel": "nope"}}, "player.default_channel"),
        ({**minimal(), "player": {"zoom": 2}}, "player.zoom: unknown key"),
    ],
)
def test_invalid_new_keys_name_the_key(data: dict[str, Any], message: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse(data, tmp_path)


def test_group_logo_and_always(tmp_path: Path) -> None:
    plain = parse(minimal(), tmp_path).channels[0]
    assert (plain.group, plain.logo, plain.always) == (None, None, False)
    data = minimal(group="Tomorrowland", logo="https://example.invalid/l.png", always=True)
    channel = parse(data, tmp_path).channels[0]
    assert (channel.group, channel.logo, channel.always) == (
        "Tomorrowland",
        "https://example.invalid/l.png",
        True,
    )


def test_player_settings(tmp_path: Path) -> None:
    assert parse(minimal(), tmp_path).player == PlayerConfig()
    data = {**minimal(), "player": {"latitude": 52, "longitude": 4.5, "default_channel": "owr"}}
    assert parse(data, tmp_path).player == PlayerConfig(52.0, 4.5, "owr")


STATIONS = Path(__file__).parent.parent / "examples" / "stations-nl.toml"


def test_example_station_list_loads() -> None:
    config = load(STATIONS)
    assert len(config.channels) == 50
    groups = [c.group for c in config.channels]
    assert groups.count("Tomorrowland") == 3
    assert {c.source.type for c in config.channels} == {"triton", "qmusic", "npo"}
    assert all(
        c.streams and all(s.url.startswith("https://") for s in c.streams) for c in config.channels
    )
    assert [c.id for c in config.channels if c.always] == ["owr", "daybreak", "anthems"]
    text = STATIONS.read_text(encoding="utf-8")
    # Copied from an LMS favorites list: none of its TuneIn tracking parameters.
    assert "serial=" not in text and "partnerId" not in text


def test_a_file_without_channels_loads(tmp_path: Path) -> None:
    empty: list[dict[str, Any]] = [{"server": {}}, {"channels": []}]
    for data in empty:
        config = parse(data, tmp_path)
        assert config.channels == ()
        assert config.state.channels == ()


def test_state_from_toml_keeps_order_and_groups() -> None:
    data = minimal()
    first = dict(data["channels"][0], group="Tomorrowland")
    second = dict(data["channels"][0], id="daybreak", group="Tomorrowland")
    third = dict(data["channels"][0], id="solo")
    data["channels"] = [first, second, third]
    state = state_from_toml(data)
    assert state.groups == ("Tomorrowland",)
    assert [(c.id, c.group, c.enabled) for c in state.channels] == [
        ("owr", "Tomorrowland", True),
        ("daybreak", "Tomorrowland", True),
        ("solo", None, True),
    ]
    assert "group" not in state.channels[0].doc
    # The player lists ungrouped channels first; the listing order matches.
    assert [c.id for c in validate_state(state).channels] == ["solo", "owr", "daybreak"]


def test_disabled_channels_are_left_out_but_still_checked() -> None:
    row = state_from_toml(minimal()).channels[0]
    off = ConfigState(channels=(ChannelRow(row.id, None, False, row.doc),))
    assert validate_state(off).channels == ()
    broken = ConfigState(channels=(ChannelRow(row.id, None, False, {**row.doc, "name": 5}),))
    with pytest.raises(ConfigError, match=re.escape("channels[0].name")):
        validate_state(broken)


def test_default_channel_must_be_enabled() -> None:
    state = state_from_toml({**minimal(), "player": {"default_channel": "owr"}})
    row = state.channels[0]
    off = ConfigState(player=state.player, channels=(ChannelRow(row.id, None, False, row.doc),))
    with pytest.raises(ConfigError, match=re.escape("player.default_channel")):
        validate_state(off)


def test_a_row_must_name_a_known_group_and_match_its_id() -> None:
    row = state_from_toml(minimal()).channels[0]
    with pytest.raises(ConfigError, match=re.escape("channels[0].group")):
        validate_state(ConfigState(channels=(ChannelRow(row.id, "Nowhere", True, row.doc),)))
    with pytest.raises(ConfigError, match=re.escape("channels[0].id")):
        validate_state(ConfigState(channels=(ChannelRow("other", None, True, row.doc),)))


def test_group_names_are_unique_and_not_empty() -> None:
    with pytest.raises(ConfigError, match=re.escape("groups[1]")):
        validate_state(ConfigState(groups=("A", "A")))
    with pytest.raises(ConfigError, match=re.escape("groups[0]")):
        validate_state(ConfigState(groups=(" ",)))


def test_admin_is_off_by_default(tmp_path: Path) -> None:
    config = parse(minimal(), tmp_path)
    assert config.admin == AdminConfig()
    assert config.server.trusted_proxies == ()


def test_admin_settings_and_trusted_proxies(tmp_path: Path) -> None:
    data = {
        **minimal(),
        "server": {"trusted_proxies": ["172.18.0.0/16", "::1"]},
        "admin": {"enabled": True, "setup_page": True, "public_origin": "https://radio.example"},
    }
    config = parse(data, tmp_path)
    assert config.admin == AdminConfig(True, True, "https://radio.example")
    assert [str(n) for n in config.server.trusted_proxies] == ["172.18.0.0/16", "::1/128"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8083",
        "http://127.0.0.1:18086",
        "https://radio.example",
        "http://tunebox.home.arpa:8083",
        "http://192.168.1.10:8083",
    ],
)
def test_public_origin_may_be_https_or_plain_http(origin: str, tmp_path: Path) -> None:
    data = {**minimal(), "admin": {"enabled": True, "public_origin": origin}}
    assert parse(data, tmp_path).admin.public_origin == origin


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("HTTP://Tunebox.Home.Arpa:80", "http://tunebox.home.arpa"),
        ("https://Radio.Example:443", "https://radio.example"),
        ("https://radio.example:8443", "https://radio.example:8443"),
        ("http://[FD00::1]:8083", "http://[fd00::1]:8083"),
    ],
)
def test_public_origin_is_stored_as_a_browser_sends_it(
    given: str, stored: str, tmp_path: Path
) -> None:
    data = {**minimal(), "admin": {"enabled": True, "public_origin": given}}
    admin = parse(data, tmp_path).admin
    assert admin.public_origin == stored
    assert admin.plain_http is stored.startswith("http:")


@pytest.mark.parametrize(
    ("admin", "message"),
    [
        ({"enabled": True}, "admin.public_origin: required"),
        ({"enabled": True, "public_origin": "ftp://radio.example"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "http://"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "http://x:99999"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "http://u@x"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "https://radio.example/path"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "http://192.0.2.1?x"}, "admin.public_origin"),
        ({"enabled": True, "public_origin": "http://localhost/x"}, "admin.public_origin"),
        ({"enabled": "yes"}, "admin.enabled"),
        ({"colour": 1}, "admin.colour: unknown key"),
    ],
)
def test_bad_admin_settings_name_the_key(
    admin: dict[str, Any], message: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse({**minimal(), "admin": admin}, tmp_path)


def test_a_bad_trusted_proxy_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("server.trusted_proxies[0]")):
        parse({**minimal(), "server": {"trusted_proxies": ["not-an-address"]}}, tmp_path)
    with pytest.raises(ConfigError, match=re.escape("server.trusted_proxies")):
        parse({**minimal(), "server": {"trusted_proxies": "10.0.0.0/8"}}, tmp_path)


def test_a_group_inside_the_document_is_refused_not_a_crash() -> None:
    doc = {**minimal()["channels"][0], "group": "Pop"}
    with pytest.raises(ConfigError, match=re.escape("channels[0].group")):
        validate_state(ConfigState(channels=(ChannelRow("owr", None, True, doc),)))


def test_a_station_can_need_the_pin(tmp_path: Path) -> None:
    assert parse(minimal(needs_pin=True), tmp_path).channels[0].needs_pin is True
    assert parse(minimal(), tmp_path).channels[0].needs_pin is False
    with pytest.raises(ConfigError, match=r"channels\[0\]\.needs_pin"):
        parse(minimal(needs_pin="yes"), tmp_path)


def test_a_log_file_is_optional_and_lives_under_the_data_folder(tmp_path: Path) -> None:
    assert parse(minimal(), tmp_path).server.log_file is None
    data = {**minimal(), "server": {"data_dir": "/data", "log_file": "logs/tunebox.log"}}
    assert parse(data, tmp_path).server.log_file == Path("/data/logs/tunebox.log")
    data = {**minimal(), "server": {"log_file": "/var/log/t.log"}}
    assert parse(data, tmp_path).server.log_file == Path("/var/log/t.log")
    with pytest.raises(ConfigError, match=r"server\.log_file"):
        parse({**minimal(), "server": {"log_file": 3}}, tmp_path)
    with pytest.raises(ConfigError, match=r"server\.log_file"):
        parse({**minimal(), "server": {"log_file": ""}}, tmp_path)


def test_every_pattern_rule_is_compiled() -> None:
    rules = {key: "(x)" for key in config.PATTERN_RULES}
    compiled = config._rules(rules, "rules")  # pyright: ignore[reportPrivateUsage]
    for key in config.PATTERN_RULES:
        assert getattr(compiled, key) is not None
