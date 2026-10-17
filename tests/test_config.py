import re
from pathlib import Path
from typing import Any

import pytest

from tunebox.config import ConfigError, load, parse

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
        ({}, "channels: required"),
        ({"channels": []}, "channels: required"),
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
