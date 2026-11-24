"""Facts that show a symbol is used although no call names it.

The dead-code detector (:func:`code_review_graph.refactor.find_dead_code`) can
only see what the graph records. Four kinds of use leave no ``CALLS`` edge, so
this module reads them out of a parsed file for the parser to record:

* a bare name passed as a value (``xs.map(parse)``, ``register(handler)``,
  ``callback=fn``) or a type named in a type position (a Python annotation, a
  Java or Scala ``type_identifier``) or an enum-style member read
  (``Status.OK``), resolved to a definition in the same file;
* the names a file re-exports (``from .core import X`` in an ``__init__.py``,
  ``export { X } from './x'``, ``export * from './x'``), which are the public
  surface of the module they sit in;
* the function a handler string names (``onclick="fn(1)"`` written into a
  template, or built inside any source file), which runs in a browser that no
  graph edge reaches.

Everything here is a pure function of the parse tree or the source text, so it
is cheap to test and has no store access.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

# Value-position / type-position / member-read candidates are only worth
# reading in these languages. JavaScript and TypeScript argument callbacks are
# handled where the parser walks the tree; they are here only for the member
# read and the same-file fallback.
VALUE_REFERENCE_LANGUAGES = frozenset({
    "python", "java", "scala", "javascript", "typescript", "tsx",
})

# Node types holding the arguments of a call.
_ARGUMENT_LIST_TYPES = frozenset({"argument_list", "arguments"})

# Node types whose first identifier child is the object of a member read.
_MEMBER_READ_TYPES = {
    "python": frozenset({"attribute"}),
    "java": frozenset({"field_access"}),
    "scala": frozenset({"field_expression"}),
    "javascript": frozenset({"member_expression"}),
    "typescript": frozenset({"member_expression"}),
    "tsx": frozenset({"member_expression"}),
}

# Names that are built in or so common that a type-position edge to them is
# noise (and could only ever match a same-named project class by accident).
TYPE_USE_STOPLIST = frozenset({
    "String", "Int", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short",
    "Char", "Character", "Unit", "Any", "AnyRef", "AnyVal", "Object", "Nothing", "Null",
    "Seq", "List", "Map", "Set", "Option", "Some", "None", "Array", "Vector", "Either",
    "Try", "Future", "Iterator", "Iterable", "Optional", "Collection", "Class", "Void",
    "Number", "Date", "Dict", "Tuple", "Type", "Callable", "Literal", "Union", "Self",
})


def _walk(root) -> Iterator:
    """Every node under *root*, iteratively (nesting comes from the input)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def value_candidates(root, language: str) -> list[tuple[str, int, str]]:
    """Names used as values, in type positions or as ``Name.member`` objects.

    Returns ``(name, line, form)`` where *form* is ``"arg"`` (a bare identifier
    argument, or the value of a keyword argument), ``"type"`` (a type named in
    a type position) or ``"member"`` (the object of ``Name.member``).
    """
    if language not in VALUE_REFERENCE_LANGUAGES:
        return []
    member_types = _MEMBER_READ_TYPES.get(language, frozenset())
    found: list[tuple[str, int, str]] = []
    for node in _walk(root):
        node_type = node.type
        if node_type in _ARGUMENT_LIST_TYPES:
            if language in ("javascript", "typescript", "tsx"):
                continue  # read by the parser's own callback extraction
            for arg in node.children:
                value = arg
                if arg.type == "keyword_argument":
                    value = arg.child_by_field_name("value")
                if value is not None and value.type == "identifier":
                    found.append((_text(value), value.start_point[0] + 1, "arg"))
        elif node_type in member_types:
            obj = _member_object(node)
            if obj is not None and obj.type == "identifier":
                name = _text(obj)
                if name[:1].isupper() and not name.isupper():
                    found.append((name, obj.start_point[0] + 1, "member"))
        elif node_type == "type_identifier" and language in ("java", "scala"):
            found.append((_text(node), node.start_point[0] + 1, "type"))
        elif node_type == "type" and language == "python":
            # An annotation: ``x: Item``, ``-> List[Item]``, ``p: Optional[Item]``.
            for sub in _walk(node):
                if sub.type == "identifier":
                    found.append((_text(sub), sub.start_point[0] + 1, "type"))
    return found


def _member_object(node):
    """The object of a member read, or ``None``."""
    obj = node.child_by_field_name("object")
    if obj is None:
        obj = node.child_by_field_name("value")  # Scala field_expression
    if obj is None and node.named_children:
        obj = node.named_children[0]
    return obj


def python_reexport_names(import_node) -> list[str]:
    """Names a ``from x import ...`` statement brings in; ``["*"]`` for a star.

    An ``as`` alias reports the original name, which is the one the source
    module defines.
    """
    module = import_node.child_by_field_name("module_name")
    names: list[str] = []
    for child in import_node.children:
        if module is not None and child.start_byte == module.start_byte:
            continue
        if child.type == "wildcard_import":
            return ["*"]
        if child.type == "dotted_name":
            names.append(_text(child).rsplit(".", 1)[-1])
        elif child.type == "aliased_import":
            original = child.child_by_field_name("name")
            if original is not None:
                names.append(_text(original).rsplit(".", 1)[-1])
    return names


def js_reexport(export_node) -> Optional[tuple[str, list[str]]]:
    """``(module specifier, names)`` for ``export ... from '<module>'``.

    ``export * from`` and ``export * as ns from`` give ``["*"]``; a default
    re-export (``export { default } from``) names nothing the source defines
    under that word, so it is left out.
    """
    source = export_node.child_by_field_name("source")
    if source is None:
        return None
    spec = _text(source).strip().strip("'\"`")
    if not spec:
        return None
    names: list[str] = []
    for child in export_node.children:
        if child.type == "*" or child.type == "namespace_export":
            return spec, ["*"]
        if child.type == "export_clause":
            for specifier in child.children:
                if specifier.type != "export_specifier":
                    continue
                original = specifier.child_by_field_name("name")
                if original is not None and _text(original) != "default":
                    names.append(_text(original))
    return spec, names


# --- inline handler strings ------------------------------------------------

# ``onclick="fn(1)"``, ``onclick=\"fn(1)\"``, ``hx-on:click="fn()"``,
# ``@click="fn()"``, ``x-on:click="fn()"``. The name must be a plain call:
# ``obj.run()`` and ``alert(...)`` after a ``return`` or ``;`` are not read as
# names this page defines, except for a leading ``return ``.
_HANDLER_ATTRIBUTE = (
    r"(?:\bon[a-z]{3,20}|\bhx-on[\w:.-]*|\bx-on:[\w.:-]+|(?<![\w@])@[\w.:-]+)"
)
INLINE_HANDLER_RE = re.compile(
    _HANDLER_ATTRIBUTE
    + r"""\s*=\s*\\*["']\s*(?:return\s+)?([A-Za-z_$][\w$]*)\s*\(""",
)
# ``hx-vals='js:fn()'`` and ``hx-headers="js:fn()"``.
_HTMX_JS_RE = re.compile(r"""\bhx-[\w-]+\s*=\s*\\*["']\s*js:\s*([A-Za-z_$][\w$]*)\s*\(""")

MAX_INLINE_HANDLER_NAMES = 200

# Cheap byte-level test run before the text is decoded and scanned.
INLINE_HANDLER_HINT = re.compile(rb"\bon[a-z]{3,20}\s*=|hx-|x-on:|@[\w.:-]+\s*=")


def inline_handler_names(text: str) -> list[str]:
    """Function names that handler attributes in *text* call, sorted, bounded."""
    names = {m.group(1) for m in INLINE_HANDLER_RE.finditer(text)}
    names.update(m.group(1) for m in _HTMX_JS_RE.finditer(text))
    return sorted(names)[:MAX_INLINE_HANDLER_NAMES]


def nested_function_names(root) -> set[str]:
    """Names of JavaScript function declarations nested below file scope.

    A script block often wraps its functions in an ``init()`` or an IIFE, so
    ``pending.map(savePanel)`` names a function the file-scope pre-scan never
    saw. Only declarations count (not methods or arrow variables).
    """
    names: set[str] = set()
    for node in _walk(root):
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                names.add(_text(name))
    return names
