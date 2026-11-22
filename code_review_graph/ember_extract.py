"""Parse-time Ember.js evidence for JavaScript and TypeScript modules.

Ember wires an application by naming convention, not by imports: ``@service
store`` finds ``app/services/store.js`` and ``this.store.findRecord('post')``
finds ``app/models/post.js``. This module reads that wiring out of one parsed
file and records it as edges with *placeholder* targets of the form
``ember:<kind>:<name>``. :mod:`code_review_graph.ember_resolver` binds the
placeholders to files after the whole build, because only then are all the
modules known.

Everything here is value-free: only identifiers (property names, service and
model names, route names) are read. String literals in the source are never
stored, except the literal that *is* a name (``service('retry')``,
``this.route('posts', { path: '/p' })``).

A file takes part only when it shows Ember evidence: it imports from
``@ember/*``, ``ember``, ``ember-data``, ``@ember-data/*`` or
``@glimmer/component``. A Redux ``store.query('x')`` in a file with none of
those is left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .parser import EdgeInfo, NodeInfo

#: Marker key stored in the parser's ``import_map`` while a file is parsed, so
#: the generic call extraction knows the file shows Ember evidence.
EMBER_EVIDENCE_KEY = "__crg_ember__"

PLACEHOLDER_PREFIX = "ember:"

#: Most nodes one file may contribute to a single Ember walk. A generated
#: bundle that is far bigger than any hand-written module is not walked.
MAX_WALK_NODES = 400_000
#: Most routes read from one router file, and the deepest nesting followed.
MAX_ROUTES = 2_000
MAX_ROUTE_DEPTH = 24

_EMBER_MODULES = re.compile(
    r"^(?:@ember/|@ember-data/|@glimmer/component$|ember$|ember-data(?:/|$)|ember-modifier$)"
)

#: Ember module -> role of the class that module's default export names.
_BASE_BY_MODULE = {
    "@ember/service": "service",
    "@ember/component": "component",
    "@glimmer/component": "component",
    "@ember/component/helper": "helper",
    "@ember/routing/route": "route",
    "@ember/controller": "controller",
    "@ember/object/mixin": "mixin",
    "ember-modifier": "modifier",
    "@ember/modifier": "modifier",
    "@ember-data/model": "model",
    "ember-data/model": "model",
    "@ember-data/adapter": "adapter",
    "ember-data/adapter": "adapter",
    "@ember-data/adapter/rest": "adapter",
    "ember-data/adapters/rest": "adapter",
    "@ember-data/adapter/json-api": "adapter",
    "ember-data/adapters/json-api": "adapter",
    "@ember-data/serializer": "serializer",
    "ember-data/serializer": "serializer",
    "@ember-data/serializer/rest": "serializer",
    "ember-data/serializers/rest": "serializer",
    "@ember-data/serializer/json": "serializer",
    "ember-data/serializers/json": "serializer",
    "@ember-data/serializer/json-api": "serializer",
    "ember-data/serializers/json-api": "serializer",
    "@ember-data/serializer/transform": "transform",
    "ember-data/transform": "transform",
}

#: ``Ember.<Member>`` / ``DS.<Member>`` -> role.
_BASE_BY_MEMBER = {
    "Service": "service",
    "Component": "component",
    "Helper": "helper",
    "Route": "route",
    "Controller": "controller",
    "Mixin": "mixin",
    "Model": "model",
    "Adapter": "adapter",
    "RESTAdapter": "adapter",
    "JSONAPIAdapter": "adapter",
    "Serializer": "serializer",
    "RESTSerializer": "serializer",
    "JSONSerializer": "serializer",
    "JSONAPISerializer": "serializer",
    "Transform": "transform",
}

#: Directory under ``app/`` or ``addon/`` -> role.
_ROLE_BY_DIR = {
    "routes": "route",
    "controllers": "controller",
    "components": "component",
    "services": "service",
    "models": "model",
    "adapters": "adapter",
    "serializers": "serializer",
    "helpers": "helper",
    "modifiers": "modifier",
    "mixins": "mixin",
    "transforms": "transform",
    "templates": "template",
}

#: File stem of a pods module -> role (``app/posts/route.js``).
_ROLE_BY_POD_STEM = {
    "route": "route",
    "controller": "controller",
    "component": "component",
    "service": "service",
    "model": "model",
    "adapter": "adapter",
    "serializer": "serializer",
    "helper": "helper",
    "modifier": "modifier",
    "template": "template",
}

_STORE_METHODS = frozenset({
    "findRecord", "findAll", "query", "queryRecord", "peekAll", "peekRecord",
    "createRecord", "unloadAll", "unloadRecord", "push", "pushPayload",
})
#: ``push`` and ``unloadRecord`` take a record or a payload, not a model name.
_STORE_NAMED_METHODS = _STORE_METHODS - {"push", "unloadRecord"}

_RELATIONSHIPS = frozenset({"belongsTo", "hasMany"})

def dasherize(name: str) -> str:
    """Ember's module spelling of a name: ``retryService`` is ``retry-service``."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", text)
    return text.replace("_", "-").replace(" ", "-").lower()


def role_from_path(file_path: str) -> Optional[str]:
    """Role a module path implies under the Ember layout, or None.

    Test directories are never roles: ``tests/unit/services/x-test.js`` tests a
    service, it is not one.
    """
    parts = file_path.replace("\\", "/").split("/")
    if "tests" in parts[:-1] and "dummy" not in parts:
        return None
    if "node_modules" in parts or "bower_components" in parts:
        return None
    # An ``app`` or ``addon`` directory may itself sit under another one
    # (``addon/app/services/x.js``), so try each until one gives a role.
    for index, part in enumerate(parts[:-1]):
        if part not in ("app", "addon"):
            continue
        rest = parts[index + 1:]
        if rest[0] in _ROLE_BY_DIR:
            return _ROLE_BY_DIR[rest[0]]
        stem = rest[-1].rsplit(".", 1)[0]
        if len(rest) >= 2 and stem in _ROLE_BY_POD_STEM:
            return _ROLE_BY_POD_STEM[stem]
    return None


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def string_value(node) -> Optional[str]:
    """The text of a plain string literal node, else None (templates, calls)."""
    if node is None or node.type != "string":
        return None
    for child in node.children:
        if child.type == "string_fragment":
            return _text(child)
    inner = _text(node)
    return inner[1:-1] if len(inner) >= 2 and inner[0] in "'\"" else None


@dataclass
class EmberImports:
    """What one module imports from Ember, by local name."""

    #: local name -> role, for ``import Service from '@ember/service'``.
    base_locals: dict[str, str] = field(default_factory=dict)
    #: local names that inject a service: ``service`` or ``inject as service``.
    service_injectors: set[str] = field(default_factory=set)
    #: local names that inject a controller.
    controller_injectors: set[str] = field(default_factory=set)
    #: local names bound to the ``ember`` or ``ember-data`` default export.
    namespaces: set[str] = field(default_factory=set)
    #: local names of ``belongsTo`` / ``hasMany`` (value: the relationship kind).
    relationships: dict[str, str] = field(default_factory=dict)
    evidence: bool = False


def collect_imports(root, source: bytes) -> EmberImports:
    """Read the top-level imports of a module for Ember bindings."""
    imports = EmberImports()
    for node in root.children:
        if node.type != "import_statement":
            continue
        module = string_value(node.child_by_field_name("source"))
        if module is None:
            for child in node.children:
                module = string_value(child) or module
        if module is None or not _EMBER_MODULES.match(module):
            continue
        imports.evidence = True
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for part in clause.children:
            if part.type == "identifier":
                local = _text(part)
                if module in ("ember", "ember-data"):
                    imports.namespaces.add(local)
                elif module in _BASE_BY_MODULE:
                    imports.base_locals[local] = _BASE_BY_MODULE[module]
                if module == "@ember/service":
                    imports.base_locals[local] = "service"
            elif part.type == "named_imports":
                for spec in part.children:
                    if spec.type != "import_specifier":
                        continue
                    names = [c for c in spec.children if c.type == "identifier"]
                    if not names:
                        continue
                    imported = _text(names[0])
                    local = _text(names[-1])
                    if module == "@ember/service" and imported in ("service", "inject"):
                        imports.service_injectors.add(local)
                    elif module == "@ember/controller" and imported in ("inject", "controller"):
                        imports.controller_injectors.add(local)
                    elif imported in _RELATIONSHIPS and "data" in module:
                        imports.relationships[local] = imported
                    elif module in _BASE_BY_MODULE and imported == "default":
                        imports.base_locals[local] = _BASE_BY_MODULE[module]
            elif part.type == "namespace_import":
                ident = next((c for c in part.children if c.type == "identifier"), None)
                if ident is not None and module in ("ember", "ember-data"):
                    imports.namespaces.add(_text(ident))
    return imports


def has_evidence(root, source: bytes) -> bool:
    """Does the module import anything from Ember?"""
    return collect_imports(root, source).evidence


def _walk(root) -> Iterator:
    """Pre-order walk, bounded by :data:`MAX_WALK_NODES`."""
    stack = [root]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > MAX_WALK_NODES:
            return
        yield node
        stack.extend(reversed(node.children))


def _callee_chain(node) -> list[str]:
    """``a.b.c`` -> ``["a", "b", "c"]``; anything else -> ``[]``."""
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


def _first_string_arg(call) -> tuple[Optional[str], bool]:
    """(literal first argument or None, whether there was a first argument)."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None, False
    named = [c for c in args.children if c.is_named]
    if not named:
        return None, False
    return string_value(named[0]), True


def _injection_kind(callee, imports: EmberImports) -> Optional[str]:
    """``service`` / ``controller`` when *callee* is an Ember injection helper."""
    if callee.type == "identifier":
        name = _text(callee)
        if name in imports.service_injectors:
            return "service"
        if name in imports.controller_injectors:
            return "controller"
        return None
    chain = _callee_chain(callee)
    # ``Ember.inject.service(...)`` and ``inject.service(...)``
    if len(chain) >= 2 and chain[-2] == "inject" and chain[-1] in ("service", "controller"):
        if len(chain) == 3 and chain[0] not in imports.namespaces:
            return None
        if len(chain) == 2 and not imports.evidence:
            return None
        return chain[-1]
    return None


def _decorator_call(decorator):
    """(callee node, call node or None) for ``@name`` / ``@name(...)``."""
    for child in decorator.children:
        if child.type == "call_expression":
            return child.child_by_field_name("function"), child
        if child.type in ("identifier", "member_expression"):
            return child, None
    return None, None


def _field_name(node) -> Optional[str]:
    name = node.child_by_field_name("name") or node.child_by_field_name("property")
    if name is None:
        name = next(
            (c for c in node.children
             if c.type in ("property_identifier", "private_property_identifier")),
            None,
        )
    return _text(name) if name is not None else None


@dataclass
class ModuleFacts:
    """What one module declares, handed back to the parser."""

    base_role: Optional[str] = None
    class_roles: dict[str, str] = field(default_factory=dict)


def _base_role(expr, imports: EmberImports) -> Optional[str]:
    """Role of an ``extends`` / ``.extend`` base expression."""
    if expr is None:
        return None
    if expr.type == "identifier":
        return imports.base_locals.get(_text(expr))
    chain = _callee_chain(expr)
    if len(chain) == 2 and chain[0] in imports.namespaces:
        return _BASE_BY_MEMBER.get(chain[1])
    return None


def _class_name(node) -> Optional[str]:
    name = node.child_by_field_name("name")
    return _text(name) if name is not None else None


def extract(
    root,
    source: bytes,
    file_path: str,
    nodes: list["NodeInfo"],
    edges: list["EdgeInfo"],
    node_info_cls,
    edge_info_cls,
    language: str,
) -> ModuleFacts:
    """Add Ember injections, relationships and routes of one module.

    Also stamps ``extra["ember_kind"]`` on the module's File node and on its
    classes, from the module path first and the base class second.
    """
    facts = ModuleFacts()
    imports = collect_imports(root, source)
    path_role = role_from_path(file_path)
    is_router = bool(re.search(r"(?:^|/)router\.(?:js|ts)$", file_path))
    if not imports.evidence and not (path_role and is_router):
        return facts

    # (class name or None for file level) pushed while descending.
    class_stack: list[Optional[str]] = []
    class_ends: list[int] = []
    router_done = False

    def owner_qualified() -> str:
        names = [n for n in class_stack if n]
        if not class_stack or not names or class_stack[-1] is None:
            return file_path
        return f"{file_path}::{'.'.join(names)}"

    def add_edge(kind: str, target: str, node, extra: dict) -> None:
        edges.append(edge_info_cls(
            kind=kind,
            source=owner_qualified(),
            target=target,
            file_path=file_path,
            line=node.start_point[0] + 1,
            extra=extra,
        ))

    def inject_edge(kind: str, prop: str, explicit: Optional[str],
                    node, spelling: str) -> None:
        raw = explicit if explicit else prop
        add_edge("INJECTS", f"{PLACEHOLDER_PREFIX}{kind}:{raw}", node, {
            "framework": "ember",
            "ember_inject": kind,
            "ember_name": raw,
            "ember_explicit": bool(explicit),
            "field_name": prop,
            "ember_spelling": spelling,
        })

    def relationship_edge(kind: str, prop: str, target: Optional[str], node) -> None:
        if not target:
            return
        add_edge("REFERENCES", f"{PLACEHOLDER_PREFIX}model:{target}", node, {
            "framework": "ember",
            "ember_ref": "model",
            "ember_name": target,
            "ember_relationship": kind,
            "field_name": prop,
        })

    def handle_value_call(prop: str, call, node, spelling: str) -> None:
        callee = call.child_by_field_name("function")
        if callee is None:
            return
        kind = _injection_kind(callee, imports)
        explicit, _ = _first_string_arg(call)
        if kind:
            inject_edge(kind, prop, explicit, node, spelling)
            return
        rel = None
        if callee.type == "identifier":
            rel = imports.relationships.get(_text(callee))
        else:
            chain = _callee_chain(callee)
            if len(chain) == 2 and chain[0] in imports.namespaces and chain[1] in _RELATIONSHIPS:
                rel = chain[1]
        if rel:
            relationship_edge(rel, prop, explicit, node)

    for node in _walk(root):
        # Leave class scopes whose text range has ended.
        while class_ends and node.start_byte >= class_ends[-1]:
            class_ends.pop()
            class_stack.pop()
        ntype = node.type
        if ntype in ("class_declaration", "class", "abstract_class_declaration"):
            class_stack.append(_class_name(node))
            class_ends.append(node.end_byte)
            name = _class_name(node)
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            base_expr = None
            if heritage is not None:
                base_expr = next((c for c in heritage.children if c.is_named), None)
                if base_expr is not None and base_expr.type == "extends_clause":
                    base_expr = next((c for c in base_expr.children if c.is_named), None)
            role = _base_role(base_expr, imports)
            if role and name:
                facts.class_roles[name] = role
            if role and facts.base_role is None:
                facts.base_role = role
        elif ntype in ("field_definition", "public_field_definition"):
            prop = _field_name(node)
            if not prop:
                continue
            for dec in (c for c in node.children if c.type == "decorator"):
                callee, call = _decorator_call(dec)
                if callee is None:
                    continue
                kind = _injection_kind(callee, imports)
                explicit = _first_string_arg(call)[0] if call is not None else None
                if kind:
                    inject_edge(kind, prop, explicit, node, "decorator")
                    continue
                name = _text(callee) if callee.type == "identifier" else ""
                rel = imports.relationships.get(name)
                if rel is None:
                    chain = _callee_chain(callee)
                    if len(chain) == 2 and chain[0] in imports.namespaces \
                            and chain[1] in _RELATIONSHIPS:
                        rel = chain[1]
                if rel and call is not None:
                    relationship_edge(rel, prop, explicit, node)
        elif ntype == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if key is None or value is None or value.type != "call_expression":
                continue
            prop = string_value(key) if key.type == "string" else _text(key)
            if prop and key.type in ("property_identifier", "identifier", "string"):
                handle_value_call(prop, value, node, "property")
        elif ntype == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            chain = _callee_chain(callee)
            # ``.extend(Base, ...)``: a classic module's base class.
            is_extend = len(chain) >= 2 and chain[-1] in ("extend", "reopenClass")
            if is_extend and facts.base_role is None:
                head = callee.child_by_field_name("object")
                role = _base_role(head, imports)
                if role:
                    facts.base_role = role
            elif len(chain) == 2 and chain[1] == "create" and chain[0] in imports.base_locals \
                    and imports.base_locals[chain[0]] == "mixin" and facts.base_role is None:
                facts.base_role = "mixin"
            method_node = (
                callee.child_by_field_name("property")
                if callee.type == "member_expression" else None
            )
            method = _text(method_node) if method_node is not None else ""
            if method in _STORE_NAMED_METHODS and _is_store_receiver(callee):
                name, _had_arg = _first_string_arg(node)
                if name:
                    add_edge("REFERENCES", f"{PLACEHOLDER_PREFIX}model:{name}", node, {
                        "framework": "ember",
                        "ember_ref": "model",
                        "ember_name": name,
                        "ember_store_method": method,
                    })
            if is_router and not router_done and chain and chain[-1] == "map":
                router_done = True
                _extract_routes(node, file_path, nodes, node_info_cls, language)

    return facts


def _is_store_receiver(callee) -> bool:
    """``this.store.m`` / ``store.m`` / ``this.get('store').m``."""
    obj = callee.child_by_field_name("object") if callee.type == "member_expression" else None
    if obj is None:
        return False
    if obj.type == "identifier":
        return _text(obj) == "store"
    if obj.type == "member_expression":
        chain = _callee_chain(obj)
        return chain == ["this", "store"]
    if obj.type == "call_expression":
        fn = obj.child_by_field_name("function")
        chain = _callee_chain(fn) if fn is not None else []
        if chain == ["this", "get"]:
            return _first_string_arg(obj)[0] == "store"
    return False


def chained_receiver(call_node) -> Optional[tuple[str, str]]:
    """``(property, method)`` for ``this.get('prop').method(...)``, else None.

    The generic parser keeps no receiver for this shape, and it is the main
    way classic Ember code reaches an injected service.
    """
    callee = call_node.child_by_field_name("function")
    if callee is None or callee.type != "member_expression":
        return None
    method = callee.child_by_field_name("property")
    obj = callee.child_by_field_name("object")
    if method is None or obj is None or obj.type != "call_expression":
        return None
    fn = obj.child_by_field_name("function")
    if fn is None:
        return None
    chain = _callee_chain(fn)
    prop = None
    if chain == ["this", "get"]:
        prop = _first_string_arg(obj)[0]
    elif fn.type == "identifier" and _text(fn) == "get":
        args = obj.child_by_field_name("arguments")
        named = [c for c in args.children if c.is_named] if args is not None else []
        if len(named) >= 2 and named[0].type == "this":
            prop = string_value(named[1])
    if not prop or not re.fullmatch(r"[A-Za-z_$][\w$]*", prop):
        return None
    return prop, _text(method)


def _object_pair_value(obj, name: str):
    for child in obj.children:
        if child.type != "pair":
            continue
        key = child.child_by_field_name("key")
        if key is None:
            continue
        label = string_value(key) if key.type == "string" else _text(key)
        if label == name:
            return child.child_by_field_name("value")
    return None


def _extract_routes(map_call, file_path: str, nodes: list["NodeInfo"],
                    node_info_cls, language: str) -> None:
    """Turn ``Router.map(function () { this.route(...) })`` into route nodes."""
    args = map_call.child_by_field_name("arguments")
    callback = next((c for c in args.children if c.is_named), None) if args else None
    if callback is None:
        return
    emitted: dict[str, int] = {}
    count = 0

    def body_of(fn):
        return fn.child_by_field_name("body") if fn is not None else None

    def visit_body(body, parent_name: str, parent_url: str, depth: int) -> None:
        nonlocal count
        if body is None or depth > MAX_ROUTE_DEPTH:
            return
        statements = [body] if body.type != "statement_block" else list(body.children)
        for stmt in statements:
            call = stmt
            if stmt.type == "expression_statement":
                call = next((c for c in stmt.children if c.is_named), None)
            if call is None or call.type != "call_expression":
                continue
            callee = call.child_by_field_name("function")
            chain = _callee_chain(callee) if callee is not None else []
            if len(chain) != 2 or chain[0] != "this" or chain[1] not in (
                "route", "resource", "mount",
            ):
                continue
            if count >= MAX_ROUTES:
                return
            call_args = call.child_by_field_name("arguments")
            named = [c for c in call_args.children if c.is_named] if call_args else []
            if not named:
                continue
            name = string_value(named[0])
            if not name:
                continue
            options = next((c for c in named[1:] if c.type == "object"), None)
            nested = next(
                (c for c in named[1:]
                 if c.type in ("function_expression", "function", "arrow_function")),
                None,
            )
            path_override: Optional[str] = None
            path_dynamic = False
            reset = chain[1] == "resource"
            if options is not None:
                path_node = _object_pair_value(options, "path")
                if path_node is not None:
                    path_override = string_value(path_node)
                    path_dynamic = path_override is None
                reset_node = _object_pair_value(options, "resetNamespace")
                if reset_node is not None and _text(reset_node) == "true":
                    reset = True
            segment = path_override if path_override is not None else f"/{name}"
            if segment and not segment.startswith("/"):
                segment = "/" + segment
            url = (parent_url.rstrip("/") + segment) or "/"
            full = name if (reset or not parent_name) else f"{parent_name}.{name}"
            count += 1
            key = f"route:{full}"
            seen = emitted.get(key, 0)
            emitted[key] = seen + 1
            node_name = key if seen == 0 else f"{key}@L{call.start_point[0] + 1}"
            extra: dict = {
                "framework": "ember",
                "ember_kind": "mount" if chain[1] == "mount" else "route",
                "route_name": full,
                "route": url,
            }
            if path_override is not None:
                extra["path_override"] = True
            if path_dynamic:
                extra["path_dynamic"] = True
            if reset and parent_name:
                extra["reset_namespace"] = True
            nodes.append(node_info_cls(
                kind="Endpoint",
                name=node_name,
                file_path=file_path,
                line_start=call.start_point[0] + 1,
                line_end=call.end_point[0] + 1,
                language=language,
                extra=extra,
            ))
            if nested is not None and chain[1] != "mount":
                visit_body(body_of(nested), full, url, depth + 1)

    visit_body(body_of(callback), "", "", 0)
