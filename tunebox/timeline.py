"""Which entry is heard now, which comes next, and which were heard before."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from tunebox.tracks import Track


@dataclass(frozen=True)
class Timeline:
    audible: Track | None
    upcoming: Track | None
    upcoming_in_s: int | None
    not_heard: tuple[Track, ...]
    history: tuple[Track, ...]

    @property
    def current(self) -> Track | None:
        """None during a station ID; the player shows the channel name then."""
        if self.audible is None or self.audible.kind != "track":
            return None
        return self.audible


def build(recent: Sequence[Track], now_ms: int, delay_s: float) -> Timeline:
    delay_ms = round(delay_s * 1000)
    not_heard = tuple(t for t in recent if t.started_at + delay_ms > now_ms)
    heard = [t for t in recent if t.started_at + delay_ms <= now_ms]
    audible = heard[0] if heard else None
    upcoming = not_heard[-1] if not_heard else None
    upcoming_in_s = (
        math.ceil((upcoming.started_at + delay_ms - now_ms) / 1000) if upcoming else None
    )
    history = tuple(t for t in heard[1:] if t.kind == "track")
    return Timeline(audible, upcoming, upcoming_in_s, not_heard, history)
