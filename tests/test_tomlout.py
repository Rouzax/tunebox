import math
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from tunebox.config import state_from_toml, state_to_toml
from tunebox.tomlout import dumps

EXAMPLE = Path(__file__).parent.parent / "examples" / "stations-nl.toml"


ROUND_TRIPS: list[dict[str, Any]] = [
    {"a": "plain", "b": 'quote " and \\ back', "c": "line\nbreak\ttab", "d": "é ☃ \x7f"},
    {"ctl": "".join(chr(n) for n in range(0x20)), "uni": "😀 \u2028"},
    {"n": 7, "f": 7.5, "neg": -3, "big": 2**40, "t": True, "f2": False},
    {"tiny": 1e-300, "huge": 1.5e300, "negzero": -0.0, "third": 1 / 3},
    {"list": ["a", "b"], "nums": [1, 2.5], "empty": []},
    {"nested": [[1, 2], ["a"]]},
    {"table": {"x": 1, "inner": {"y": "z"}}, "after": {"k": "v"}},
    {"rows": [{"a": 1, "sub": [{"b": 2}]}, {"a": 2}]},
    {"weird key": 1, "dotted.key": 2, "": 3, "é": 4, 'q"uote': 5},
    {"t": {}},
    {"t": {"inner": {}, "other": 1}},
    {"rows": [{}, {"a": {}}]},
    {},
]


@pytest.mark.parametrize("data", ROUND_TRIPS)
def test_dumps_reads_back_equal(data: dict[str, Any]) -> None:
    assert tomllib.loads(dumps(data)) == data


def test_dumps_refuses_what_toml_cannot_hold() -> None:
    with pytest.raises(ValueError):
        dumps({"none": None})
    with pytest.raises(ValueError):
        dumps({"mixed": [{"a": 1}, 2]})
    with pytest.raises(ValueError):
        dumps({"nan": math.nan})
    with pytest.raises(ValueError):
        dumps({"inf": [math.inf]})
    with pytest.raises(ValueError):
        dumps({"obj": object()})
    with pytest.raises(ValueError):
        dumps({"lone": "a\ud800b"})
    with pytest.raises(ValueError):
        dumps({"\udfff": 1})
    with pytest.raises(ValueError):
        dumps({1: "x"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        dumps({"t": {2: "x"}})  # type: ignore[dict-item]


def test_the_example_stations_survive_a_round_trip() -> None:
    state = state_from_toml(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")))
    again = state_from_toml(tomllib.loads(dumps(state_to_toml(state))))
    assert again == state


def test_groups_without_stations_and_disabled_stations_survive() -> None:
    data = {
        "groups": [{"name": "Empty"}, {"name": "B"}],
        "channels": [
            {
                "id": "a",
                "name": "A",
                "group": "B",
                "enabled": False,
                "source": {"type": "qmusic", "station": "qmusic"},
                "streams": [{"url": "https://stream.example/a"}],
            },
            {
                "id": "b",
                "name": "B",
                "source": {"type": "qmusic", "station": "qmusic"},
                "streams": [{"url": "https://stream.example/b"}],
            },
        ],
    }
    state = state_from_toml(data)
    assert state.groups == ("Empty", "B")
    assert state.channels[1].group is None
    assert state.channels[0].enabled is False
    assert "enabled" not in state.channels[0].doc
    exported = state_to_toml(state)
    channels = cast("list[dict[str, object]]", exported["channels"])
    assert next(iter(channels[0])) == "id"
    assert "group" not in channels[1]
    assert state_from_toml(tomllib.loads(dumps(exported))) == state
