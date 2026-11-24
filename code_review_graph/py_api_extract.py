"""Python HTTP server routes (FastAPI, Flask) as ``Endpoint`` nodes.

``@router.get("/x")``, ``@app.post(...)``, ``@bp.route("/x", methods=[...])``
and ``@app.api_route(...)`` become server ``Endpoint`` nodes with a ``HANDLES``
edge from the handler function, the same shape the JavaScript extractor gives
Express and Next.js routes, so :mod:`code_review_graph.feign_links` can join a
caller to them.

A route's full path is usually split over several files: each ``APIRouter``
(or ``Blueprint``) has its own ``prefix``, and an ``include_router`` or
``register_blueprint`` call in another file adds more.  This module records
what one file says and nothing more:

* the route's own path plus the prefix of the router it is declared on, when
  that router is built in the same file (``route``);
* one ``py_map`` node per file that builds a router or mounts one, listing the
  routers with their prefixes, the mounts and the file's imports.

:mod:`code_review_graph.py_route_prefixes` joins the files at query time.  A
prefix that is not a string literal is never guessed: such a route is left out.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "FASTAPI",
    "FLASK",
    "MAP_ROLE",
    "MAX_ENDPOINTS",
    "convert_path",
    "extract",
]

FASTAPI = "fastapi"
FLASK = "flask"

#: ``extra["role"]`` of the per-file node that lists routers, mounts and imports.
MAP_ROLE = "py_map"

MAX_ENDPOINTS = 2_000

_ROUTER_CLASSES = {
    "APIRouter": FASTAPI, "FastAPI": FASTAPI, "Blueprint": FLASK, "Flask": FLASK,
}
_PREFIX_KEYWORDS = {"APIRouter": "prefix", "Blueprint": "url_prefix"}
_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_MOUNT_CALLS = {"include_router": "prefix", "register_blueprint": "url_prefix"}
#: ``router.add_api_route("/x", handler)``-style registrations: the decorator-free
#: spelling of the same routes.  Value: (position of the handler, framework).
_ADD_CALLS = {
    "add_api_route": FASTAPI, "add_api_websocket_route": FASTAPI, "add_url_rule": FLASK,
}
_WEBSOCKET = "WEBSOCKET"
_ROUTERISH_RE = re.compile(
    r"^(?:app|api|router|bp|blueprint|\w+_router|\w+_bp|\w+_blueprint|\w+_api|\w+_app)$",
)
_SKIP_DIRS = frozenset({
    "tests", "test", "testing", "fixtures", "examples", "docs", "scripts", "migrations",
    "venv", ".venv", "site-packages", "node_modules", "build", "dist",
})
_FLASK_CONVERTER_RE = re.compile(
    r"<(?:(?P<conv>[A-Za-z_]\w*)(?:\([^)]*\))?:)?(?P<name>[A-Za-z_]\w*)>",
)
_QUICK_RE = re.compile(
    rb"APIRouter|FastAPI|Blueprint|Flask|"
    rb"\.(?:route|get|post|put|patch|delete|websocket|add_api_route|add_url_rule)\(",
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
    #: variable -> (framework, prefix or ``None`` when the prefix is not literal)
    routers: dict[str, tuple[str, Optional[str]]] = field(default_factory=dict)
    mounts: list[dict[str, Any]] = field(default_factory=list)
    #: local name -> [module, imported name or ``None``, relative level]
    imports: dict[str, list[Any]] = field(default_factory=dict)
    decorated: list[Any] = field(default_factory=list)
    adds: list[Any] = field(default_factory=list)


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


def _prefix_of(call: Any, keyword: Optional[str]) -> Optional[str]:
    """The literal prefix a router constructor or mount names; ``""`` when absent."""
    if keyword is None:
        return ""
    _, keywords = _call_parts(call)
    node = keywords.get(keyword)
    if node is None:
        return ""
    return _literal(node)


def _note_assignment(node: Any, facts: _Facts) -> None:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or left.type != "identifier" or right.type != "call":
        return
    name = _callee_name(right)
    if name in _ROUTER_CLASSES:
        facts.routers[_text(left)] = (
            _ROUTER_CLASSES[name], _prefix_of(right, _PREFIX_KEYWORDS.get(name)),
        )


def _child_ref(node: Any) -> Optional[dict[str, str]]:
    if node.type == "identifier":
        return {"var": _text(node)}
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None and obj.type == "identifier":
            return {"obj": _text(obj), "attr": _text(attr)}
    return None


def _note_mount(call: Any, facts: _Facts) -> None:
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return
    attr = fn.child_by_field_name("attribute")
    parent = fn.child_by_field_name("object")
    if attr is None or parent is None or parent.type != "identifier":
        return
    keyword = _MOUNT_CALLS.get(_text(attr))
    if keyword is None:
        return
    positional, keywords = _call_parts(call)
    first = positional[0] if positional else keywords.get(
        "router" if keyword == "prefix" else "blueprint",
    )
    ref = _child_ref(first) if first is not None else None
    if ref is None:
        return
    facts.mounts.append({
        "parent": _text(parent), "child": ref, "prefix": _prefix_of(call, keyword),
    })


def _dotted(node: Any) -> str:
    return "".join(_text(child) for child in node.children)


def _note_import_from(node: Any, facts: _Facts) -> None:
    module = node.child_by_field_name("module_name")
    if module is None:
        return
    level = 0
    dotted = ""
    if module.type == "relative_import":
        for child in module.children:
            if child.type == "import_prefix":
                level = _text(child).count(".")
            elif child.type == "dotted_name":
                dotted = _dotted(child)
    else:
        dotted = _dotted(module)
    for name in node.children_by_field_name("name"):
        if name.type == "aliased_import":
            real = name.child_by_field_name("name")
            alias = name.child_by_field_name("alias")
            if real is not None and alias is not None:
                facts.imports[_text(alias)] = [dotted, _dotted(real), level]
        elif name.type == "dotted_name":
            facts.imports[_dotted(name)] = [dotted, _dotted(name), level]


def _note_import(node: Any, facts: _Facts) -> None:
    for name in node.children_by_field_name("name"):
        if name.type == "aliased_import":
            real = name.child_by_field_name("name")
            alias = name.child_by_field_name("alias")
            if real is not None and alias is not None:
                facts.imports[_text(alias)] = [_dotted(real), None, 0]


def _collect(root: Any) -> _Facts:
    facts = _Facts()
    for node in _walk(root):
        kind = node.type
        if kind == "assignment":
            _note_assignment(node, facts)
        elif kind == "call":
            _note_mount(node, facts)
            if _callee_name(node) in _ADD_CALLS:
                facts.adds.append(node)
        elif kind == "import_from_statement":
            _note_import_from(node, facts)
        elif kind == "import_statement":
            _note_import(node, facts)
        elif kind == "decorated_definition":
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
    #: Registered by a call (``add_api_route``), so the handler is looked up by name.
    lookup: bool = False


def _methods_of(call: Any, attr: str) -> tuple[str, ...]:
    if attr in _VERBS:
        return (attr.upper(),)
    if attr in ("websocket", "add_api_websocket_route"):
        return (_WEBSOCKET,)
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


def _is_router(var: str, facts: _Facts) -> bool:
    """Whether ``var`` is a router object, decided from how it came to be.

    A name built here by ``APIRouter``/``FastAPI``/``Blueprint``/``Flask`` is one.
    An imported name is one only when its own name or the name it was imported
    under reads as a router (``oauth_router``, ``app``, ``bp``); an imported
    ``cache`` or ``client`` is not, so ``@cache.get(...)`` is left alone.
    """
    if var in facts.routers:
        return True
    spec = facts.imports.get(var)
    if spec is not None:
        real = spec[1] if spec[1] else var
        return bool(_ROUTERISH_RE.match(var) or _ROUTERISH_RE.match(str(real)))
    return bool(_ROUTERISH_RE.match(var))


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
    if attr not in _VERBS and attr not in ("route", "api_route", "websocket"):
        return None
    if not _is_router(var, facts):
        return None
    positional, keywords = _call_parts(call)
    path = _literal(positional[0] if positional else keywords.get("path"))
    methods = _methods_of(call, attr)
    name = handler.child_by_field_name("name")
    if path is None or not methods or name is None:
        return None
    framework = (
        facts.routers[var][0] if var in facts.routers
        else (FLASK if attr == "route" else FASTAPI)
    )
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
    for call in facts.adds:
        route = _add_route(call, facts)
        if route is not None:
            found.append(route)
    return found


def _handler_name(node: Any) -> Optional[str]:
    """``fn`` for ``fn``, ``View`` for ``View.as_view("n")``; else ``None``."""
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node)
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            obj = fn.child_by_field_name("object")
            if attr is not None and obj is not None and _text(attr) == "as_view" \
                    and obj.type == "identifier":
                return _text(obj)
    return None


def _add_route(call: Any, facts: _Facts) -> Optional[_Route]:
    """A route registered by ``router.add_api_route("/x", handler, methods=[...])``."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return None
    obj = fn.child_by_field_name("object")
    attr_node = fn.child_by_field_name("attribute")
    if obj is None or attr_node is None or obj.type != "identifier":
        return None
    attr, var = _text(attr_node), _text(obj)
    if not _is_router(var, facts):
        return None
    positional, keywords = _call_parts(call)
    path = _literal(positional[0] if positional else keywords.get("path", keywords.get("rule")))
    if attr == "add_url_rule":
        target = keywords.get("view_func") or (positional[2] if len(positional) > 2 else None)
    else:
        target = keywords.get("endpoint") or (positional[1] if len(positional) > 1 else None)
    handler = _handler_name(target)
    methods = _methods_of(call, attr)
    if path is None or handler is None or not methods:
        return None
    line = call.start_point[0] + 1
    return _Route(
        var, methods, path, line, call.end_point[0] + 1, handler, 0,
        facts.routers[var][0] if var in facts.routers else _ADD_CALLS[attr], lookup=True,
    )


def _qualified(name: str, file_path: str, parent: Optional[str]) -> str:
    return f"{file_path}::{parent}.{name}" if parent else f"{file_path}::{name}"


def _handler_qn(route: _Route, file_path: str, functions: list[Any]) -> Optional[str]:
    if route.lookup:
        same = [f for f in functions if f.name == route.handler]
        if len(same) != 1:
            return None
        return _qualified(same[0].name, file_path, same[0].parent_name)
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
    """Append route ``Endpoint`` nodes, ``HANDLES`` edges and a ``py_map`` node.

    Returns how many nodes were appended.  A file whose source does not mention
    a router class or a route decorator is skipped without being walked.
    """
    if is_test or any(part in _SKIP_DIRS for part in rel_path.split("/")[:-1]):
        return 0
    if not _QUICK_RE.search(source):
        return 0
    facts = _collect(root)
    routes = _routes(facts)
    emitted = 0
    functions = [
        n for n in nodes if n.kind in ("Function", "Test") or (n.kind == "Class")
    ]
    counter = 0
    for route in routes:
        if counter >= MAX_ENDPOINTS:
            break
        declared = facts.routers.get(route.var)
        prefix = declared[1] if declared else ""
        converted, catch_all = convert_path(route.path)
        source_qn = _handler_qn(route, file_path, functions)
        if source_qn is None:
            continue
        for method in route.methods:
            counter += 1
            local = join_path(prefix or "", converted) if prefix is not None else converted
            name = f"{route.handler}@{route.framework}[{counter}] {method} {local}"
            meta: dict[str, Any] = {
                "annotation": route.framework,
                "handler": route.handler,
                "http_method": method,
                "route": local,
                "framework": route.framework,
                "origin": "py",
                "py_route": True,
                "router_var": route.var,
                "route_path": converted,
            }
            if prefix is None:
                meta["prefix_dynamic"] = True
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
    if facts.routers or facts.mounts or any(r.var in facts.imports for r in routes):
        nodes.append(node_cls(
            kind="Endpoint", name=f"{file_path}@py-routes-map", file_path=file_path,
            line_start=1, line_end=max(1, source.count(b"\n") + 1), language=language,
            extra={
                "role": MAP_ROLE, "origin": "py",
                "routers": {k: list(v) for k, v in facts.routers.items()},
                "mounts": facts.mounts[:MAX_ENDPOINTS],
                "imports": facts.imports,
            },
        ))
        emitted += 1
    return emitted
