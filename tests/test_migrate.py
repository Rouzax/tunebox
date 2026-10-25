import logging
import sqlite3
from collections.abc import Generator

import pytest

from tunebox.migrate import SchemaTooNew, migrate

STEPS = ("CREATE TABLE a (x INTEGER)", "CREATE TABLE b (y INTEGER); CREATE INDEX b_y ON b (y)")


@pytest.fixture
def db() -> Generator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    yield conn
    conn.close()


def version(db: sqlite3.Connection) -> int:
    return db.execute("SELECT version FROM schema_version").fetchone()[0]


def test_applies_every_step_in_order_and_records_the_version(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        migrate(db, STEPS, "usage")
    assert version(db) == 2
    db.execute("INSERT INTO b VALUES (1)")
    applied = [r for r in caplog.records if r.getMessage() == "migration_applied"]
    assert [r.__dict__["fields"] for r in applied] == [
        {"database": "usage", "version": 1},
        {"database": "usage", "version": 2},
    ]


def test_runs_only_the_steps_not_yet_applied(db: sqlite3.Connection) -> None:
    migrate(db, STEPS[:1], "usage")
    migrate(db, STEPS, "usage")
    assert version(db) == 2


def test_a_newer_schema_is_refused_untouched(db: sqlite3.Connection) -> None:
    migrate(db, STEPS, "usage")
    with pytest.raises(SchemaTooNew):
        migrate(db, STEPS[:1], "usage")
    assert version(db) == 2


def test_a_failing_step_rolls_back_whole(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.OperationalError):
        migrate(db, ("CREATE TABLE a (x INTEGER); CREATE TABLE a (x INTEGER)",), "usage")
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'a'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 0
