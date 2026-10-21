import subprocess
import time
from pathlib import Path

import pytest

from tunebox.config import PATTERN_RULES, ChannelRow, ConfigState, load
from tunebox.rulecheck import (
    SYNTHETIC,
    check,
    lint,
    lint_new,
    patterns,
    refused,
    samples_for,
    stored_refusals,
)
from tunebox.tracks import MAX_TITLE

STATION_ID = r"^[A-Z]\d{4} \d{2} [A-Za-z]{3} \d{4}\b"
PLAYED_BY = r"\s*\(Played by ([^)]*)\)\s*$"
EXAMPLES = Path(__file__).parent.parent / "examples" / "stations-nl.toml"
CHAIN = "repeats that can match the same characters"


@pytest.mark.parametrize(
    "pattern",
    [
        STATION_ID,
        PLAYED_BY,
        r"\(feat\. [^)]+\)",
        "^ID$",
        r"(?:ab){2}",
        r"a{2,3}",
        "\\s*[-\u2013]\\s*(.+)$",
        r"^(.*?) - (.*)$",
        r"\s*\((?:feat|ft)\.? ([^)]*)\)\s*$",
        r"\s*-\s*",
    ],
)
def test_patterns_in_use_pass_the_lint(pattern: str) -> None:
    assert lint(pattern) is None


def test_every_example_rule_passes_the_lint() -> None:
    rules = [c.rules for c in load(EXAMPLES).channels]
    patterns = [p.pattern for r in rules for p in (r.station_id, r.played_by) if p is not None]
    assert patterns
    assert [p for p in patterns if lint(p) is not None] == []


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("(a+)+$", "nested repetition"),
        ("(a*)*b", "nested repetition"),
        ("(a|aa)+$", "nested repetition"),
        (r"(\w+\s?)*$", "nested repetition"),
        ("(?:a+){2,}", "nested repetition"),
        ("((a+))+$", "nested repetition"),
        (r"((\d+))+$", "nested repetition"),
        ("((A+))+B", "nested repetition"),
        (r"(?:(\w+)\s?)*$", "nested repetition"),
        ("(?:x|(?:a|aa))+", "nested repetition"),
        ("(?:-?){40}-{40}!", "nested repetition"),
        ("z*z*z*z*q", CHAIN),
        (r"(.*)-(.*)-(.*)-(.*)\[mix\]", CHAIN),
        ("[-\u2013]+\\s*[-\u2013]+\\s*[-\u2013]+\\s*mixed by (.+)", CHAIN),
        ("-*-*-*-*-*$x", CHAIN),
        (r"\.*\.*\.*\.*:", CHAIN),
        ("^.*-.*-.*-.*:$", CHAIN),
        (r"\s*\(?\s*\s*Played by (.*)\)", CHAIN),
        ("(?i)a*A*a*q", CHAIN),
        ("(?=.*-.*-.*q)", CHAIN),
        ("(?:x|.*)-.*-.*q", CHAIN),
        (r"^\d*\d*\d*\d*\d*$", CHAIN),
        ("^[A-Z]*[A-Z]*[A-Z]*[A-Z]*[A-Z]*$", CHAIN),
        ("\u0436*\u0436*\u0436*\u0436*q", CHAIN),
        ("[\u0430-\u044f]*[\u0430-\u044f]*[\u0430-\u044f]*q", CHAIN),
        ("[\u0430-\u043c]*[\u0438-\u044f]*\u043c*q", CHAIN),
        ("(?i)\u0416*\u0436*\u0416*q", CHAIN),
        ("[^\\x00-\\U0001ffff]*[^\\x00-\\U0001ffff]*[^\\x00-\\U0001ffff]*q", CHAIN),
        ("[^\\x00-\\u3000]*[\\u3001-\\u3005]*[^\\x00-\\u3000]*q", CHAIN),
        (r"\d*[\u0600-\u06ff]*[\u0600-\u06ff]*q", CHAIN),
        (r"\w*[\u2000-\u2fff]*[\u2000-\u2fff]*q", CHAIN),
        (r"\s*[\u1000-\u1fff]*[\u1000-\u1fff]*q", CHAIN),
        (r"[^\W\d]*[\u2000-\u2fff]*[\u2000-\u2fff]*q", CHAIN),
        (r"[^\W\d]*[\u0660-\u06e9]*[\u0660-\u06e9]*q", CHAIN),
        (r"(?i)[a-z]*[\u2120-\u212f]*[\u2120-\u212f]*q", CHAIN),
        (r"(x)\1", "back-references"),
        ("(?P<n>x)(?P=n)", "back-references"),
        ("(x)?(?(1)y|z)", "back-references"),
        ("a" * 201, "200 characters"),
        ("(", "not a valid pattern"),
    ],
)
def test_dangerous_patterns_are_refused_with_a_reason(pattern: str, reason: str) -> None:
    found = lint(pattern)
    assert found is not None
    assert reason in found


def test_good_patterns_pass_the_check() -> None:
    assert check([STATION_ID, PLAYED_BY], ["FISHER - Stay", "Title (Played by Someone)"]) is None


def test_a_slow_pattern_is_refused_in_time_and_named() -> None:
    started = time.monotonic()
    found = check([PLAYED_BY, r"^(a|a?)+$"], ["a" * 40 + "!"])
    assert found is not None
    index, reason = found
    assert index == 1
    assert "too slow" in reason
    assert time.monotonic() - started < 4


def test_every_search_has_its_own_budget() -> None:
    found = check(["z*z*q"], [], per_search_s=1e-5)
    assert found is not None
    assert found[0] == 0
    assert "too slow" in found[1]


def test_a_pattern_that_does_not_compile_is_refused() -> None:
    found = check([STATION_ID, "("], [])
    assert found is not None
    assert found[0] == 1
    assert "missing" in found[1]


def test_a_child_that_cannot_start_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "run", broken)
    found = check([STATION_ID], [])
    assert found is not None
    assert found[0] is None
    assert "could not run" in found[1]


def test_samples_are_made_of_the_patterns_own_characters() -> None:
    samples = samples_for("x[-\u2013]+y")
    for run in ("-" * MAX_TITLE, "\u2013" * MAX_TITLE, "x" * MAX_TITLE):
        assert run in samples


def test_no_sample_is_longer_than_a_title() -> None:
    every = [*SYNTHETIC, *samples_for(PLAYED_BY), *samples_for(STATION_ID)]
    assert max(len(sample) for sample in every) == MAX_TITLE


def test_samples_include_the_characters_a_range_names() -> None:
    samples = samples_for("q[\u0430-\u044f]+q")
    assert "\u0430" * MAX_TITLE in samples


def state_with(*patterns: str) -> ConfigState:
    rows = tuple(
        ChannelRow(
            f"s{n}",
            None,
            True,
            {
                "id": f"s{n}",
                "name": "S",
                "stream_url": "https://example.invalid/stream.aac",
                "source": {"type": "triton", "mount": "X"},
                "rules": {"played_by": p},
            },
        )
        for n, p in enumerate(patterns)
    )
    return ConfigState({}, {}, (), rows)


def test_refused_skips_stored_patterns_and_names_the_field() -> None:
    stored = state_with("(a+)+$")  # refused by the lint, but already stored
    candidate = state_with("(a+)+$", "(b+)+$")
    reason = refused(candidate, stored, list)
    assert reason is not None and reason.startswith("channels[1].rules.played_by: ")


def test_refused_lets_a_state_of_stored_patterns_through() -> None:
    stored = state_with("(a+)+$")
    assert refused(stored, stored, list) is None


def test_stored_refusals_lists_what_the_lint_now_refuses() -> None:
    found = stored_refusals(state_with("(x)", "(a+)+$"))
    assert [field for field, _ in found] == ["channels[1].rules.played_by"]


def test_lint_new_skips_the_patterns_the_store_holds() -> None:
    stored = state_with("(x)", "(a+)+$")
    assert lint_new(stored, stored) is None
    reason = lint_new(state_with("(x)", "(a+)+$"), state_with("(x)"))
    assert reason == "channels[1].rules.played_by: " + str(lint("(a+)+$"))
    assert lint_new(state_with("(x)", "(y)"), state_with()) is None


def test_patterns_finds_every_key_config_compiles() -> None:
    doc = {"rules": {key: "(x)" for key in PATTERN_RULES}}
    state = ConfigState({}, {}, (), (ChannelRow("a", None, True, doc),))
    assert {field.rsplit(".", 1)[1] for field, _ in patterns(state)} == set(PATTERN_RULES)
