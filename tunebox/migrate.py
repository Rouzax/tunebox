"""Numbered schema migrations for a SQLite file: each entry upgrades the
schema by one version and is never edited once released. Statements are
separated by ";" and contain none themselves."""

from __future__ import annotations

import logging
import sqlite3
from typing import cast

from tunebox.logs import event

log = logging.getLogger(__name__)


class SchemaTooNew(sqlite3.DatabaseError):
    """The file was written by a newer tunebox; it is left untouched."""


def migrate(db: sqlite3.Connection, migrations: tuple[str, ...], database: str) -> None:
    """db must be in autocommit mode (isolation_level=None): each step is
    its own BEGIN IMMEDIATE transaction, so a crash leaves the file at a
    released version."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = db.execute("SELECT version FROM schema_version").fetchone()
    current = 0 if row is None else cast(int, row[0])
    if current > len(migrations):
        raise SchemaTooNew(
            f"schema version {current} is newer than this tunebox knows ({len(migrations)})"
        )
    for number in range(current, len(migrations)):
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in migrations[number].split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("DELETE FROM schema_version")
            db.execute("INSERT INTO schema_version VALUES (?)", (number + 1,))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        event(log, logging.INFO, "migration_applied", database=database, version=number + 1)
