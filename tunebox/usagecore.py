"""The pure parts of usage statistics: constants, the short user agent, the
device cookie, local days and one day's totals. usagelive.py keeps the
live state, usage.py the writer and usagedb.py the SQL."""

from __future__ import annotations

import re
import secrets
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from tunebox.pin import read_cookie

DEVICE_COOKIE = "tunebox_device"
COOKIE_MAX_AGE = 31_536_000
IDLE_S = 60.0
# The player's poll interval (POLL_MS in web/js/api.js): one heard poll is
# this much listening.
POLL_S = 10.0
FLUSH_S = 60.0
MAX_LIVE = 1_000
MAX_PENDING = 10_000
DAILY_CAP = 10_000
RETENTION_DEFAULT = 365
RETENTION_MIN = 30
RETENTION_MAX = 3_650
COMMAND_WAIT_S = 5.0
RETRY_S = 60.0
NAME_MAX = 40
PAGE = 50
AGENT_READ = 512

_DEVICE = re.compile(r"[0-9a-f]{32}")
# Order matters: Edge and Samsung Internet also say Chrome, Chrome also says Safari.
_BROWSERS = (
    ("Edge", re.compile(r"\bEdg(?:e|A|iOS)?/(\d+)")),
    ("Samsung Internet", re.compile(r"\bSamsungBrowser/(\d+)")),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"\b(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"\bVersion/(\d+)[.\d]*.*\bSafari/")),
)
_SYSTEMS = (
    ("Android", re.compile(r"\bAndroid\b")),
    ("iOS", re.compile(r"\b(?:iPhone|iPad|iPod)\b")),
    ("ChromeOS", re.compile(r"\bCrOS\b")),
    ("Windows", re.compile(r"\bWindows\b")),
    ("macOS", re.compile(r"\bMac OS X\b|\bMacintosh\b")),
    ("Linux", re.compile(r"\bLinux\b")),
)


def short_agent(header: str | None) -> str:
    """ "Browser N, System" from a fixed vocabulary, so the set of values a
    client can produce stays small. N is left out unless it is 1 to 999."""
    text = (header or "")[:AGENT_READ]
    browser = "Browser"
    for name, pattern in _BROWSERS:
        found = pattern.search(text)
        if found:
            version = int(found.group(1)[:4])
            browser = f"{name} {version}" if 1 <= version <= 999 else name
            break
    system = next((name for name, pattern in _SYSTEMS if pattern.search(text)), "Other")
    return f"{browser}, {system}"


def valid_device(value: str | None) -> bool:
    return value is not None and _DEVICE.fullmatch(value) is not None


def new_device() -> str:
    return secrets.token_hex(16)


def device_cookie(cookie_header: str | None, secure: bool) -> tuple[str, str]:
    """The device id for a player page answer and its Set-Cookie value. Sent
    on every answer, the same id again when the request had a valid one, so
    a device used once a year never expires and no per-device state is kept."""
    found = read_cookie(cookie_header, DEVICE_COOKIE)
    device = found if found is not None and valid_device(found) else new_device()
    flag = " Secure;" if secure else ""
    return device, (
        f"{DEVICE_COOKIE}={device}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly;{flag} SameSite=Lax"
    )


def day_of(ts: float, zone: tzinfo) -> str:
    return datetime.fromtimestamp(ts, zone).date().isoformat()


def day_bounds(day: str, zone: tzinfo) -> tuple[float, float]:
    """Local midnight to the next local midnight: 23 or 25 hours on the
    days the clocks change."""
    first = date.fromisoformat(day)
    start = datetime(first.year, first.month, first.day, tzinfo=zone)
    following = first + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone)
    return start.timestamp(), end.timestamp()


def interval_end(started_at: float, last_seen: float) -> float:
    return max(last_seen, started_at + POLL_S)


@dataclass(frozen=True)
class Interval:
    start: float
    end: float
    channel: str
    stream: str
    device: str | None
    agent: str


@dataclass
class DayTotals:
    seconds_channel: dict[str, float] = field(default_factory=dict[str, float])
    seconds_stream: dict[str, float] = field(default_factory=dict[str, float])
    seconds_device: dict[str, float] = field(default_factory=dict[str, float])
    seconds_agent: dict[str, float] = field(default_factory=dict[str, float])
    visits: int = 0
    peak: int = 0


def aggregate(
    intervals: Iterable[Interval], visits: Iterable[float], start: float, end: float
) -> DayTotals:
    """One day's totals from listening intervals and visit times, each
    clipped to [start, end). The peak sweeps interval starts and ends; at
    one instant an end counts before a start, so touching sessions are one
    listener. Widths are taken as given: callers widen a single poll with
    interval_end, or it would count nothing."""
    sums: dict[str, defaultdict[str, float]] = {
        name: defaultdict(float) for name in ("channel", "stream", "device", "agent")
    }
    points: list[tuple[float, int]] = []
    for interval in intervals:
        lo, hi = max(interval.start, start), min(interval.end, end)
        if hi <= lo:
            continue
        seconds = hi - lo
        sums["channel"][interval.channel] += seconds
        sums["stream"][f"{interval.channel}/{interval.stream}"] += seconds
        sums["device"][interval.device or "-"] += seconds
        sums["agent"][interval.agent] += seconds
        points += [(lo, 1), (hi, -1)]
    peak = current = 0
    for _, step in sorted(points):
        current += step
        peak = max(peak, current)
    return DayTotals(
        dict(sums["channel"]),
        dict(sums["stream"]),
        dict(sums["device"]),
        dict(sums["agent"]),
        sum(1 for at in visits if start <= at < end),
        peak,
    )
