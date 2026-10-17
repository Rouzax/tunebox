"""Structured JSON logging: a fixed event name per line, data in fields."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import secrets
import sys
import time
from collections.abc import Generator
from typing import Any, TextIO, cast

context_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("context_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        entry: dict[str, Any] = {
            "ts": f"{stamp}.{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        ctx = context_id.get()
        if ctx:
            entry["ctx"] = ctx
        fields: object = record.__dict__.get("fields")
        if isinstance(fields, dict):
            entry.update(cast("dict[str, Any]", fields))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # json.dumps escapes control characters, so source data cannot forge a line.
        return json.dumps(entry, ensure_ascii=False, default=str)


def event(logger: logging.Logger, level: int, name: str, **fields: object) -> None:
    if logger.isEnabledFor(level):
        logger.log(level, name, extra={"fields": fields})


@contextlib.contextmanager
def bound(prefix: str) -> Generator[str]:
    """Tags every line logged inside the block with one id (a request, a poll)."""
    ctx = f"{prefix}-{secrets.token_hex(4)}"
    token = context_id.set(ctx)
    try:
        yield ctx
    finally:
        context_id.reset(token)


def configure(spec: str, stream: TextIO = sys.stdout) -> None:
    """Applies a spec such as "info,tunebox.covers=debug": a root level, then per-logger levels."""
    levels: dict[str, int] = {}
    names = logging.getLevelNamesMapping()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        logger_name, _, level_name = part.rpartition("=")
        level = names.get(level_name.upper())
        if level is None:
            raise ValueError(f"unknown log level {level_name!r} in LOG_LEVEL")
        levels[logger_name] = level
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(levels.pop("", logging.INFO))
    for logger_name, level in levels.items():
        logging.getLogger(logger_name).setLevel(level)
