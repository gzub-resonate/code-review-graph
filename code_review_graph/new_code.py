"""Risk for the code a pull request adds, which no caller in the base graph reaches yet.

The risk score of a change is built from what already surrounds a symbol: its callers, flows
and tests. Code a pull request adds has none of that, so scored the same way it would look
safe. This module scores it from what the pull request itself shows: whether a test added with
it calls the symbol, whether it is a new endpoint, how large and branchy it is, and whether its
name looks security-related. The terms and weights follow ``changes.compute_risk_score``.

The score is a heuristic. A test is tied to a symbol by the names the parser reads, not by a
resolved call, and every row says how it was tied (``coverage``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .constants import SECURITY_KEYWORDS
from .parser import is_test_file

__all__ = ["NewSymbol", "collect", "score_new_code"]

#: Symbols kept per file, so a generated file cannot grow a request without bound.
MAX_SYMBOLS_PER_FILE = 400

#: Weights, as in ``changes.compute_risk_score``: the untested and security terms are the same,
#: a new endpoint counts like a flow entry point, and size and branching share a small term.
UNTESTED = 0.30
NAME_ONLY = 0.15
UNIT_ONLY = 0.10
ENDPOINT = 0.25
SECURITY = 0.20
SHAPE = 0.15

#: Lines and branch points at which each half of the shape term is full.
_FULL_LINES = 80
_FULL_BRANCHES = 12

_BRANCH = re.compile(
    r"\b(?:if|elif|for|foreach|while|case|catch|except|switch|when)\b|&&|\|\||\?\s",
)
#: Names too common for a bare-name match to mean anything.
_GENERIC = frozenset({
    "get", "set", "add", "run", "apply", "of", "from", "test", "main", "build", "init",
    "create", "update", "delete", "find", "load", "save", "call", "handle", "equals",
    "hashcode", "tostring", "close", "start", "stop", "execute", "process", "value",
})
_MIN_NAME = 4
_TEST_SUFFIXES = (
    "unittest", "integrationtest", "webmvctest", "mockmvctest", "_tests", "_test", "tests",
    "test", "_spec", "spec",
)


@dataclass(frozen=True)
class NewSymbol:
    """What the parser read about one symbol of a pull request's file."""

    file: str
    name: str
    kind: str
    parent: Optional[str]
    line_start: int
    line_end: int
    is_test: bool
    #: Handles a route (a new endpoint).
    handler: bool = False
    #: Branch points in its body, counted from the source text.
    branches: int = 0
    #: The last segment of every name it calls.
    calls: frozenset[str] = frozenset()
    #: Added by the pull request, as against an existing test kept for the names it calls.
    added: bool = True


def _local(qualified: str) -> str:
    """``Class.method`` from a parser's ``path::Class.method``."""
    return qualified.split("::", 1)[-1]


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def _unit(path: str, *, test: bool) -> str:
    """The thing a file is about: ``FooServiceIT.java`` and ``test_foo.py`` name ``fooservice``
    and ``foo``; a production file names itself."""
    stem = _stem(path)
    if not test:
        return stem.lower()
    lowered = stem.lower()
    if lowered.startswith("test_"):
        lowered = lowered[len("test_"):]
    for suffix in _TEST_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            return lowered[: -len(suffix)]
    if len(stem) > 2 and stem.endswith("IT") and stem[-3].islower():
        return lowered[:-2]
    return lowered


def collect(
    path: str, nodes: Iterable[Any], edges: Iterable[Any], text: str,
    added: set[tuple[str, str, str]],
) -> list[NewSymbol]:
    """The symbols of one parsed file that matter for scoring.

    Every test symbol is kept, for the names it calls. Of the rest, only the symbols the pull
    request adds (*added*: ``(kind, name, parent)`` keys) are, as production code to score.
    """
    handlers: set[str] = set()
    calls: dict[str, set[str]] = {}
    for edge in edges:
        if edge.kind == "HANDLES":
            handlers.add(_local(edge.source))
        elif edge.kind == "CALLS":
            calls.setdefault(_local(edge.source), set()).add(
                _local(edge.target).rsplit(".", 1)[-1]
            )
    lines = text.splitlines()
    in_test_file = is_test_file(path)
    out: list[NewSymbol] = []
    for node in nodes:
        if node.kind not in ("Function", "Test", "Class"):
            continue
        is_test = bool(node.is_test) or node.kind == "Test" or in_test_file
        key = (node.kind, node.name, node.parent_name or "")
        if not is_test and key not in added:
            continue
        if len(out) >= MAX_SYMBOLS_PER_FILE:
            break
        local = f"{node.parent_name}.{node.name}" if node.parent_name else node.name
        start, end = node.line_start or 0, node.line_end or 0
        body = "\n".join(lines[max(0, start - 1):end]) if end >= start > 0 else ""
        out.append(NewSymbol(
            file=path, name=node.name, kind=node.kind, parent=node.parent_name,
            line_start=start, line_end=end, is_test=is_test, handler=local in handlers,
            branches=0 if is_test else len(_BRANCH.findall(body)),
            calls=frozenset(calls.get(local, ())), added=key in added,
        ))
    return out


def _coverage(symbol: NewSymbol, tests: list[NewSymbol]) -> tuple[str, list[str]]:
    """How a test tied to *symbol* was found.

    ``linked``: a test of the symbol's class (or module) calls it, or a test file for the class
    exists and the symbol is the class itself. ``name_only``: a test elsewhere calls its name.
    ``unit_only``: a test file for its class exists but no test calls it, which may only mean
    it is reached through another method. ``none``: no test in the pull request relates to it.

    Returns the tier and the names of up to three tests.
    """
    owner = symbol.name if symbol.kind == "Class" else symbol.parent
    unit = (owner or _stem(symbol.file)).lower()
    file_unit = _stem(symbol.file).lower()
    unit_tests = [
        t for t in tests
        if _unit(t.file, test=True) in {unit, file_unit}
        or (owner and (t.parent or "").lower().startswith(owner.lower()))
    ]
    calling_all = [t for t in tests if symbol.name in t.calls]
    linked = [
        t for t in calling_all
        if t in unit_tests or (owner is not None and owner in t.calls)
    ]
    if linked:
        return "linked", [t.name for t in linked[:3]]
    if symbol.kind == "Class" and unit_tests:
        return "linked", [t.name for t in unit_tests[:3]]
    # A name like ``get`` is in every codebase, so it can tie a test to a symbol only
    # together with the symbol's class or file, never alone.
    too_common = len(symbol.name) < _MIN_NAME or symbol.name.lower() in _GENERIC
    calling = [] if too_common else calling_all
    if calling:
        return "name_only", [t.name for t in calling[:3]]
    if unit_tests:
        return "unit_only", []
    return "none", []


def _row(symbol: NewSymbol, tests: list[NewSymbol]) -> dict[str, Any]:
    coverage, test_names = _coverage(symbol, tests)
    score = 0.0
    reasons: list[str] = []
    # A class is exercised through its methods, which are scored on their own, so its own
    # coverage is shown but adds nothing: otherwise every data class would rank as untested.
    carries_coverage = symbol.kind != "Class"
    if coverage == "none":
        reasons.append("no test in this pull request calls it")
        score += UNTESTED if carries_coverage else 0.0
    elif coverage == "unit_only":
        reasons.append(
            "a test file for its class is in this pull request, but no test calls it directly"
        )
        score += UNIT_ONLY if carries_coverage else 0.0
    elif coverage == "name_only":
        reasons.append("a test calls its name, but nothing ties that test to this class")
        score += NAME_ONLY if carries_coverage else 0.0
    if symbol.handler:
        score += ENDPOINT
        reasons.append("new endpoint")
    haystack = f"{symbol.name} {symbol.file}".lower()
    if any(word in haystack for word in SECURITY_KEYWORDS):
        score += SECURITY
        reasons.append("security-related name")
    size = max(1, symbol.line_end - symbol.line_start + 1)
    shape = (
        min(1.0, size / _FULL_LINES) * SHAPE / 2
        + min(1.0, symbol.branches / _FULL_BRANCHES) * SHAPE / 2
    )
    score += shape
    if shape >= SHAPE / 2:
        reasons.append(f"{size} lines, {symbol.branches} branch point(s)")
    row: dict[str, Any] = {
        "file": symbol.file, "name": symbol.name, "kind": symbol.kind,
        "line": symbol.line_start, "score": round(min(score, 1.0), 4),
        "coverage": coverage, "reasons": reasons,
    }
    if symbol.parent:
        row["parent"] = symbol.parent
    if test_names:
        row["tests"] = test_names
    return row


def score_new_code(symbols: Iterable[NewSymbol]) -> dict[str, Any]:
    """Score the production symbols a pull request adds, riskiest first.

    Returns ``rows`` (one per added production symbol, ordered by score, then file and
    line), ``production_added``, ``tests_added`` (test symbols in the files read),
    ``untested`` (functions whose coverage is ``none``; a class is exercised through its
    methods) and ``score``, the highest row score, as the overall risk of a change is its
    riskiest symbol.
    """
    everything = list(symbols)
    tests = [s for s in everything if s.is_test]
    production = [s for s in everything if not s.is_test]
    rows = [_row(s, tests) for s in production]
    rows.sort(key=lambda r: (-r["score"], r["file"], r["line"], r["name"]))
    return {
        "rows": rows,
        "production_added": len(rows),
        "tests_added": sum(1 for s in tests if s.added and s.kind in ("Test", "Function")),
        "untested": sum(1 for r in rows if r["coverage"] == "none" and r["kind"] != "Class"),
        "score": rows[0]["score"] if rows else 0.0,
    }
