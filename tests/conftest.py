import logging
from collections.abc import Generator

import pytest


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
