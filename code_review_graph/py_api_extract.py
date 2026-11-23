"""Python HTTP server routes (FastAPI, Flask) as ``Endpoint`` nodes.

``@app.get("/x")``, ``@app.post(...)``, ``@app.route("/x", methods=[...])`` and
``@app.api_route(...)`` on a ``FastAPI()`` or ``Flask()`` application built in the
same file become server ``Endpoint`` nodes with a ``HANDLES`` edge from the handler
function, the same shape the JavaScript extractor gives Express and Next.js routes,
so :mod:`code_review_graph.feign_links` can join a caller to them.

A route on an application is complete as written.  A route on an ``APIRouter`` or a
``Blueprint``, or on an application imported from another file, takes part of its path
from a prefix set elsewhere, so it is not recorded here.  A path that is not a string
literal is never guessed: such a route is left out.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "FASTAPI",
    "FLASK",
    "MAX_ENDPOINTS",
    "convert_path",
    "extract",
]

FASTAPI = "fastapi"
FLASK = "flask"

MAX_ENDPOINTS = 2_000

_APP_CLASSES = {"FastAPI": FASTAPI, "Flask": FLASK}
_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_SKIP_DIRS = frozenset({
    "tests", "test", "testing", "fixtures", "examples", "docs", "scripts", "migrations",
    "venv", ".venv", "site-packages", "node_modules", "build", "dist",
})
_FLASK_CONVERTER_RE = re.compile(
    r"<(?:(?P<conv>[A-Za-z_]\w*)(?:\([^)]*\))?:)?(?P<name>[A-Za-z_]\w*)>",
)
_QUICK_RE = re.compile(
    rb"FastAPI|Flask|\.(?:route|get|post|put|patch|delete)\(",
)
_PATH_PARAM_RE = re.compile(r"\{(?P<name>[A-Za-z_]\w*):path\}$")


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace")


def _walk(root: Any):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _literal(node: Any) -> Optional[str]:
    """The value of a plain string literal, else ``None`` (f-strings included)."""
    if node is None:
        return None
    if node.type == "string":
        if any(child.type == "interpolation" for child in node.children):
            return None
        try:
            value = ast.literal_eval(_text(node))
        except (ValueError, SyntaxError):
            return None
        return value if isinstance(value, str) else None
    if node.type == "concatenated_string":
        parts = [_literal(child) for child in node.children if child.type == "string"]
        return None if any(p is None for p in parts) else "".join(p for p in parts if p)
    return None


def _call_parts(call: Any) -> tuple[list[Any], dict[str, Any]]:
    """Positional argument nodes and keyword argument value nodes of a call."""
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    args = call.child_by_field_name("arguments")
    if args is None:
        return positional, keywords
    for child in args.children:
        if child.type == "keyword_argument":
            name = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if name is not None and value is not None:
                keywords[_text(name)] = value
        elif child.is_named and child.type != "comment":
            positional.append(child)
    return positional, keywords


def convert_path(path: str) -> tuple[str, bool]:
    """``(route, catch_all)`` with Flask ``<conv:name>`` turned into ``{name}``.

    A trailing ``{name:path}`` (FastAPI) or ``<path:name>`` (Flask) takes the
    rest of the path, so it becomes the ``{...name}`` form the join treats as a
    catch-all.
    """
    catch_all = False

    def flask(match: "re.Match[str]") -> str:
        nonlocal catch_all
        if match.group("conv") == "path" and match.end() == len(path):
            catch_all = True
            return "{..." + match.group("name") + "}"
        return "{" + match.group("name") + "}"

    route = _FLASK_CONVERTER_RE.sub(flask, path)
    tail = _PATH_PARAM_RE.search(route)
    if tail:
        catch_all = True
        route = route[: tail.start()] + "{..." + tail.group("name") + "}"
    return route, catch_all


def join_path(*parts: str) -> str:
    """Join route pieces with one slash between them and a leading slash."""
    pieces = [part.strip("/") for part in parts if part and part.strip("/")]
    return "/" + "/".join(pieces)


@dataclass
class _Facts:
    #: variable -> framework, for each ``FastAPI()`` / ``Flask()`` built in the file
    apps: dict[str, str] = field(default_factory=dict)
    decorated: list[Any] = field(default_factory=list)


def _callee_name(call: Any) -> Optional[str]:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        return _text(attr) if attr is not None else None
    return None


def _note_assignment(node: Any, facts: _Facts) -> None:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or left.type != "identifier" or right.type != "call":
        return
    framework = _APP_CLASSES.get(_callee_name(right) or "")
    if framework is not None:
        facts.apps[_text(left)] = framework


def _collect(root: Any) -> _Facts:
    facts = _Facts()
    for node in _walk(root):
        if node.type == "assignment":
            _note_assignment(node, facts)
        elif node.type == "decorated_definition":
            facts.decorated.append(node)
    return facts


@dataclass(frozen=True)
class _Route:
    var: str
    methods: tuple[str, ...]
    path: str
    line: int
    end_line: int
    handler: str
    handler_line: int
    framework: str


def _methods_of(call: Any, attr: str) -> tuple[str, ...]:
    if attr in _VERBS:
        return (attr.upper(),)
    _, keywords = _call_parts(call)
    node = keywords.get("methods")
    if node is None:
        return ("GET",)
    if node.type not in ("list", "tuple", "set"):
        return ()
    values = [_literal(child) for child in node.children if child.is_named]
    if not values or any(v is None for v in values):
        return ()
    return tuple(sorted({v.upper() for v in values if v}))


def _route_of(decorator: Any, handler: Any, facts: _Facts) -> Optional[_Route]:
    call = next((c for c in decorator.children if c.type == "call"), None)
    if call is None:
        return None
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return None
    obj = fn.child_by_field_name("object")
    attr_node = fn.child_by_field_name("attribute")
    if obj is None or attr_node is None or obj.type != "identifier":
        return None
    attr, var = _text(attr_node), _text(obj)
    if attr not in _VERBS and attr not in ("route", "api_route"):
        return None
    framework = facts.apps.get(var)
    if framework is None:
        return None
    positional, keywords = _call_parts(call)
    path = _literal(positional[0] if positional else keywords.get("path"))
    methods = _methods_of(call, attr)
    name = handler.child_by_field_name("name")
    if path is None or not methods or name is None:
        return None
    return _Route(
        var, methods, path, decorator.start_point[0] + 1, call.end_point[0] + 1,
        _text(name), handler.start_point[0] + 1, framework,
    )


def _routes(facts: _Facts) -> list[_Route]:
    found: list[_Route] = []
    for node in facts.decorated:
        handler = node.child_by_field_name("definition")
        if handler is None or handler.type != "function_definition":
            continue
        for decorator in node.children:
            if decorator.type != "decorator":
                continue
            route = _route_of(decorator, handler, facts)
            if route is not None:
                found.append(route)
    return found


def _qualified(name: str, file_path: str, parent: Optional[str]) -> str:
    return f"{file_path}::{parent}.{name}" if parent else f"{file_path}::{name}"


def _handler_qn(route: _Route, file_path: str, functions: list[Any]) -> str:
    best = None
    for fn in functions:
        if fn.name == route.handler and fn.line_start >= route.line:
            if best is None or fn.line_start < best.line_start:
                best = fn
    if best is None:
        return _qualified(route.handler, file_path, None)
    return _qualified(best.name, file_path, best.parent_name)


def extract(
    root: Any,
    source: bytes,
    file_path: str,
    rel_path: str,
    nodes: list[Any],
    edges: list[Any],
    node_cls: Any,
    edge_cls: Any,
    language: str,
    is_test: bool,
) -> int:
    """Append route ``Endpoint`` nodes and ``HANDLES`` edges; return how many nodes.

    A file whose source does not mention an application class or a route
    decorator is skipped without being walked.
    """
    if is_test or any(part in _SKIP_DIRS for part in rel_path.split("/")[:-1]):
        return 0
    if not _QUICK_RE.search(source):
        return 0
    facts = _collect(root)
    routes = _routes(facts)
    emitted = 0
    functions = [n for n in nodes if n.kind in ("Function", "Test")]
    counter = 0
    for route in routes:
        if counter >= MAX_ENDPOINTS:
            break
        converted, catch_all = convert_path(route.path)
        local = join_path(converted)
        source_qn = _handler_qn(route, file_path, functions)
        for method in route.methods:
            counter += 1
            name = f"{route.handler}@{route.framework}[{counter}] {method} {local}"
            meta: dict[str, Any] = {
                "annotation": route.framework,
                "handler": route.handler,
                "http_method": method,
                "route": local,
                "framework": route.framework,
                "origin": "py",
                "py_route": True,
            }
            if catch_all:
                meta["catch_all"] = True
            nodes.append(node_cls(
                kind="Endpoint", name=name, file_path=file_path, line_start=route.line,
                line_end=route.end_line, language=language, extra=meta,
            ))
            edges.append(edge_cls(
                kind="HANDLES", source=source_qn,
                target=_qualified(name, file_path, None), file_path=file_path,
                line=route.line, extra=meta,
            ))
            emitted += 1
    return emitted
