import io
import json
import logging

import pytest

from tunebox.logs import bound, configure, event


def lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_event_is_one_json_line_with_its_fields() -> None:
    buffer = io.StringIO()
    configure("info", buffer)
    event(logging.getLogger("tunebox.test"), logging.INFO, "started", port=8080)
    [entry] = lines(buffer)
    assert entry["event"] == "started"
    assert entry["port"] == 8080
    assert entry["level"] == "info"
    assert entry["logger"] == "tunebox.test"
    assert str(entry["ts"]).endswith("Z")


def test_bound_context_id_is_on_every_line() -> None:
    buffer = io.StringIO()
    configure("info", buffer)
    with bound("req") as ctx:
        event(logging.getLogger("tunebox.test"), logging.INFO, "one")
    event(logging.getLogger("tunebox.test"), logging.INFO, "two")
    first, second = lines(buffer)
    assert ctx.startswith("req-")
    assert first["ctx"] == ctx
    assert "ctx" not in second


def test_per_logger_levels() -> None:
    configure("warning,tunebox.covers=debug", io.StringIO())
    assert logging.getLogger("tunebox.covers").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("tunebox.channel").isEnabledFor(logging.INFO)


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="loud"):
        configure("loud", io.StringIO())


def test_admin_crash_logs_no_input() -> None:
    buffer = io.StringIO()
    configure("info", buffer)
    log = logging.getLogger("tunebox.test")
    with bound("admin"):
        try:
            int("secret-pin-1234")
        except ValueError:
            log.error("request_crashed", exc_info=True)
    with bound("req"):
        try:
            int("visible-value")
        except ValueError:
            log.error("request_crashed", exc_info=True)
    out = buffer.getvalue()
    assert "secret-pin-1234" not in out
    assert "ValueError (details withheld" in out
    assert "visible-value" in out


def test_pin_requests_withhold_exception_text() -> None:
    buffer = io.StringIO()
    configure("info", buffer)
    log = logging.getLogger("tunebox.test")
    with bound("pin"):
        try:
            int("secret-pin-1234")
        except ValueError:
            log.error("request_crashed", exc_info=True)
    out = buffer.getvalue()
    assert "secret-pin-1234" not in out
    assert "ValueError (details withheld" in out
