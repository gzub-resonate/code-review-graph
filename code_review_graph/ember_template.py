"""Bounded structural scan of Ember Handlebars / Glimmer templates (``.hbs``).

No Handlebars or Glimmer grammar ships with ``tree-sitter-language-pack``, so
this is a small hand-written scanner in the spirit of the Liquid support: it
finds *which components, helpers and modifiers a template invokes by name* and
nothing else. It never stores a string literal, an attribute value or any text
of the template; only identifier-shaped names.

What counts as an invocation:

* ``<FooBar />`` and ``<Foo::Bar>``: a component (capitalised tag, no dot).
* ``{{foo-bar ...}}``, ``{{foo/bar}}``, ``{{#foo-bar}}``: a component or a
  helper. Ember resolves the name against both, so the scanner calls it
  ``invocable`` and leaves the choice to the resolver, which knows the files.
* ``{{foo arg}}`` with arguments: also an invocation. A bare ``{{foo}}``
  without a dash, slash or arguments is a property read and is skipped.
* ``<div {{foo-bar}}>``: a modifier.
* ``(foo-bar x)`` inside a mustache: a helper.
* ``{{component "foo-bar"}}``, ``{{helper "x"}}``, ``{{modifier "x"}}``: the
  literal that *is* the name. Dynamic forms (``{{component this.which}}``) are
  skipped.

Skipped on purpose: built-in keywords and helpers (``if``, ``each``, ``on``,
``fn``, ``yield``, ...), dotted or ``@``/``this`` paths, block parameters
(``as |foo|``) declared anywhere in the file, comments, and raw blocks.

Bounds: input over :data:`MAX_TEMPLATE_BYTES` is not scanned, and at most
:data:`MAX_INVOCATIONS` distinct names are kept per file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ember_extract import dasherize

MAX_TEMPLATE_BYTES = 1_000_000
MAX_INVOCATIONS = 2_000
#: Most characters one mustache or one tag may span before it is abandoned.
_MAX_SPAN = 20_000
#: ``_skip_mustache`` result for a mustache that never closes within the span.
UNTERMINATED = -1
#: Characters one file may cost to scan in all (a hostile file stops here).
MAX_WORK = 4_000_000
_RAW_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_TOKEN_RE = re.compile(r"<|\{\{")
_LITERAL_RE = re.compile(r"\s*(['\"])([A-Za-z][A-Za-z0-9_/-]*)\1")

BUILTINS = frozenset("""
action and array concat debugger each each-in else fn get has-block
has-block-params hash if if-let in-element input let link-to log loading
modifier mut on outlet partial readonly textarea unbound unless with yield
component helper t-eq eq not or neq gt gte lt lte lookup-helper
""".split())
#: Built-in angle-bracket components.
BUILTIN_COMPONENTS = frozenset({
    "Input", "Textarea", "LinkTo", "Await", "Portal", "Fragment",
})

_NAME_RE = re.compile(r"[a-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9_-]+)*")
_TAG_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]*")
_BLOCK_PARAMS_RE = re.compile(r"\bas\s*\|([^|]*)\|")


class _BudgetExceededError(Exception):
    """Raised inside :func:`scan` when a file costs more than :data:`MAX_WORK`."""


@dataclass(frozen=True)
class Invocation:
    """One distinct name a template invokes, with where and how often."""

    ref: str  # component | invocable | helper | modifier
    name: str  # slash-separated, dasherized
    line: int  # first line
    count: int
    syntax: str  # angle | curly | block | subexpression | modifier | literal


def _skip_mustache(text: str, start: int) -> int:
    """Index just past the ``}}`` closing the mustache that opens at *start*.

    Quotes protect a ``}}`` inside a string. Returns :data:`UNTERMINATED` (-1)
    for a mustache with no closer within :data:`_MAX_SPAN` characters, so a
    caller never treats the rest of the file as a mustache body.
    """
    i = start + 2
    limit = min(len(text), start + _MAX_SPAN)
    quote = ""
    while i < limit:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "}" and text.startswith("}}", i):
            end = i + 2
            while end < len(text) and text[end] == "}":
                end += 1
            return end
        i += 1
    return UNTERMINATED


def _normalise_angle(name: str) -> str:
    return "/".join(dasherize(part) for part in name.split("::"))


def _first_literal(text: str, pos: int = 0) -> str:
    """The name in ``"foo-bar" ...``/``'foo-bar'`` at *pos*, only when a plain name."""
    match = _LITERAL_RE.match(text, pos)
    return match.group(2) if match else ""


def scan(text: str) -> list[Invocation]:
    """Return the distinct component, helper and modifier names in *text*."""
    if len(text) > MAX_TEMPLATE_BYTES:
        return []
    locals_: set[str] = set()
    for match in _BLOCK_PARAMS_RE.finditer(text):
        locals_.update(match.group(1).split())

    found: dict[tuple[str, str], list] = {}  # (ref, name) -> [line, count, syntax]
    work = 0

    def charge(amount: int) -> None:
        nonlocal work
        work += amount
        if work > MAX_WORK:
            raise _BudgetExceededError

    def skip(src: str, start: int) -> int:
        end = _skip_mustache(src, start)
        charge((end if end != UNTERMINATED else min(len(src), start + _MAX_SPAN)) - start)
        return end

    def add(ref: str, name: str, offset: int, syntax: str) -> None:
        if not name:
            return
        key = (ref, name)
        entry = found.get(key)
        if entry is not None:
            entry[1] += 1
            return
        if len(found) >= MAX_INVOCATIONS:
            return
        found[key] = [text.count("\n", 0, offset) + 1, 1, syntax]

    def subexpressions(body: str, offset: int) -> None:
        # Walk the mustache body outside quotes for ``(name`` heads.
        charge(len(body))
        quote = ""
        i = 0
        while i < len(body):
            ch = body[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                match = _NAME_RE.match(body, i + 1)
                if match:
                    name = match.group(0)
                    end = match.end()
                    if name == "component":
                        literal = _first_literal(body, end)
                        if literal:
                            add("component", dasherize(literal), offset, "literal")
                    elif name == "helper":
                        literal = _first_literal(body, end)
                        if literal:
                            add("helper", dasherize(literal), offset, "literal")
                    elif name == "modifier":
                        literal = _first_literal(body, end)
                        if literal:
                            add("modifier", dasherize(literal), offset, "literal")
                    elif name not in BUILTINS and name not in locals_:
                        add("helper", name, offset, "subexpression")
            i += 1

    def mustache(body: str, offset: int, in_tag: bool) -> None:
        stripped = body.lstrip("{~").rstrip("}~").strip()
        if not stripped or stripped[0] in "/!>&^":
            return
        block = stripped[0] == "#"
        if block:
            stripped = stripped[1:].lstrip("~ ")
        subexpressions(stripped, offset)
        match = _NAME_RE.match(stripped)
        if not match:
            return
        name = match.group(0)
        rest = stripped[match.end():]
        # ``foo.bar`` and ``foo:bar`` are paths, not names.
        if rest[:1] in (".", ":") or name in locals_:
            return
        if name in ("component", "helper", "modifier"):
            literal = _first_literal(stripped, match.end())
            if literal:
                ref = {"component": "component", "helper": "helper", "modifier": "modifier"}[name]
                add(ref, dasherize(literal), offset, "literal")
            return
        if name in BUILTINS or name == "else":
            return
        if in_tag:
            add("modifier", name, offset, "modifier")
            return
        has_args = bool(rest.strip())
        if block:
            add("component", name, offset, "block")
        elif has_args or "-" in name or "/" in name:
            add("invocable", name, offset, "curly")

    def scan_tag(start: int) -> int:
        """Handle the tag opening at *start*; return the index after it."""
        match = _TAG_NAME_RE.match(text, start + 1)
        if not match:
            return start + 1
        name = match.group(0)
        if name[0].isupper() and name not in BUILTIN_COMPONENTS and "." not in name:
            add("component", _normalise_angle(name), start, "angle")
        i = match.end()
        limit = min(len(text), start + _MAX_SPAN)
        after_equals = False
        while i < limit:
            charge(1)
            ch = text[i]
            if ch == ">":
                return i + 1
            if text.startswith("{{", i):
                end = skip(text, i)
                if end == UNTERMINATED:
                    i += 2
                    continue
                if not text.startswith("{{!", i):
                    mustache(text[i + 2:end - 2], i, in_tag=not after_equals)
                after_equals = False
                i = end
                continue
            if ch in "'\"":
                close = text.find(ch, i + 1, limit)
                if close < 0:
                    return limit
                charge(close - i)
                inner = text[i + 1:close]
                k = inner.find("{{")
                while k >= 0:
                    end = skip(inner, k)
                    if end == UNTERMINATED:
                        k = inner.find("{{", k + 2)
                        continue
                    mustache(inner[k + 2:end - 2], i + 1 + k, in_tag=False)
                    k = inner.find("{{", end)
                i = close + 1
                after_equals = False
                continue
            after_equals = ch == "="
            i += 1
        return limit

    i = 0
    n = len(text)
    try:
        while i < n:
            token = _TOKEN_RE.search(text, i)
            if token is None:
                break
            charge(1)
            start = token.start()
            if token.group() == "{{":
                if start > 0 and text[start - 1] == "\\":
                    i = start + 2
                elif text.startswith("{{!--", start):
                    end = text.find("--}}", start + 5)
                    i = n if end < 0 else end + 4
                elif text.startswith("{{{{", start):
                    # Raw block: skip to its own ``{{{{/name}}}}``. With no such
                    # closer the rest of the file is raw, so no later opener
                    # rescans the same tail.
                    named = _RAW_NAME_RE.match(text, start + 4)
                    if named:
                        close = text.find("{{{{/" + named.group(0), start + 4)
                        end = text.find("}}}}", close) if close >= 0 else -1
                    else:
                        end = text.find("}}}}", start + 4)
                    charge((n if end < 0 else end) - start)
                    i = n if end < 0 else end + 4
                else:
                    end = skip(text, start)
                    if end == UNTERMINATED:
                        i = start + 2
                        continue
                    if not text.startswith("{{!", start):
                        mustache(text[start + 2:end - 2], start, in_tag=False)
                    i = end
            elif text.startswith("<!--", start):
                end = text.find("-->", start + 4)
                i = n if end < 0 else end + 3
            elif text.startswith("</", start):
                end = text.find(">", start)
                i = n if end < 0 else end + 1
            else:
                i = scan_tag(start)
    except _BudgetExceededError:
        pass
    return [
        Invocation(ref, name, line, count, syntax)
        for (ref, name), (line, count, syntax) in found.items()
    ]
