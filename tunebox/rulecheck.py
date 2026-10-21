"""Guards for the title rules the panel saves. They are regular
expressions that run on every title from a feed tunebox does not control,
and CPython's re can be neither interrupted nor run beside other threads (it
holds the GIL): one slow match freezes the whole process, on every poll that
sees the title again.

So the guarantee is structural (lint): a pattern is refused when its shape
allows backtracking that grows faster than the title, read from re's own
parse tree so brackets cannot hide it. New patterns are then also timed in a
child process (check) on recent titles and on titles built from the
pattern's own characters, each search with its own budget, as a backstop for
what the shape rules miss."""

from __future__ import annotations

import bisect
import functools
import importlib
import itertools
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from typing import Any, cast

from tunebox.config import PATTERN_RULES, ConfigState
from tunebox.tracks import MAX_TITLE

MAX_PATTERN = 200
PER_SEARCH_S = 0.05
# The whole check, the child's start-up included; most of it is start-up.
CHECK_TIMEOUT_S = 2.0
MAX_SAMPLES = 200
# Long, repetitive titles: what makes backtracking blow up.
SYNTHETIC = (
    "a" * MAX_TITLE,
    "a" * (MAX_TITLE - 1) + "!",
    "A" * (MAX_TITLE - 1) + "b",
    "1" * (MAX_TITLE - 1) + "a",
    ("1a " * MAX_TITLE)[:MAX_TITLE],
    " " * (MAX_TITLE - 1) + "x",
    ("(" + "x " * MAX_TITLE)[: MAX_TITLE - 1] + ")",
    ("Played by " + "a" * MAX_TITLE)[:MAX_TITLE],
)
NESTED = "nested repetition such as (a+)+ or (a|aa)+ is not allowed"
CHAINED = (
    "three or more repeats that can match the same characters, such as .*-.*-.*;"
    " use a class without the separator, such as [^-]*"
)
# re's own parser, the one re.compile uses: what it reads is what runs. It
# is private and untyped, so it is reached by name.
_parser: Any = importlib.import_module("re._parser")
_c: Any = importlib.import_module("re._constants")
_REPEATS = {_c.MAX_REPEAT, _c.MIN_REPEAT, _c.POSSESSIVE_REPEAT}
_REFERENCES = {_c.GROUPREF, _c.GROUPREF_EXISTS}
_ZERO_WIDTH = {_c.AT, _c.ASSERT, _c.ASSERT_NOT}
# Character sets are compared over these: Latin-1 and the punctuation,
# spaces and scripts feeds actually send.
_PROBE = tuple(
    chr(c) for c in (*range(256), 0x2013, 0x2014, 0x2019, 0x2028, 0x3000, 0x4E2D, 0x1F3B5)
)


def _space(ch: str) -> bool:
    return ch.isspace() or ch in "\x1c\x1d\x1e\x1f\x85"


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _anything(_ch: str) -> bool:
    return True


_CATEGORIES: dict[Any, Callable[[str], bool]] = {
    _c.CATEGORY_DIGIT: str.isdecimal,
    _c.CATEGORY_SPACE: _space,
    _c.CATEGORY_WORD: _word,
}
_NEGATED = {
    _c.CATEGORY_NOT_DIGIT: _c.CATEGORY_DIGIT,
    _c.CATEGORY_NOT_SPACE: _c.CATEGORY_SPACE,
    _c.CATEGORY_NOT_WORD: _c.CATEGORY_WORD,
}
_RUN = (
    "import json, re, sys, time\n"
    "job = json.load(sys.stdin)\n"
    "for index, item in enumerate(job['patterns']):\n"
    "    print(index, flush=True)\n"
    "    compiled = re.compile(item['pattern'])\n"
    "    for sample in job['samples'] + item['samples']:\n"
    "        started = time.perf_counter()\n"
    "        compiled.search(sample)\n"
    "        if time.perf_counter() - started > job['budget']:\n"
    "            print('slow', flush=True)\n"
    "            sys.exit(3)\n"
    "print('ok')\n"
)
_SLOW = "too slow on long titles; simplify the pattern"


def lint(pattern: str) -> str | None:
    """The reason a pattern is refused, or None."""
    if len(pattern) > MAX_PATTERN:
        return f"at most {MAX_PATTERN} characters"
    try:
        tree = _parser.parse(pattern)
    except re.error as error:
        return f"not a valid pattern: {error}"
    if _references(tree):
        return "back-references are not allowed"
    return _shape(tree, repeated=False, sets=_Sets(tree))


class _Sets:
    """Character sets of a pattern's items, compared exactly over a probe
    built for the pattern.

    Every class an item can use is a union of code-point ranges (its
    literals and ranges) and of \\d, \\s and \\w, and those three split all
    characters into four kinds: digit, space, other word, the rest. The
    characters a pattern names, with their neighbours, cut the code points
    into pieces on which every one of its classes is constant per kind, so
    the first character of each kind in each piece decides every overlap.
    With (?i), characters match through their case family, so every cased
    character is probed too."""

    def __init__(self, tree: object) -> None:
        named = {ch for item in _items(tree) for ch in _named(item)}
        cuts = sorted({0, 256, sys.maxunicode + 1, *(ord(ch) + d for ch in named for d in (0, 1))})
        found = [ch for low, high in itertools.pairwise(cuts) for ch in _kinds(low, high - 1)]
        cased = _tables().cased if _ignores_case(tree) else ()
        self.probe = tuple(dict.fromkeys([*_PROBE, *sorted(named), *found, *cased]))
        self.cache: dict[int, frozenset[str]] = {}


class _Tables:
    """Where each kind of character starts and ends, and the case families,
    from one pass over all code points (about a second, once per process)."""

    def __init__(self) -> None:
        self.runs: list[tuple[list[int], list[int]]] = [([], []) for _ in range(4)]
        families: dict[str, set[str]] = {}
        previous = -1
        for code in range(sys.maxunicode + 1):
            ch = chr(code)
            kind = 0 if ch.isdecimal() else 1 if _space(ch) else 2 if _word(ch) else 3
            starts, ends = self.runs[kind]
            if kind == previous:
                ends[-1] = code
            else:
                starts.append(code)
                ends.append(code)
                previous = kind
            if ch.lower() != ch or ch.upper() != ch:
                for key in {ch, ch.lower(), ch.upper(), ch.casefold()}:
                    families.setdefault(key, set()).add(ch)
        self.families = {key: frozenset(members) for key, members in families.items()}
        self.cased = tuple(sorted({ch for members in families.values() for ch in members}))


@functools.cache
def _tables() -> _Tables:
    return _Tables()


def _kinds(low: int, high: int) -> Iterator[str]:
    """The first character of each kind between low and high."""
    for starts, ends in _tables().runs:
        at = bisect.bisect_left(ends, low)
        if at < len(starts) and starts[at] <= high:
            yield chr(max(starts[at], low))


def _ignores_case(tree: Any) -> bool:
    if tree.state.flags & re.IGNORECASE:
        return True
    return any(
        (op is _c.SUBPATTERN and av[1] & re.IGNORECASE)
        or any(_ignores_case(part) for part in _parts(av))
        for op, av in _items(tree)
    )


def _named(item: tuple[Any, Any]) -> Iterator[str]:
    """Each character an item names, its neighbours and their case
    variants."""
    op, av = item
    if op in (_c.LITERAL, _c.NOT_LITERAL):
        yield from _around(av)
    elif op is _c.RANGE:
        yield from _around(av[0])
        yield from _around(av[1])
    elif op is _c.IN:
        for member in av:
            yield from _named(member)
    else:
        for part in _parts(av):
            for sub in _items(part):
                yield from _named(sub)


def _around(code: int) -> Iterator[str]:
    for near in (code - 1, code, code + 1):
        if 0 <= near <= sys.maxunicode:
            yield from _cased(chr(near))


def _cased(ch: str) -> Iterator[str]:
    """ch, its case variants and every character that folds to one of
    them (U+212A KELVIN SIGN with k)."""
    families = _tables().families
    for variant in (ch, ch.lower(), ch.upper(), ch.casefold()):
        yield from variant
        yield from families.get(variant, ())


def _references(tree: object) -> bool:
    return any(
        op in _REFERENCES or any(_references(part) for part in _parts(av))
        for op, av in _items(tree)
    )


def _shape(tree: object, repeated: bool, sets: _Sets) -> str | None:
    """repeated: inside an item that repeats more than once, where any
    further repeat or alternative multiplies the ways to match."""
    if _chain(tree, sets) >= 3:
        return CHAINED
    for op, av in _items(tree):
        if repeated and (op in _REPEATS or op is _c.BRANCH):
            return NESTED
        many = op in _REPEATS and av[1] > 1
        for part in _parts(av):
            reason = _shape(part, repeated or many, sets)
            if reason:
                return reason
    return None


def _chain(tree: object, sets: _Sets) -> int:
    """The longest run of variable-width items in one sequence where each
    can take over the characters of the one before, across what separates
    them: such a run of k items tries about n**k ways to split a title of n
    characters. Two cost milliseconds at MAX_TITLE; three, seconds."""
    items = list(_flat(tree))
    variable = [i for i, item in enumerate(items) if _variable(item)]
    longest: dict[int, int] = {}
    for position, j in enumerate(variable):
        longest[j] = 1
        mine = _chars(items[j], sets)
        for i in variable[:position]:
            theirs = _chars(items[i], sets)
            if not theirs & mine:
                continue
            between = items[i + 1 : j]
            if all(_width(b)[0] == 0 or theirs & _chars(b, sets) for b in between):
                longest[j] = max(longest[j], longest[i] + 1)
    return max(longest.values(), default=0)


def _flat(tree: object) -> Iterator[tuple[Any, Any]]:
    """The items of a sequence with groups opened up: a group is matched
    in line with what surrounds it."""
    for op, av in _items(tree):
        if op is _c.SUBPATTERN:
            yield from _flat(av[-1])
        else:
            yield op, av


def _width(item: tuple[Any, Any]) -> tuple[int, int]:
    return cast("tuple[int, int]", _parser.SubPattern(_parser.State(), [item]).getwidth())


def _variable(item: tuple[Any, Any]) -> bool:
    low, high = _width(item)
    return low != high


def _chars(item: tuple[Any, Any], sets: _Sets) -> frozenset[str]:
    """The probe characters an item can consume, closed under case so that
    (?i) cannot hide an overlap."""
    key = id(item)
    if key not in sets.cache:
        op, av = item
        if op in _ZERO_WIDTH:
            found: set[str] = set()
        elif op in (_c.LITERAL, _c.NOT_LITERAL, _c.ANY, _c.CATEGORY, _c.IN):
            found = {ch for ch in sets.probe if _matches(op, av, ch)}
        else:
            found = {ch for part in _parts(av) for sub in _items(part) for ch in _chars(sub, sets)}
        sets.cache[key] = frozenset(variant for ch in found for variant in _cased(ch))
    return sets.cache[key]


def _matches(op: Any, av: Any, ch: str) -> bool:
    if op is _c.LITERAL:
        return ord(ch) == av
    if op is _c.NOT_LITERAL:
        return ord(ch) != av
    if op is _c.ANY:
        return ch != "\n"
    if op is _c.RANGE:
        return bool(av[0] <= ord(ch) <= av[1])
    if op is _c.CATEGORY:
        if av in _NEGATED:
            return not _CATEGORIES[_NEGATED[av]](ch)
        return _CATEGORIES.get(av, _anything)(ch)
    if op is _c.IN:
        members = list(av)
        negated = bool(members) and members[0][0] is _c.NEGATE
        hit = any(_matches(o, a, ch) for o, a in (members[1:] if negated else members))
        return hit != negated
    return True


def _items(tree: object) -> Iterator[tuple[Any, Any]]:
    yield from cast("Iterable[tuple[Any, Any]]", tree)


def _parts(av: object) -> Iterator[object]:
    """The sub-patterns an item holds, at any depth of its arguments."""
    if isinstance(av, _parser.SubPattern):
        yield av
    elif isinstance(av, (tuple, list)):
        for value in cast("Iterable[object]", av):
            yield from _parts(value)


def patterns(state: ConfigState) -> list[tuple[str, str]]:
    """Every rule pattern in the state, with the field that holds it."""
    found: list[tuple[str, str]] = []
    for index, row in enumerate(state.channels):
        rules = row.doc.get("rules")
        if not isinstance(rules, dict):
            continue
        table = cast("dict[str, object]", rules)
        for key in PATTERN_RULES:
            value = table.get(key)
            if isinstance(value, str) and value:
                found.append((f"channels[{index}].rules.{key}", value))
    return found


def _unseen(candidate: ConfigState, stored: ConfigState) -> list[tuple[str, str]]:
    """The candidate's patterns that the stored state does not hold."""
    known = {pattern for _, pattern in patterns(stored)}
    return [(field, pattern) for field, pattern in patterns(candidate) if pattern not in known]


def lint_new(candidate: ConfigState, stored: ConfigState) -> str | None:
    """Why the lint refuses the first of the candidate's new patterns, as
    "<field>: <reason>", or None; no child process runs. Patterns already
    stored are left alone: they have run on live titles, and a lint that
    grew stricter must not block every save until they are edited."""
    for field, pattern in _unseen(candidate, stored):
        reason = lint(pattern)
        if reason:
            return f"{field}: {reason}"
    return None


def check_new(
    candidate: ConfigState, stored: ConfigState, samples: Callable[[], list[str]]
) -> str | None:
    """Why the timed check refuses one of the candidate's new patterns, or
    None. Runs after lint_new has passed them. samples gives the titles to
    try new patterns on; gathering them reads every channel, so it is asked
    only when there is a new pattern."""
    new: dict[str, str] = {}
    for field, pattern in _unseen(candidate, stored):
        new.setdefault(pattern, field)
    if not new:
        return None
    listed = list(new)
    result = check(listed, samples())
    if result is None:
        return None
    at, reason = result
    return f"{new[listed[at]] if at is not None else 'rules'}: {reason}"


def stored_refusals(state: ConfigState) -> list[tuple[str, str]]:
    """The stored patterns the current lint refuses, as (field, reason)."""
    return [(field, reason) for field, pattern in patterns(state) if (reason := lint(pattern))]


def samples_for(pattern: str) -> list[str]:
    """Titles made of the pattern's own characters, MAX_TITLE long: runs of
    what each part matches, with a tail nothing matches, and its literal
    text repeated between them. These are the titles that make a slow
    pattern show itself."""
    try:
        tree = _parser.parse(pattern)
    except re.error:
        return []
    sets = _Sets(tree)
    groups: list[frozenset[str]] = []
    literals: list[str] = []
    named: list[str] = []
    _collect(tree, sets, groups, literals, named)
    every = frozenset[str]().union(*groups)
    outside = next((ch for ch in "!~#\x00" if ch not in every), None)
    found: list[str] = []
    # The characters the pattern names itself come first: they are what a
    # hostile title would be made of.
    own = list(dict.fromkeys([*named, *"".join(literals), *" -a1"]))
    for group in dict.fromkeys(groups):
        members = [ch for ch in own if ch in group][:3] or [min(group)]
        for member in members:
            run = member * MAX_TITLE
            found.append(run)
            if outside:
                found.append(run[:-1] + outside)
            for literal in literals:
                for unit in (literal + member, member + literal + member):
                    found.append((unit * MAX_TITLE)[:MAX_TITLE])
    return list(dict.fromkeys(found))[:MAX_SAMPLES]


def _collect(
    tree: object,
    sets: _Sets,
    groups: list[frozenset[str]],
    literals: list[str],
    named: list[str],
) -> None:
    """Gathers the character sets of the pattern's parts, its runs of
    literal text, and the characters its classes name."""
    text = ""
    for op, av in _items(tree):
        if op is _c.LITERAL:
            text += chr(av)
            continue
        if text:
            literals.append(text)
            text = ""
        if op in (_c.NOT_LITERAL, _c.ANY, _c.CATEGORY, _c.IN):
            chars = _chars((op, av), sets)
            if chars:
                groups.append(chars)
            if op is _c.IN:
                named.extend(_named((op, av)))
        for part in _parts(av):
            _collect(part, sets, groups, literals, named)
    if text:
        literals.append(text)
    groups.extend(frozenset(ch) for ch in literals if ch)


def check(
    patterns: list[str], samples: list[str], per_search_s: float = PER_SEARCH_S
) -> tuple[int | None, str] | None:
    """None when every pattern compiles and every search over the samples
    (and the titles built from that pattern) finishes within per_search_s;
    else the index of the pattern refused (None when the check itself could
    not run) and the reason. The child starts isolated (-I) and is killed
    at CHECK_TIMEOUT_S."""
    job = json.dumps(
        {
            "patterns": [{"pattern": p, "samples": samples_for(p)} for p in patterns],
            "samples": [*samples, *SYNTHETIC],
            "budget": per_search_s,
        }
    )
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _RUN],
            input=job,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return _last_started(error.stdout), _SLOW
    except OSError:
        return None, "the rule check could not run; try again later"
    lines = done.stdout.split()
    if done.returncode == 0 and lines[-1:] == ["ok"]:
        return None
    if lines[-1:] == ["slow"]:
        return _last_started(done.stdout), _SLOW
    last = done.stderr.strip().splitlines()[-1:] or ["the pattern could not run"]
    return _last_started(done.stdout), last[0]


def _last_started(stdout: str | bytes | None) -> int | None:
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    started = [int(word) for word in (stdout or "").split() if word.isdigit()]
    return started[-1] if started else None
