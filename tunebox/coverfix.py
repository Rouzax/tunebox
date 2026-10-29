"""Cover fixes: a cover or an artist's photo the owner chose in the panel,
which wins over the automatic decision and is never searched again. The
table lives in the settings database; CoverFixes holds it whole in memory,
so the /now path and the cover worker never read the database."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import astuple, dataclass
from types import MappingProxyType
from typing import Literal, Protocol, cast

from tunebox.logs import event
from tunebox.settings import Settings, SettingsUnavailable

log = logging.getLogger(__name__)

FixSource = Literal["deezer", "itunes", "link", "upload", "none"]

_Row = tuple[str, str | None, str, FixSource, str, str, float]


@dataclass(frozen=True)
class Fix:
    """key: a song key, or a photo key (photo:<artists>). value: an https
    catalogue image link, blob:<64 hex> for an upload, or None for "no
    photo" (photo keys only). artist and title are the song's text when the
    fix was set, for the panel only: nothing builds a Track from them."""

    key: str
    value: str | None
    label: str
    source: FixSource
    artist: str
    title: str
    set_at: float


class FixReader(Protocol):
    def get(self, key: str) -> Fix | None: ...

    def values(self) -> set[str] | None: ...


class NoFixes:
    """The reader of a cover service built without fixes."""

    def get(self, key: str) -> Fix | None:
        return None

    def values(self) -> set[str] | None:
        return set()


def _read(db: sqlite3.Connection) -> Mapping[str, Fix]:
    rows = cast(
        "list[_Row]",
        db.execute(
            "SELECT key, value, label, source, artist, title, set_at FROM cover_fixes"
        ).fetchall(),
    )
    return MappingProxyType({row[0]: Fix(*row) for row in rows})


class CoverFixes:
    """Readers take the current map without a lock: it is replaced whole,
    never changed in place. Writers are serialised, so the map left last is
    the one the last commit read."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._write = threading.Lock()
        self._map: Mapping[str, Fix] = MappingProxyType({})
        self._known = False
        try:
            with settings.read() as db:
                self._map = _read(db)
            self._known = True
        except SettingsUnavailable as error:
            # Covers fall back to automatic; values() stays unknown so the
            # daily sweep cannot remove the images of fixes it cannot see.
            event(log, logging.ERROR, "cover_fixes_unreadable", error=str(error))

    def get(self, key: str) -> Fix | None:
        return self._map.get(key)

    def all(self) -> list[Fix]:
        return list(self._map.values())

    def values(self) -> set[str] | None:
        """The images the fixes name, or None while the table is unread."""
        if not self._known:
            return None
        return {fix.value for fix in self._map.values() if fix.value is not None}

    def set(self, fix: Fix) -> None:
        with self._write:
            with self._settings.transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO cover_fixes VALUES (?, ?, ?, ?, ?, ?, ?)",
                    astuple(fix),
                )
                committed = _read(db)
            self._map = committed
            self._known = True

    def clear(self, key: str) -> Fix | None:
        """Removes the fix; returns the row deleted (read from the database,
        so it is exact even when the start-up read failed), or None when
        there was none."""
        with self._write:
            with self._settings.transaction() as db:
                row = db.execute(
                    "DELETE FROM cover_fixes WHERE key = ?"
                    " RETURNING key, value, label, source, artist, title, set_at",
                    (key,),
                ).fetchone()
                committed = _read(db)
            self._map = committed
            self._known = True
        if row is None:
            return None
        return Fix(*cast("_Row", row))
