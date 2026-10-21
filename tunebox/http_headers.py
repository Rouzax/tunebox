"""Reading request headers the one way tunebox trusts: a header that came
more than once is no value at all. A proxy that splits or a client that
repeats a security header gives two answers, and either may be the forged
one."""

from __future__ import annotations

from collections.abc import Iterable


def single(pairs: Iterable[tuple[str, str]], name: str) -> str | None:
    wanted = name.lower()
    found = [value for key, value in pairs if key.lower() == wanted]
    return found[0] if len(found) == 1 else None
