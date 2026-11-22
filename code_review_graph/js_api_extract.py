"""Parse-time HTTP routes and API calls in JavaScript and TypeScript modules.

Three things are read out of one parsed file and recorded as ``Endpoint``
nodes, in the same shape the Spring Feign and controller code uses:

* **Client calls** with a statically known route: ``axios`` (the default
  export and instances made with ``axios.create``), ``fetch``, ``$.ajax`` and
  friends, and Ember ``ajax``.  Each is a client ``Endpoint`` (``role`` is
  ``"client"``) with a ``REQUESTS`` edge from the enclosing function.
* **Ember Data** models: the routes the store derives from the adapter's
  ``namespace`` and the model's name (see :func:`ember_model_routes`).
* **Server routes**: the Next.js app router (``route.ts``) and pages router
  (``pages/api``), and Express, Koa, Fastify or Hono style
  ``app.get('/x', handler)``.  Each is a server ``Endpoint`` with a
  ``HANDLES`` edge from the handler.

Nothing is guessed.  A URL that is not a string literal, a template literal, a
concatenation of literals or a same-file ``const`` string is recorded as
*unmatchable* with a reason (when the call is certainly an HTTP call) or left
out; ``tools/registry_tools.py`` joins the rest at query time.

Known limits, also in ``docs/COMMANDS.md``:

* An instance imported from another file has no known ``baseURL``; its
  routes are recorded as written.  Only a literal ``baseURL`` given to
  ``axios.create`` in the same file (or ``instance.defaults.baseURL = '...'``)
  is applied.
* Test files are skipped.
* ``app.use('/prefix', router)`` mounting is not followed; a router's routes
  are recorded relative to the router.
* Plural names for Ember Data follow ember-inflector for regular nouns plus a
  short irregular list (:data:`_IRREGULAR_PLURALS`); an unusual noun can be
  wrong, which is why those edges are ``INFERRED`` at 0.6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import ember_extract

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .parser import EdgeInfo, NodeInfo

#: Edge kind of an outbound call (same value as ``SPRING_FEIGN_EDGE_KIND``).
REQUESTS_EDGE = "REQUESTS"

#: ``extra["client"]`` of every client endpoint written here; the Feign
#: reader looks for ``"feign"`` and so ignores these.
CLIENT_TAG = "js_http"

#: Most nodes one file may contribute to a walk.
MAX_WALK_NODES = 400_000
#: Most endpoints one file may produce.
MAX_ENDPOINTS = 2_000

#: Confidence of an Ember Data route (the plural form is a convention).
EMBER_DATA_CONFIDENCE = 0.6

#: Rendered in place of a route piece that cannot be resolved.
UNRESOLVED = "<unresolved>"

_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_SERVER_VERBS = _HTTP_VERBS | {"all"}
_NEXT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
#: Verbs whose axios call takes the config as the third argument.
_BODY_VERBS = frozenset({"post", "put", "patch"})

#: Cheap pre-filter: a file with none of these cannot hold a call or route.
_QUICK_RE = re.compile(
    rb"axios|fetch\s*\(|\bajax\b|\$\.|jQuery|getJSON"
    rb"|\.(?:get|post|put|patch|delete|all|route)\s*\(|\bapi\b|[Cc]lient|[Hh]ttp"
)
_SERVER_EVIDENCE_RE = re.compile(
    rb"""(?:require\(|from\s+|import\s+)['"](?:express|koa|@koa/router|koa-router|fastify"""
    rb"""|hono|restify|@hapi/hapi|polka|connect)['"/]"""
)
_ROUTER_NAME_RE = re.compile(r"^(?:app|server|router|\w*[Rr]outer|\w*[Aa]pp|fastify|api)$")
_INSTANCE_NAME_RE = re.compile(
    r"^[$_]*(?:api|http|axios|request|fetcher|client|ajax)|(?:Api|Http|Axios|Client|Request|Fetcher|Ajax)$"
)
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//")
_INTERNAL_IMPORT_PREFIXES = (".", "/", "@/", "~/", "#", "$", "src/", "app/", "lib/")

#: Directories whose files are test doubles, stubs or demos, never real routes or calls.
_SKIP_DIRS = frozenset({
    "mocks", "__mocks__", "mock", "fixtures", "__fixtures__", "stories", ".storybook",
    "e2e", "cypress", "playwright", "node_modules", "dist", "build",
})

_FUNCTION_KINDS = frozenset({"Function", "Test"})
_UNWRAP = frozenset({
    "parenthesized_expression", "as_expression", "satisfies_expression",
    "non_null_expression", "type_assertion",
})


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _walk(root: Any):
    stack = [root]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > MAX_WALK_NODES:
            return
        yield node
        stack.extend(reversed(node.children))


def _unwrap(node: Any) -> Any:
    while node is not None and node.type in _UNWRAP:
        inner = node.child_by_field_name("expression")
        if inner is None:
            named = [c for c in node.children if c.is_named]
            inner = named[0] if named else None
        node = inner
    return node


def _callee(call: Any) -> Any:
    """The called expression of a call.

    The TypeScript grammar reads ``await client.get<T>(url)`` as a call whose
    callee is ``await client.get``; the ``await`` is dropped here.
    """
    func = _unwrap(call.child_by_field_name("function"))
    if func is not None and func.type == "await_expression":
        named = [c for c in func.children if c.is_named]
        func = _unwrap(named[0]) if named else None
    return func


def _chain(node: Any) -> list[str]:
    """``a.b.c`` -> ``["a", "b", "c"]``; ``[]`` for anything that is not a plain chain."""
    parts: list[str] = []
    current = node
    while current is not None and current.type == "member_expression":
        prop = current.child_by_field_name("property")
        if prop is None:
            return []
        parts.append(_text(prop))
        current = current.child_by_field_name("object")
    if current is None or current.type not in ("identifier", "this"):
        return []
    parts.append(_text(current))
    parts.reverse()
    return parts


def _string_literal(node: Any) -> Optional[str]:
    """Text of a plain string literal, else ``None``."""
    if node is None or node.type != "string":
        return None
    raw = _text(node)
    return raw[1:-1] if len(raw) >= 2 and raw[0] in "'\"" else None


def _args(call: Any) -> list[Any]:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return []
    return [c for c in arguments.children if c.is_named and c.type != "comment"]


# ---------------------------------------------------------------------------
# Static string evaluation
# ---------------------------------------------------------------------------


def static_text(node: Any, consts: dict[str, str], head: bool = True) -> Optional[str]:
    """Fold a URL expression into text, ``{param}`` for each unknown piece.

    Handles string literals, template literals, ``+`` concatenation and
    identifiers bound to a same-file ``const`` string.  Returns ``None`` when
    the expression cannot be read, including when the *start* is unknown
    (``${base}/x`` or ``base + '/x'``): a leading unknown piece may be a host
    or any prefix, so the route is not guessed.
    """
    node = _unwrap(node)
    if node is None:
        return None
    kind = node.type
    if kind == "string":
        return _string_literal(node)
    if kind == "template_string":
        out: list[str] = []
        first = True
        for child in node.children:
            if child.type in ("`",):
                continue
            if child.type == "template_substitution":
                inner = [c for c in child.children if c.is_named]
                known: Optional[str] = None
                if len(inner) == 1:
                    expr = _unwrap(inner[0])
                    if expr is not None and expr.type == "string":
                        known = _string_literal(expr)
                    elif expr is not None and expr.type == "identifier":
                        known = consts.get(_text(expr))
                if known is not None:
                    out.append(known)
                elif head and first:
                    return None
                else:
                    out.append("{param}")
            else:
                out.append(_text(child))
            first = False
        return "".join(out)
    if kind == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is None or _text(op) != "+":
            return None
        left = static_text(node.child_by_field_name("left"), consts, head)
        if left is None:
            return None
        right_node = node.child_by_field_name("right")
        right = static_text(right_node, consts, head=False)
        if right is None:
            # An unknown trailing piece is one path parameter, like ``${x}``.
            right = "{param}"
        return left + right
    if kind == "identifier":
        name = _text(node)
        if name in consts:
            return consts[name]
        return None if head else "{param}"
    return None if head else "{param}"


def split_route(text: str) -> tuple[str, bool]:
    """``(route without query/fragment, absolute)`` for folded URL text."""
    absolute = bool(_ABSOLUTE_RE.match(text))
    for mark in ("?", "#"):
        cut = text.find(mark)
        if cut != -1:
            text = text[:cut]
    return text, absolute


def join_base(base: Optional[str], path: str) -> str:
    """Join a literal ``baseURL`` and a path the way axios does (one slash between)."""
    if not base:
        return path
    if _ABSOLUTE_RE.match(path):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


# ---------------------------------------------------------------------------
# Client-call recognition
# ---------------------------------------------------------------------------


@dataclass
class _Config:
    """What an object-literal request config says, by key."""

    url: Optional[Any] = None
    method: Optional[Any] = None
    base_url: Optional[Any] = None
    has_spread: bool = False
    present: bool = False


def _object_config(node: Any) -> _Config:
    cfg = _Config()
    node = _unwrap(node)
    if node is None or node.type != "object":
        return cfg
    cfg.present = True
    for child in node.children:
        if child.type == "spread_element":
            cfg.has_spread = True
        elif child.type == "pair":
            key = child.child_by_field_name("key")
            value = child.child_by_field_name("value")
            name = _string_literal(key) if key is not None and key.type == "string" else _text(key)
            if name in ("url", "uri"):
                cfg.url = value
            elif name in ("method", "type"):
                cfg.method = value
            elif name == "baseURL":
                cfg.base_url = value
        elif child.type == "shorthand_property_identifier":
            name = _text(child)
            if name == "url":
                cfg.url = child
            elif name in ("method", "type"):
                cfg.method = child
            elif name == "baseURL":
                cfg.base_url = child
    return cfg


@dataclass
class ClientHit:
    """One recognised HTTP call, before it becomes a node."""

    kind: str
    method: str
    route: str
    line: int
    end_line: int
    route_dynamic: bool = False
    method_dynamic: bool = False
    absolute_url: bool = False
    base_url: Optional[str] = None
    base_url_dynamic: bool = False
    instance: Optional[str] = None
    receiver_inferred: bool = False


class _FileFacts:
    """Declarations one pass collects before the calls are read."""

    def __init__(self) -> None:
        self.consts: dict[str, str] = {}
        #: local axios names -> (literal baseURL or None, baseURL given but not literal)
        self.instances: dict[str, tuple[Optional[str], bool]] = {}
        self.axios_names: set[str] = {"axios"}
        self.imports: dict[str, str] = {}
        self.calls: list[Any] = []
        self.defaults_base: dict[str, str] = {}


def _import_bindings(root: Any, facts: _FileFacts) -> None:
    for child in root.children:
        if child.type != "import_statement":
            continue
        source = _string_literal(child.child_by_field_name("source"))
        if source is None:
            continue
        for node in _walk(child):
            if node.type == "import_clause":
                for sub in node.children:
                    if sub.type == "identifier":
                        facts.imports[_text(sub)] = source
                    elif sub.type == "named_imports":
                        for spec in sub.children:
                            if spec.type == "import_specifier":
                                alias = spec.child_by_field_name("alias")
                                name = spec.child_by_field_name("name")
                                local = alias if alias is not None else name
                                if local is not None:
                                    facts.imports[_text(local)] = source
                    elif sub.type == "namespace_import":
                        ident = [c for c in sub.children if c.type == "identifier"]
                        if ident:
                            facts.imports[_text(ident[0])] = source
    for local, source in facts.imports.items():
        if source == "axios":
            facts.axios_names.add(local)


def _collect(root: Any, facts: _FileFacts, source: bytes) -> None:
    _import_bindings(root, facts)
    for node in _walk(root):
        kind = node.type
        if kind == "call_expression":
            facts.calls.append(node)
        elif kind == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = _unwrap(node.child_by_field_name("value"))
            if name_node is None or name_node.type != "identifier" or value is None:
                continue
            name = _text(name_node)
            if value.type == "call_expression":
                made = _chain(value.child_by_field_name("function"))
                if len(made) == 2 and made[0] in facts.axios_names and made[1] == "create":
                    cfg = _object_config(next(iter(_args(value)), None))
                    base_literal: Optional[str] = None
                    dynamic = False
                    if cfg.base_url is not None:
                        base_literal = _string_literal(_unwrap(cfg.base_url))
                        dynamic = base_literal is None
                    facts.instances[name] = (base_literal, dynamic)
                    facts.axios_names.add(name)
            elif value.type in ("string", "template_string", "binary_expression"):
                # Only a top-level ``const`` is a safe constant.
                parent = node.parent
                if (
                    parent is not None
                    and parent.type == "lexical_declaration"
                    and _text(parent.children[0]) == "const"
                    and parent.parent is not None
                    and parent.parent.type in ("program", "export_statement")
                ):
                    text = static_text(value, facts.consts)
                    if text is not None and "{param}" not in text:
                        facts.consts[name] = text
        elif kind == "assignment_expression":
            left = _chain(node.child_by_field_name("left"))
            if len(left) == 3 and left[1:] == ["defaults", "baseURL"]:
                literal = _string_literal(_unwrap(node.child_by_field_name("right")))
                if literal is not None:
                    facts.defaults_base[left[0]] = literal


def _is_internal_import(spec: str) -> bool:
    return spec.startswith(_INTERNAL_IMPORT_PREFIXES)


def _method_from(node: Any) -> tuple[str, bool]:
    """(METHOD, dynamic) from a ``method:`` value node."""
    literal = _string_literal(_unwrap(node)) if node is not None else None
    if literal is None:
        return "UNKNOWN", True
    return literal.upper(), False


def _build_hit(
    kind: str,
    method: str,
    method_dynamic: bool,
    url_node: Any,
    call: Any,
    facts: _FileFacts,
    base: Optional[str] = None,
    base_dynamic: bool = False,
    instance: Optional[str] = None,
    inferred: bool = False,
) -> Optional[ClientHit]:
    folded = static_text(url_node, facts.consts) if url_node is not None else None
    line = call.start_point[0] + 1
    end_line = call.end_point[0] + 1
    if folded is None:
        if inferred:
            return None
        return ClientHit(
            kind=kind, method=method, route=UNRESOLVED, line=line, end_line=end_line,
            route_dynamic=True, method_dynamic=method_dynamic, base_url=base,
            base_url_dynamic=base_dynamic, instance=instance,
        )
    path, absolute = split_route(folded)
    route = join_base(base, path) if not absolute else path
    base_absolute = bool(base and _ABSOLUTE_RE.match(base))
    if base_absolute and not absolute:
        # ``baseURL: 'https://host/api'`` names another origin: keep the path
        # but never join it to a route of ours.
        absolute = True
    return ClientHit(
        kind=kind, method=method, route=route, line=line, end_line=end_line,
        method_dynamic=method_dynamic, absolute_url=absolute, base_url=base,
        base_url_dynamic=base_dynamic, instance=instance, receiver_inferred=inferred,
    )


def _client_hit(call: Any, facts: _FileFacts) -> Optional[ClientHit]:
    func = _callee(call)
    chain = _chain(func)
    if not chain:
        return None
    args = _args(call)
    head = chain[0]

    # fetch(url, { method })
    if chain in (["fetch"], ["window", "fetch"], ["globalThis", "fetch"], ["self", "fetch"]):
        if not args:
            return None
        cfg = _object_config(args[1]) if len(args) > 1 else _Config()
        method, dynamic = "GET", False
        if len(args) > 1 and (not cfg.present or cfg.has_spread):
            method, dynamic = "UNKNOWN", True
        elif cfg.method is not None:
            method, dynamic = _method_from(cfg.method)
        return _build_hit("fetch", method, dynamic, args[0], call, facts)

    # jQuery: $.ajax / $.getJSON / $.get / $.post (also Ember.$)
    if len(chain) >= 2 and chain[-2] in ("$", "jQuery") and chain[-1] in (
        "ajax", "getJSON", "get", "post",
    ):
        verb = chain[-1]
        if not args:
            return None
        if verb == "ajax":
            first = _unwrap(args[0])
            if first is not None and first.type == "object":
                cfg = _object_config(first)
                url_node = cfg.url
            else:
                url_node = args[0]
                cfg = _object_config(args[1]) if len(args) > 1 else _Config()
            if url_node is None:
                return None
            method, dynamic = ("GET", False)
            if cfg.method is not None:
                method, dynamic = _method_from(cfg.method)
            if cfg.has_spread:
                method, dynamic = "UNKNOWN", True
            return _build_hit("jquery", method, dynamic, url_node, call, facts)
        return _build_hit(
            "jquery", "POST" if verb == "post" else "GET", False, args[0], call, facts,
        )

    # Ember: this.ajax(url, 'GET', opts), this.ajax.request(url, opts), ajax(url)
    ember_call = (
        chain == ["this", "ajax"]
        or (chain == ["ajax"] and "ajax" in facts.imports)
        or (len(chain) == 3 and chain[:2] == ["this", "ajax"] and (
            chain[2] == "request" or chain[2] in _HTTP_VERBS or chain[2] == "del"))
        or (len(chain) == 2 and chain[0] == "ajax" and "ajax" in facts.imports and (
            chain[1] == "request" or chain[1] in _HTTP_VERBS or chain[1] == "del"))
    )
    if ember_call and args:
        verb = chain[-1]
        method, dynamic = "GET", False
        if verb in _HTTP_VERBS or verb == "del":
            method = "DELETE" if verb == "del" else verb.upper()
        else:
            second = _unwrap(args[1]) if len(args) > 1 else None
            if second is not None and second.type == "string":
                method, dynamic = _method_from(second)
            elif second is not None:
                cfg = _object_config(second)
                if cfg.method is not None:
                    method, dynamic = _method_from(cfg.method)
                elif not cfg.present:
                    method, dynamic = "UNKNOWN", True
        return _build_hit("ember-ajax", method, dynamic, args[0], call, facts)

    # axios, axios instances, imported instances
    inferred = False
    instance: Optional[str] = None
    base: Optional[str] = None
    base_dynamic = False
    if head in facts.axios_names and head not in ("this",):
        if head in facts.instances:
            base, base_dynamic = facts.instances[head]
            instance = head
        if head in facts.defaults_base and base is None:
            base = facts.defaults_base[head]
        if head not in ("axios",) and head not in facts.instances:
            instance = head
    elif (
        len(chain) == 2
        and head in facts.imports
        and _is_internal_import(facts.imports[head])
        and _INSTANCE_NAME_RE.search(head)
        and chain[1] in _HTTP_VERBS
    ):
        inferred, instance = True, head
    else:
        return None

    if len(chain) == 1 or chain[1] == "request":
        if len(chain) > 2 or not args:
            return None
        cfg = _object_config(args[0])
        if not cfg.present or cfg.url is None:
            return None
        method, dynamic = ("GET", False)
        if cfg.method is not None:
            method, dynamic = _method_from(cfg.method)
        if cfg.has_spread:
            method, dynamic = "UNKNOWN", True
        if cfg.base_url is not None:
            literal = _string_literal(_unwrap(cfg.base_url))
            base, base_dynamic = literal, literal is None
        return _build_hit(
            "axios", method, dynamic, cfg.url, call, facts, base, base_dynamic, instance,
        )
    if len(chain) == 2 and chain[1] in _HTTP_VERBS:
        if not args:
            return None
        verb = chain[1]
        cfg_index = 2 if verb in _BODY_VERBS else 1
        if len(args) > cfg_index:
            cfg = _object_config(args[cfg_index])
            if cfg.base_url is not None:
                literal = _string_literal(_unwrap(cfg.base_url))
                base, base_dynamic = literal, literal is None
        return _build_hit(
            "axios", verb.upper(), False, args[0], call, facts, base, base_dynamic,
            instance, inferred,
        )
    return None


# ---------------------------------------------------------------------------
# Server routes
# ---------------------------------------------------------------------------


@dataclass
class ServerHit:
    """One recognised served route, before it becomes a node."""

    framework: str
    method: str
    route: str
    line: int
    end_line: int
    handler: Optional[str] = None
    catch_all: bool = False
    optional_catch_all: bool = False


def _segment_to_route(segment: str) -> tuple[Optional[str], bool, bool]:
    """Next.js directory segment -> (route piece or ``None`` to drop, catch-all, optional)."""
    if segment.startswith("[[...") and segment.endswith("]]"):
        return "{..." + segment[5:-2] + "}", True, True
    if segment.startswith("[...") and segment.endswith("]"):
        return "{..." + segment[4:-1] + "}", True, False
    if segment.startswith("[") and segment.endswith("]"):
        return "{" + segment[1:-1] + "}", False, False
    if segment.startswith("(") or segment.startswith("@"):
        return None, False, False
    return segment, False, False


def next_route_path(parts: list[str]) -> Optional[tuple[str, bool, bool]]:
    """Route of a Next.js route file from its repo-relative directory parts."""
    pieces: list[str] = []
    catch_all = optional = False
    for segment in parts:
        piece, is_catch, is_optional = _segment_to_route(segment)
        if piece is None:
            continue
        pieces.append(piece)
        catch_all = catch_all or is_catch
        optional = optional or is_optional
    return "/" + "/".join(pieces), catch_all, optional


def _next_hits(rel: str, root: Any, source: bytes) -> list[ServerHit]:
    parts = rel.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    ext = parts[-1].rsplit(".", 1)[-1]
    if ext not in ("ts", "tsx", "js", "jsx", "mjs", "cjs") or "node_modules" in parts:
        return []
    dirs = parts[:-1]
    hits: list[ServerHit] = []
    if stem == "route" and "app" in dirs:
        base = dirs.index("app")
        route, catch_all, optional = next_route_path(dirs[base + 1:]) or ("/", False, False)
        for child in root.children:
            if child.type != "export_statement":
                continue
            for name, handler in _exported_names(child):
                if name in _NEXT_METHODS:
                    hits.append(ServerHit(
                        "nextjs-app", name, route, child.start_point[0] + 1,
                        child.end_point[0] + 1, handler, catch_all, optional,
                    ))
        return hits
    for index in range(len(dirs) - 1):
        if dirs[index] == "pages" and dirs[index + 1] == "api":
            tail = dirs[index + 1:] + ([] if stem == "index" else [stem])
            if stem.startswith("_"):
                return []
            route, catch_all, optional = next_route_path(tail) or ("/", False, False)
            handler = _default_export_name(root)
            methods = _pages_methods(source)
            line = 1
            for method in methods or ["ANY"]:
                hits.append(ServerHit(
                    "nextjs-pages", method, route, line, root.end_point[0] + 1,
                    handler, catch_all, optional,
                ))
            return hits
    return hits


def _exported_names(export: Any) -> list[tuple[str, Optional[str]]]:
    """``(exported name, local function name)`` pairs of one ``export`` statement."""
    found: list[tuple[str, Optional[str]]] = []
    decl = export.child_by_field_name("declaration")
    if decl is not None:
        if decl.type in ("function_declaration", "generator_function_declaration"):
            name = decl.child_by_field_name("name")
            if name is not None:
                found.append((_text(name), _text(name)))
        elif decl.type == "lexical_declaration":
            for sub in decl.children:
                if sub.type == "variable_declarator":
                    name = sub.child_by_field_name("name")
                    if name is not None and name.type == "identifier":
                        found.append((_text(name), _text(name)))
    for child in export.children:
        if child.type == "export_clause":
            for spec in child.children:
                if spec.type == "export_specifier":
                    name = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    if name is not None:
                        found.append((_text(alias if alias is not None else name), _text(name)))
    return found


def _default_export_name(root: Any) -> Optional[str]:
    for child in root.children:
        if child.type != "export_statement":
            continue
        if not any(c.type == "default" for c in child.children):
            continue
        decl = child.child_by_field_name("declaration")
        if decl is not None and decl.type in ("function_declaration", "function_expression"):
            name = decl.child_by_field_name("name")
            return _text(name) if name is not None else None
        value = child.child_by_field_name("value")
        if value is not None and value.type == "identifier":
            return _text(value)
    return None


_PAGES_METHOD_RE = re.compile(
    rb"""(?:method\s*(?:===|==)\s*|case\s+)['"](GET|POST|PUT|PATCH|DELETE)['"]"""
)


def _pages_methods(source: bytes) -> list[str]:
    seen: list[str] = []
    for match in _PAGES_METHOD_RE.finditer(source):
        method = match.group(1).decode()
        if method not in seen:
            seen.append(method)
    return seen


def _express_route(path: str) -> tuple[str, bool]:
    """Express ``:id`` to ``{id}``; a ``*`` segment is a catch-all."""
    catch_all = False
    out: list[str] = []
    for segment in path.split("/"):
        if segment.startswith(":"):
            name = re.sub(r"[^A-Za-z0-9_].*$", "", segment[1:]) or "param"
            out.append("{" + name + "}")
        elif segment in ("*", "(.*)", "*path"):
            out.append("{...rest}")
            catch_all = True
        else:
            out.append(segment)
    return "/".join(out), catch_all


def _server_call_hits(call: Any, facts: _FileFacts) -> list[ServerHit]:
    func = _callee(call)
    if func is None or func.type != "member_expression":
        return []
    prop = func.child_by_field_name("property")
    verb = _text(prop)
    if verb not in _SERVER_VERBS and verb != "route":
        return []
    obj = _unwrap(func.child_by_field_name("object"))
    args = _args(call)
    line = call.start_point[0] + 1
    end_line = call.end_point[0] + 1

    # fastify.route({ method, url, handler })
    first_arg = _unwrap(args[0]) if args else None
    if verb == "route" and first_arg is not None and first_arg.type == "object":
        return _object_route_hits(first_arg, line, end_line)

    # router.route('/x').get(h).post(h): the verb call's object is a route() call
    if verb in _SERVER_VERBS and obj is not None and obj.type == "call_expression":
        inner = _chain_route_path(obj)
        if inner is None or not args:
            return []
        route, catch_all = _express_route(inner)
        return [ServerHit(
            "express", "ANY" if verb == "all" else verb.upper(), route, line, end_line,
            _handler_name(args[-1]), catch_all,
        )]
    if verb == "route":
        return []

    owner = _chain(obj) if obj is not None else []
    if len(owner) != 1 or owner[0] in facts.axios_names or not _ROUTER_NAME_RE.match(owner[0]):
        return []
    if len(args) < 2:
        return []
    path = _string_literal(_unwrap(args[0]))
    if path is None or not path.startswith("/"):
        return []
    handler_node = _unwrap(args[-1])
    if handler_node is None or handler_node.type not in (
        "arrow_function", "function_expression", "function", "identifier",
        "member_expression", "call_expression", "await_expression",
    ):
        return []
    route, catch_all = _express_route(path)
    return [ServerHit(
        "express", "ANY" if verb == "all" else verb.upper(), route, line, end_line,
        _handler_name(args[-1]), catch_all,
    )]


def _chain_route_path(call: Any) -> Optional[str]:
    """Path of a ``x.route('/p')`` call, else ``None``; verbs chained on it recurse."""
    func = _callee(call)
    if func is None or func.type != "member_expression":
        return None
    prop = _text(func.child_by_field_name("property"))
    if prop == "route":
        args = _args(call)
        return _string_literal(_unwrap(args[0])) if args else None
    if prop in _SERVER_VERBS:
        inner = _unwrap(func.child_by_field_name("object"))
        if inner is not None and inner.type == "call_expression":
            return _chain_route_path(inner)
    return None


def _object_route_hits(obj: Any, line: int, end_line: int) -> list[ServerHit]:
    method_node = url_node = handler_node = None
    for child in obj.children:
        if child.type != "pair":
            continue
        key = _text(child.child_by_field_name("key"))
        value = child.child_by_field_name("value")
        if key == "method":
            method_node = value
        elif key in ("url", "path"):
            url_node = value
        elif key == "handler":
            handler_node = value
    path = _string_literal(_unwrap(url_node)) if url_node is not None else None
    if path is None or not path.startswith("/"):
        return []
    methods: list[str] = []
    value = _unwrap(method_node)
    if value is not None and value.type == "string":
        methods = [(_string_literal(value) or "").upper()]
    elif value is not None and value.type == "array":
        methods = [
            (_string_literal(_unwrap(c)) or "").upper() for c in value.children if c.is_named
        ]
    methods = [m for m in methods if m]
    if not methods:
        return []
    route, catch_all = _express_route(path)
    handler = _handler_name(handler_node) if handler_node is not None else None
    return [
        ServerHit("express", m, route, line, end_line, handler, catch_all) for m in methods
    ]


def _handler_name(node: Any) -> Optional[str]:
    node = _unwrap(node)
    if node is not None and node.type == "identifier":
        return _text(node)
    return None


# ---------------------------------------------------------------------------
# Ember Data
# ---------------------------------------------------------------------------

#: Words ember-inflector would not pluralise by the plain rules.
_IRREGULAR_PLURALS = {
    "person": "people", "man": "men", "woman": "women", "child": "children",
    "mouse": "mice", "foot": "feet", "tooth": "teeth", "goose": "geese", "ox": "oxen",
}
_UNCOUNTABLE = frozenset({
    "equipment", "information", "rice", "money", "species", "series", "fish", "sheep",
    "jeans", "police", "news", "data", "metadata",
})
_VOWELS = "aeiou"


def pluralize_word(word: str) -> str:
    """Plural of one lower-case word: regular rules plus a short irregular list.

    ``y`` after a consonant becomes ``ies`` (``category`` -> ``categories``),
    ``s``, ``x``, ``z``, ``ch`` and ``sh`` take ``es`` (``status`` ->
    ``statuses``), everything else takes ``s``.  Uncountable and irregular
    nouns come from small tables; anything else irregular (``leaf``,
    ``criterion``) is plain ``+s`` and so wrong.
    """
    if not word:
        return word
    lower = word.lower()
    if lower in _UNCOUNTABLE:
        return word
    if lower in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[lower]
    if lower.endswith("y") and len(lower) > 1 and lower[-2] not in _VOWELS:
        return word[:-1] + "ies"
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def camelize(name: str) -> str:
    """``blog-post`` -> ``blogPost`` (a ``/`` namespace separator is kept)."""
    def one(piece: str) -> str:
        words = [w for w in re.split(r"[-_ ]+", piece) if w]
        if not words:
            return piece
        rest = "".join(w[:1].upper() + w[1:] for w in words[1:])
        return words[0][:1].lower() + words[0][1:] + rest
    return "/".join(one(p) for p in name.split("/"))


def pluralize_model(model_name: str, dasherized: bool) -> str:
    """Path segment ``pathForType`` returns for a model name.

    ``RESTAdapter`` camelizes then pluralizes (``blogPosts``);
    ``JSONAPIAdapter`` pluralizes then dasherizes (``blog-posts``).  Only the
    last word of the name is pluralised.
    """
    base = ember_extract.dasherize(model_name.split("/")[-1])
    words = base.split("-")
    words[-1] = pluralize_word(words[-1])
    plural = "-".join(words)
    prefix = "/".join(model_name.split("/")[:-1])
    if not dasherized:
        plural = camelize(plural)
    return f"{prefix}/{plural}" if prefix else plural


_NAMESPACE_RE = re.compile(r"""\bnamespace\s*[:=]\s*(?:(['"])([^'"]*)\1|(\S))""")
_PATH_FOR_TYPE_RE = re.compile(r"\bpathForType\b")
_PATH_FOR_TYPE_LITERAL_RE = re.compile(
    r"""pathForType[^{=]{0,200}(?:\{|=>)\s*(?:\{\s*)?(?:return\s+)?(['"])([^'"]+)\1"""
)
_ADAPTER_KIND_RE = re.compile(r"\b(RESTAdapter|JSONAPIAdapter)\b")
_HOST_RE = re.compile(r"""\bhost\s*[:=]\s*(['"])([^'"]*)\1""")


@dataclass
class AdapterInfo:
    """What a model's adapter chain says about its URLs."""

    kind: str = "rest"
    namespace: Optional[str] = None
    namespace_dynamic: bool = False
    path_for_type: Optional[str] = None
    path_for_type_dynamic: bool = False
    host: Optional[str] = None
    found: bool = False
    explicit_base: bool = False


_MAX_ADAPTER_BYTES = 200_000


def _strip_comments(text: str) -> str:
    """Drop ``/* */`` blocks and whole-line ``//`` comments in one linear pass.

    An unterminated ``/*`` removes the rest of the text.  Quotes are not
    tracked, which is the same approximation the earlier regexes made.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    line_start = True
    while i < n:
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        if line_start and text.startswith("//", _skip_blanks(text, i)):
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        ch = text[i]
        out.append(ch)
        line_start = ch == "\n" or (line_start and ch in " \t\r")
        i += 1
    return "".join(out)


def _skip_blanks(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r":
        i += 1
    return i


def _read_adapter(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > _MAX_ADAPTER_BYTES:
        return None
    return _strip_comments(text)


def _apply_adapter(text: str, info: AdapterInfo) -> None:
    info.found = True
    kind = _ADAPTER_KIND_RE.search(text)
    if kind:
        info.kind = "jsonapi" if kind.group(1) == "JSONAPIAdapter" else "rest"
        info.explicit_base = True
    ns = _NAMESPACE_RE.search(text)
    if ns:
        if ns.group(1) is not None:
            info.namespace = ns.group(2)
            info.namespace_dynamic = False
        else:
            info.namespace = None
            info.namespace_dynamic = True
    if _PATH_FOR_TYPE_RE.search(text):
        literal = _PATH_FOR_TYPE_LITERAL_RE.search(text)
        if literal:
            info.path_for_type = literal.group(2)
            info.path_for_type_dynamic = False
        else:
            info.path_for_type = None
            info.path_for_type_dynamic = True
    host = _HOST_RE.search(text)
    if host:
        info.host = host.group(2)


def _model_name(path: Path) -> Optional[tuple[str, Path, bool]]:
    """``(model name, app directory, pods layout)`` of an Ember model file."""
    parts = list(path.parts)
    for index, part in enumerate(parts[:-1]):
        if part not in ("app", "addon"):
            continue
        rest = parts[index + 1:]
        app_dir = Path(*parts[: index + 1])
        if rest[0] == "models" and len(rest) >= 2:
            name = "/".join(rest[1:])
            return name.rsplit(".", 1)[0], app_dir, False
        stem = rest[-1].rsplit(".", 1)[0]
        if len(rest) >= 2 and stem == "model":
            return "/".join(rest[:-1]), app_dir, True
    return None


def ember_model_info(path: Path) -> Optional[tuple[str, AdapterInfo]]:
    """Model name and adapter facts of an Ember model file, else ``None``.

    The adapter chain is read from disk: ``adapters/application`` first, then
    the model's own adapter, which overrides what it sets.  It lives in other
    files, so an adapter-only edit shows on the next parse of the model.
    """
    if ember_extract.role_from_path(path.as_posix()) != "model":
        return None
    found = _model_name(path)
    if found is None:
        return None
    name, app_dir, pods = found
    if name.endswith(".d"):
        return None
    candidates: list[Path] = []
    for ext in ("js", "ts"):
        candidates.append(app_dir / "adapters" / f"application.{ext}")
    own: list[Path] = []
    for ext in ("js", "ts"):
        if pods:
            own.append(app_dir / name / f"adapter.{ext}")
        else:
            own.append(app_dir / "adapters" / f"{name}.{ext}")
    info = AdapterInfo()
    for candidate in candidates + own:
        text = _read_adapter(candidate) if candidate.is_file() else None
        if text is not None:
            _apply_adapter(text, info)
    if not info.found:
        return None
    return name, info


def ember_model_routes(name: str, info: AdapterInfo) -> list[tuple[str, str, bool]]:
    """``(method, route, dynamic)`` the store would use for a model.

    ``findAll`` is ``GET /ns/types``; ``findRecord`` is ``GET /ns/types/{id}``;
    ``createRecord`` is ``POST /ns/types``; ``updateRecord`` is ``PUT`` for
    ``RESTAdapter`` and ``PATCH`` for ``JSONAPIAdapter``; ``deleteRecord`` is
    ``DELETE /ns/types/{id}``.  ``host``, ``urlForFindRecord`` and the other
    ``urlFor*`` hooks are ignored.
    """
    if info.namespace_dynamic or info.path_for_type_dynamic:
        return [("GET", UNRESOLVED, True)]
    if info.namespace is None and not info.explicit_base:
        return []
    jsonapi = info.kind == "jsonapi"
    segment = info.path_for_type or pluralize_model(name, dasherized=jsonapi)
    prefix = "/" + (info.namespace or "").strip("/")
    prefix = prefix.rstrip("/")
    collection = f"{prefix}/{segment}".replace("//", "/")
    update = "PATCH" if jsonapi else "PUT"
    return [
        ("GET", collection, False),
        ("GET", f"{collection}/{{id}}", False),
        ("POST", collection, False),
        (update, f"{collection}/{{id}}", False),
        ("DELETE", f"{collection}/{{id}}", False),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _innermost(functions: list[Any], line: int) -> Optional[Any]:
    best = None
    best_span = 0
    for node in functions:
        if node.line_start <= line <= node.line_end:
            span = node.line_end - node.line_start
            if best is None or span < best_span:
                best, best_span = node, span
    return best


def _qualified(name: str, file_path: str, parent: Optional[str]) -> str:
    return f"{file_path}::{parent}.{name}" if parent else f"{file_path}::{name}"


def extract(
    root: Any,
    source: bytes,
    file_path: str,
    rel_path: str,
    path: Path,
    nodes: list["NodeInfo"],
    edges: list["EdgeInfo"],
    node_cls: Any,
    edge_cls: Any,
    language: str,
    is_test: bool,
) -> int:
    """Append HTTP endpoint nodes and edges for one JS/TS file; return how many.

    *file_path* is the identity path used for nodes; *rel_path* is the
    repo-relative path that decides Next.js and Ember layouts; *path* is the
    on-disk path (Ember adapters are read beside it).
    """
    if is_test or any(part in _SKIP_DIRS for part in rel_path.split("/")[:-1]):
        return 0
    emitted = 0
    functions = [n for n in nodes if n.kind in _FUNCTION_KINDS]

    def caller_of(line: int) -> tuple[str, Optional[str]]:
        node = _innermost(functions, line)
        if node is None:
            return file_path, None
        return _qualified(node.name, file_path, node.parent_name), node.name

    # -- Ember Data ---------------------------------------------------
    model = ember_model_info(path)
    if model is not None:
        model_name, adapter = model
        for index, (method, route, dynamic) in enumerate(ember_model_routes(model_name, adapter)):
            name = f"{model_name}@ember-data[{index}] {method} {route}"
            meta: dict[str, Any] = {
                "annotation": "ember-data",
                "client_method": model_name,
                "http_method": method,
                "route": route,
                "role": "client",
                "client": CLIENT_TAG,
                "client_kind": "ember-data",
                "origin": "js",
                "route_dynamic": dynamic,
                "inferred": True,
                "adapter_kind": adapter.kind,
            }
            if adapter.host:
                meta["host"] = adapter.host
            nodes.append(node_cls(
                kind="Endpoint", name=name, file_path=file_path, line_start=1,
                line_end=max(1, source.count(b"\n") + 1), language=language, extra=meta,
            ))
            edges.append(edge_cls(
                kind=REQUESTS_EDGE, source=file_path,
                target=_qualified(name, file_path, None), file_path=file_path, line=1,
                extra={
                    **meta, "resolution": "ember_data_adapter",
                    "confidence": EMBER_DATA_CONFIDENCE, "confidence_tier": "INFERRED",
                },
            ))
            emitted += 1

    # -- Next.js route files (no call needed) ---------------------------
    server_hits: list[ServerHit] = []
    next_candidate = rel_path.rsplit("/", 1)[-1].startswith("route.") or "/pages/api/" in (
        "/" + rel_path
    ) or rel_path.startswith("pages/api/")
    if next_candidate:
        server_hits.extend(_next_hits(rel_path, root, source))

    wants_calls = bool(_QUICK_RE.search(source)) or (
        "axios" in source.decode("utf-8", errors="ignore")
    )
    client_hits: list[ClientHit] = []
    if wants_calls:
        facts = _FileFacts()
        _collect(root, facts, source)
        server_ok = bool(_SERVER_EVIDENCE_RE.search(source))
        for call in facts.calls:
            hit = _client_hit(call, facts)
            if hit is not None:
                client_hits.append(hit)
                continue
            if server_ok:
                server_hits.extend(_server_call_hits(call, facts))

    counter = 0
    for hit in client_hits[:MAX_ENDPOINTS]:
        source_qn, caller = caller_of(hit.line)
        label = caller or "<module>"
        name = f"{label}@{hit.kind}[{counter}] {hit.method} {hit.route}"
        counter += 1
        meta = {
            "annotation": hit.kind,
            "client_method": label,
            "http_method": hit.method,
            "route": hit.route,
            "role": "client",
            "client": CLIENT_TAG,
            "client_kind": hit.kind,
            "origin": "js",
            "route_dynamic": hit.route_dynamic,
        }
        if hit.method_dynamic:
            meta["method_dynamic"] = True
        if hit.absolute_url:
            meta["absolute_url"] = True
        if hit.base_url:
            meta["base_url"] = hit.base_url
        if hit.base_url_dynamic:
            meta["base_url_dynamic"] = True
        if hit.instance:
            meta["instance"] = hit.instance
        if hit.receiver_inferred:
            meta["receiver_inferred"] = True
        nodes.append(node_cls(
            kind="Endpoint", name=name, file_path=file_path, line_start=hit.line,
            line_end=hit.end_line, language=language, extra=meta,
        ))
        edge_extra = {**meta, "resolution": "js_http_client"}
        if hit.receiver_inferred:
            edge_extra.update({"confidence": 0.8, "confidence_tier": "INFERRED"})
        edges.append(edge_cls(
            kind=REQUESTS_EDGE, source=source_qn, target=_qualified(name, file_path, None),
            file_path=file_path, line=hit.line, extra=edge_extra,
        ))
        emitted += 1

    function_names = {n.name for n in functions if n.parent_name is None}
    for hit_s in server_hits[:MAX_ENDPOINTS]:
        handler = hit_s.handler if hit_s.handler in function_names else None
        if handler:
            source_qn = _qualified(handler, file_path, None)
            label = handler
        else:
            source_qn, caller = caller_of(hit_s.line)
            label = caller or hit_s.handler or "<anonymous>"
        name = f"{label}@{hit_s.framework}[{counter}] {hit_s.method} {hit_s.route}"
        counter += 1
        meta = {
            "annotation": hit_s.framework,
            "handler": label,
            "http_method": hit_s.method,
            "route": hit_s.route,
            "framework": hit_s.framework,
            "origin": "js",
        }
        if hit_s.catch_all:
            meta["catch_all"] = True
        if hit_s.optional_catch_all:
            meta["optional_catch_all"] = True
        nodes.append(node_cls(
            kind="Endpoint", name=name, file_path=file_path, line_start=hit_s.line,
            line_end=hit_s.end_line, language=language, extra=meta,
        ))
        edges.append(edge_cls(
            kind="HANDLES", source=source_qn, target=_qualified(name, file_path, None),
            file_path=file_path, line=hit_s.line, extra=meta,
        ))
        emitted += 1
    return emitted
