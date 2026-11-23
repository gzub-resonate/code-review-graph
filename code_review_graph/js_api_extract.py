"""Parse-time HTTP API calls in JavaScript and TypeScript modules.

Client calls with a statically known route are read out of one parsed file
and recorded as ``Endpoint`` nodes, in the same shape the Spring Feign code
uses: ``axios`` (the default export and instances made with ``axios.create``),
``fetch``, ``$.ajax`` and friends, and Ember ``ajax``.  Each is a client
``Endpoint`` (``role`` is ``"client"``) with a ``REQUESTS`` edge from the
enclosing function.

Nothing is guessed.  A URL that is not a string literal, a template literal, a
concatenation of literals or a same-file ``const`` string is recorded as
*unmatchable* with a reason (when the call is certainly an HTTP call) or left
out.

Known limits, also in ``docs/COMMANDS.md``:

* An instance imported from another file has no known ``baseURL``; its
  routes are recorded as written.  Only a literal ``baseURL`` given to
  ``axios.create`` in the same file (or ``instance.defaults.baseURL = '...'``)
  is applied.
* Test files are skipped.
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
#: Verbs whose axios call takes the config as the third argument.
_BODY_VERBS = frozenset({"post", "put", "patch"})

#: Cheap pre-filter: a file with none of these cannot hold a call.
_QUICK_RE = re.compile(
    rb"axios|fetch\s*\(|\bajax\b|\$\.|jQuery|getJSON"
    rb"|\.(?:get|post|put|patch|delete)\s*\(|\bapi\b|[Cc]lient|[Hh]ttp"
)
_INSTANCE_NAME_RE = re.compile(
    r"^[$_]*(?:api|http|axios|request|fetcher|client|ajax)|(?:Api|Http|Axios|Client|Request|Fetcher|Ajax)$"
)
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//")
_INTERNAL_IMPORT_PREFIXES = (".", "/", "@/", "~/", "#", "$", "src/", "app/", "lib/")

#: Directories whose files are test doubles, stubs or demos, never real calls.
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
    """Append HTTP client endpoint nodes and edges for one JS/TS file; return how many.

    *file_path* is the identity path used for nodes; *rel_path* is the
    repo-relative path that excludes test-double directories; *path* is the
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

    wants_calls = bool(_QUICK_RE.search(source)) or (
        "axios" in source.decode("utf-8", errors="ignore")
    )
    client_hits: list[ClientHit] = []
    if wants_calls:
        facts = _FileFacts()
        _collect(root, facts, source)
        for call in facts.calls:
            hit = _client_hit(call, facts)
            if hit is not None:
                client_hits.append(hit)

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
    return emitted
