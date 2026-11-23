"""Parse-time HTTP routes and API calls in JavaScript and TypeScript modules.

Two things are read out of one parsed file and recorded as ``Endpoint``
nodes, in the same shape the Spring Feign and controller code uses:

* **Client calls** with a statically known route: ``axios`` (the default
  export and instances made with ``axios.create``), ``fetch``, ``$.ajax`` and
  friends, and Ember ``ajax``.  Each is a client ``Endpoint`` (``role`` is
  ``"client"``) with a ``REQUESTS`` edge from the enclosing function.
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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

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


def is_query_suffix(node: Any) -> bool:
    """Whether an expression is a query string or nothing.

    That is ``''``, ``'?a=1'`` or ``cond ? `?${q}` : ''``.

    Such a piece glued after a path (``/feeds${query}``) is the query part, not
    a path parameter, so :func:`split_route` drops it with the rest.
    """
    node = _unwrap(node)
    if node is None:
        return False
    if node.type == "string":
        text = _string_literal(node)
        return text is not None and (text == "" or text[0] in "?#")
    if node.type == "template_string":
        parts = [c for c in node.children if c.type != "`"]
        if not parts:
            return True
        return parts[0].type != "template_substitution" and _text(parts[0])[:1] in ("?", "#")
    if node.type in ("ternary_expression", "conditional_expression"):
        return all(
            is_query_suffix(node.child_by_field_name(field))
            for field in ("consequence", "alternative")
        )
    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        return op is not None and _text(op) == "+" and is_query_suffix(
            node.child_by_field_name("left"),
        )
    return False


def static_text(
    node: Any,
    consts: dict[str, str],
    head: bool = True,
    suffixes: frozenset[str] = frozenset(),
) -> Optional[str]:
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
                        if known is None and _text(expr) in suffixes:
                            known = "?"
                    elif expr is not None and is_query_suffix(expr):
                        known = "?"
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
        left = static_text(node.child_by_field_name("left"), consts, head, suffixes)
        if left is None:
            return None
        right_node = node.child_by_field_name("right")
        right = "?" if is_query_suffix(right_node) else static_text(
            right_node, consts, head=False, suffixes=suffixes,
        )
        if right is None:
            # An unknown trailing piece is one path parameter, like ``${x}``.
            right = "{param}"
        return left + right
    if kind == "identifier":
        name = _text(node)
        if name in consts:
            return consts[name]
        if not head and name in suffixes:
            return "?"
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
        #: names that only ever hold a query string (see ``is_query_suffix``)
        self.suffixes: frozenset[str] = frozenset()
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


def _bound_names(node: Any) -> list[str]:
    """Names a parameter list or arrow function binds, for the query-suffix check."""
    names: list[str] = []
    if node.type == "formal_parameters":
        for child in node.children:
            if child.type == "identifier":
                names.append(_text(child))
            elif child.type in ("required_parameter", "optional_parameter", "assignment_pattern"):
                target = child.child_by_field_name("pattern") or child.child_by_field_name("left")
                if target is not None and target.type == "identifier":
                    names.append(_text(target))
    elif node.type == "arrow_function":
        single = node.child_by_field_name("parameter")
        if single is not None and single.type == "identifier":
            names.append(_text(single))
    return names


def _collect(root: Any, facts: _FileFacts, source: bytes) -> None:
    _import_bindings(root, facts)
    # name -> one flag per binding: is it a ``const`` holding only a query string?
    bindings: dict[str, list[bool]] = {}
    for node in _walk(root):
        kind = node.type
        if kind == "call_expression":
            facts.calls.append(node)
        elif kind in ("formal_parameters", "arrow_function"):
            for bound in _bound_names(node):
                bindings.setdefault(bound, []).append(False)
        elif kind == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = _unwrap(node.child_by_field_name("value"))
            if name_node is not None and name_node.type == "identifier":
                parent = node.parent
                is_const = (
                    parent is not None
                    and parent.type == "lexical_declaration"
                    and _text(parent.children[0]) == "const"
                )
                bindings.setdefault(_text(name_node), []).append(
                    bool(is_const and value is not None and is_query_suffix(value)),
                )
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
        if kind == "assignment_expression":
            target = node.child_by_field_name("left")
            if target is not None and target.type == "identifier":
                bindings.setdefault(_text(target), []).append(False)
    facts.suffixes = frozenset(name for name, flags in bindings.items() if all(flags))


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
    folded = (
        static_text(url_node, facts.consts, suffixes=facts.suffixes)
        if url_node is not None else None
    )
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
    # (line, column, end line) of an inline handler function, else None
    inline: Optional[tuple[int, int, int]] = None


_INLINE_HANDLER_TYPES = frozenset({"arrow_function", "function_expression", "function"})


def _inline_span(node: Any) -> Optional[tuple[int, int, int]]:
    """Where an inline handler function sits, so each one gets its own identity."""
    node = _unwrap(node)
    if node is None or node.type not in _INLINE_HANDLER_TYPES:
        return None
    return node.start_point[0] + 1, node.start_point[1] + 1, node.end_point[0] + 1


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
            _handler_name(args[-1]), catch_all, inline=_inline_span(args[-1]),
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
        _handler_name(args[-1]), catch_all, inline=_inline_span(args[-1]),
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
    inline = _inline_span(handler_node) if handler_node is not None else None
    return [
        ServerHit("express", m, route, line, end_line, handler, catch_all, inline=inline)
        for m in methods
    ]


def _handler_name(node: Any) -> Optional[str]:
    node = _unwrap(node)
    if node is not None and node.type == "identifier":
        return _text(node)
    return None


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
    repo-relative path that decides the Next.js layout; *path* is the
    on-disk path of the file.
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
    inline_handlers: dict[tuple[int, int, int], tuple[str, str]] = {}
    for hit_s in server_hits[:MAX_ENDPOINTS]:
        handler = hit_s.handler if hit_s.handler in function_names else None
        if handler:
            source_qn = _qualified(handler, file_path, None)
            label = handler
        elif hit_s.inline is not None:
            # An inline handler is its own function: one node per function, so two routes
            # never share a handler, and the routes of one method list (fastify) share one.
            if hit_s.inline not in inline_handlers:
                line_s, col_s, end_s = hit_s.inline
                inline_name = f"handler@L{line_s}C{col_s}"
                registrar, _ = caller_of(line_s)
                inline_handlers[hit_s.inline] = (inline_name, registrar)
                inline_qn = _qualified(inline_name, file_path, None)
                nodes.append(node_cls(
                    kind="Function", name=inline_name, file_path=file_path, line_start=line_s,
                    line_end=end_s, language=language,
                    extra={"inline_handler": True, "framework": hit_s.framework},
                ))
                edges.append(edge_cls(
                    kind="CONTAINS", source=file_path, target=inline_qn,
                    file_path=file_path, line=line_s,
                ))
                # The registering code uses the handler, as it uses a named one.
                edges.append(edge_cls(
                    kind="REFERENCES", source=registrar, target=inline_qn,
                    file_path=file_path, line=line_s,
                ))
            label = inline_handlers[hit_s.inline][0]
            source_qn = _qualified(label, file_path, None)
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

    # What an inline handler's body calls belongs to the handler, not to the code that
    # registered it. Edges carry only a line, so a call on the handler's first line stays with
    # the registrar: that line also holds the registering call itself.
    for key, (inline_name, registrar) in inline_handlers.items():
        line_s, _col, end_s = key
        inline_qn = _qualified(inline_name, file_path, None)
        for edge in edges:
            if (
                edge.kind in ("CALLS", "REFERENCES", REQUESTS_EDGE)
                and edge.source == registrar
                and edge.target != inline_qn
                and line_s < edge.line <= end_s
            ):
                edge.source = inline_qn
    return emitted
