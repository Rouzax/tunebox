"""Station health: whether each enabled station's streams and feed work.
The rules here are pure; HealthChecks runs the checks and keeps their
latest results in the cover store."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

from tunebox.channel import Liveness
from tunebox.config import ChannelConfig, SourceConfig
from tunebox.store import HealthRow

ITEM_PAUSE_S = 3.0
BUTTON_GAP_S = 300.0
# A poller failing this long is broken; a shorter spell is a hiccup it rides out.
LIVE_BROKEN_S = 600
# No new song for this long while polls succeed: the feed is most likely stuck.
STUCK_S = 7200
MAX_HEALTH_ITEMS = 300
START_DELAY_S = 300.0
STARTUP_STALE_S = 86_400.0
# A run started this recently makes a restart skip the start-up run, so a
# crash loop never checks every station on each start.
STARTUP_QUIET_S = 21_600.0
NETWORK_RETRY_S = 1800.0
NETWORK_RETRIES = 3
WAIT_S = 60.0
# Before the morning drive; Dutch clock changes happen at 02:00 and 03:00,
# so this time exists exactly once every day.
DAILY_AT = time(4, 30)

Status = Literal["ok", "warning", "broken"]
Trigger = Literal["daily", "startup", "button", "retry"]


@dataclass(frozen=True)
class Reason:
    code: str
    item: str | None


@dataclass(frozen=True)
class Judgement:
    status: Status
    reasons: tuple[Reason, ...]


def fingerprint(source: SourceConfig) -> str:
    """The feed a result belongs to: a changed setting is another feed."""
    text = "\n".join([source.type, *(f"{k}={v}" for k, v in sorted(source.settings.items()))])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def targets(config: ChannelConfig) -> list[tuple[str, str]]:
    return [(f"stream:{s.name}", s.url) for s in config.streams] + [
        ("feed", fingerprint(config.source))
    ]


def _daily_after(now: float, zone: tzinfo) -> float:
    """The next DAILY_AT strictly after now: the loop keeps the time it
    planned and runs once the wall clock passes it, so a wake at 04:30:00.5
    still runs that day's check."""
    day = datetime.fromtimestamp(now, zone).date()
    today = datetime.combine(day, DAILY_AT, zone).timestamp()
    if today > now:
        return today
    return datetime.combine(day + timedelta(days=1), DAILY_AT, zone).timestamp()


def next_run(
    now: float,
    last_started: float | None,
    last_finished: float | None,
    process_start: float,
    zone: tzinfo,
    start_delay_s: float = START_DELAY_S,
) -> tuple[float, Literal["daily", "startup"]]:
    stale = last_finished is None or now - last_finished > STARTUP_STALE_S
    quiet = last_started is None or now - last_started > STARTUP_QUIET_S
    if stale and quiet:
        return max(now, process_start + start_delay_s), "startup"
    return _daily_after(now, zone), "daily"


def judge(
    config: ChannelConfig, rows: Mapping[str, HealthRow], live: Liveness, now_ms: int
) -> Judgement:
    """The one status rule. rows are this station's, by item; a row checked
    against another address or feed than the current one is not checked
    yet, never a failure."""
    broken: list[Reason] = []
    warning: list[Reason] = []
    info: list[Reason] = []
    current: dict[str, HealthRow] = {}
    for item, target in targets(config):
        row = rows.get(item)
        if row is None or row.target != target:
            info.append(Reason("not_checked", item))
        else:
            current[item] = row
    streams = [item for item, _ in targets(config) if item != "feed"]
    failing = [item for item in streams if item in current and not current[item].ok]
    if failing and len(failing) == len(streams):
        broken.append(Reason("streams_failing", None))
    else:
        warning.extend(Reason("stream_failing", item) for item in failing)
    for item in streams:
        row = current.get(item)
        if row is not None and row.ok and row.reason == "plain_http":
            warning.append(Reason("plain_http", item))
    feed = current.get("feed")
    if feed is not None and not feed.ok:
        if live.last_poll_ok is not None and live.last_poll_ok > feed.checked_at:
            warning.append(Reason("feed_check_failed_polls_ok", "feed"))
        else:
            broken.append(Reason("feed_failing", "feed"))
    elif feed is not None and feed.reason == "no_songs":
        warning.append(Reason("no_songs", "feed"))
    if live.active and live.stale_since is not None:
        if now_ms - live.stale_since > LIVE_BROKEN_S * 1000:
            broken.append(Reason("poll_failing", None))
    elif (
        live.active
        and live.polled_since_wake
        and live.last_change is not None
        and now_ms - live.last_change > STUCK_S * 1000
    ):
        warning.append(Reason("stuck", None))
    if broken:
        return Judgement("broken", tuple(broken))
    if warning:
        return Judgement("warning", tuple(warning))
    return Judgement("ok", tuple(info))
