"""Writes TOML: the standard library reads it (tomllib) but has no writer.
Enough of TOML for tunebox's configuration: strings, numbers, booleans,
arrays of those, tables and arrays of tables. Whatever tomllib.loads makes
of the output equals the input; that is the whole contract."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import cast

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def dumps(data: Mapping[str, object]) -> str:
    lines: list[str] = []
    _table(lines, [], data)
    return "\n".join(lines).strip() + "\n"


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise ValueError(f"TOML keys are strings, not {type(key).__name__}")
    return key if _BARE.fullmatch(key) else _string(key)


def _string(text: str) -> str:
    out: list[str] = []
    for ch in text:
        # A lone surrogate cannot be encoded as UTF-8, so the text could not be written.
        if "\ud800" <= ch <= "\udfff":
            raise ValueError("TOML text cannot hold a lone surrogate")
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _is_table(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_table_array(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    tables = [_is_table(item) for item in cast("list[object]", value)]
    if any(tables) and not all(tables):
        raise ValueError("an array mixes tables and values")
    return all(tables)


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # nan and inf read back as nan and inf, but nan never equals itself.
        if not math.isfinite(value):
            raise ValueError("TOML here holds finite numbers only")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(item) for item in cast("list[object]", value)) + "]"
    raise ValueError(f"TOML cannot hold {type(value).__name__}")


def _table(lines: list[str], path: list[str], data: Mapping[str, object]) -> None:
    # Plain values first: once a [header] is written, every key after it
    # belongs to that table.
    nested: list[tuple[str, object]] = []
    for key, value in data.items():
        if _is_table(value) or _is_table_array(value):
            nested.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_scalar(value)}")
    for key, value in nested:
        here = [*path, _key(key)]
        if _is_table(value):
            lines.extend(["", f"[{'.'.join(here)}]"])
            _table(lines, here, cast("Mapping[str, object]", value))
        else:
            for item in cast("Sequence[Mapping[str, object]]", value):
                lines.extend(["", f"[[{'.'.join(here)}]]"])
                _table(lines, here, item)
