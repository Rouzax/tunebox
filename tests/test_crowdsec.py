import json
import logging
import re
from pathlib import Path
from typing import get_args

import pytest

from tunebox.accounts import Reason
from tunebox.logs import JsonFormatter, event

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "crowdsec"


def emitted_events() -> set[str]:
    names: set[str] = set()
    for source in (ROOT / "tunebox").glob("*.py"):
        text = source.read_text(encoding="utf-8")
        names |= set(re.findall(r'"((?:admin|listener)_[a-z_]+)"', text))
    return names


def test_scenarios_name_only_events_tunebox_emits() -> None:
    emitted = emitted_events()
    scenarios = list((EXAMPLES / "scenarios").glob("*.yaml"))
    assert scenarios
    for scenario in scenarios:
        text = scenario.read_text(encoding="utf-8")
        named = set(re.findall(r"tunebox_((?:admin|listener)_[a-z_]+)", text))
        assert named, scenario.name
        assert named <= emitted, (scenario.name, named - emitted)


def test_sample_lines_have_the_shape_tunebox_writes(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("tunebox.admin")
    with caplog.at_level(logging.INFO, logger="tunebox.admin"):
        event(log, logging.WARNING, "admin_login_failed", client="203.0.113.9", reason="totp")
    written = json.loads(JsonFormatter().format(caplog.records[0]))
    assert set(written) >= {"ts", "level", "logger", "event", "client", "reason"}
    for line in (EXAMPLES / "sample.log").read_text(encoding="utf-8").splitlines():
        sample = json.loads(line)
        assert set(sample) >= {"ts", "level", "logger", "event", "client"}
        assert sample["event"] in emitted_events()


def test_the_ban_scenarios_count_only_attack_signals() -> None:
    bf = (EXAMPLES / "scenarios" / "tunebox-admin-bf.yaml").read_text(encoding="utf-8")
    # Any page can make a visitor's browser send a refused cross-site POST.
    assert "admin_csrf_rejected" not in bf
    # Pace and busy refusals are someone else's flood, not this address's.
    assert "evt.Meta.why == 'address'" in bf
    parser = (EXAMPLES / "parsers" / "s01-parse" / "tunebox-logs.yaml").read_text(encoding="utf-8")
    assert "meta: why" in parser


def test_scenario_reasons_are_reasons_tunebox_gives() -> None:
    allowed = set(get_args(Reason))
    for scenario in (EXAMPLES / "scenarios").glob("*.yaml"):
        text = scenario.read_text(encoding="utf-8")
        assert set(re.findall(r"evt\.Meta\.reason == '([a-z]+)'", text)) <= allowed
    totp = (EXAMPLES / "scenarios" / "tunebox-admin-totp-bf.yaml").read_text(encoding="utf-8")
    assert "'recovery'" in totp
