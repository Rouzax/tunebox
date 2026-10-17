import logging
from collections.abc import Generator
from pathlib import Path

import pytest

from tunebox.store import Store


@pytest.fixture(autouse=True)
def reset_logging() -> Generator[None]:
    """configure() changes process-wide logger state; undo it between tests."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("tunebox"):
            logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def close_stores(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Closes every Store a test opens, so no SQLite connection outlives its test."""
    opened: list[Store] = []
    original = Store.__init__

    def tracking(self: Store, path: Path | None) -> None:
        original(self, path)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", tracking)
    yield
    for store in opened:
        store.close()
