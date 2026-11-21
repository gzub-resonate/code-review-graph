"""Scala syntax facts the cross-file resolver needs, read from the Tree-sitter tree.

Nothing here resolves a name. The parser records, per file, what the source
says: the package clauses a declaration sits in, which kind of type it is, what
each import brings in (with renames), and, for every call, how it is written
(``bare``, ``member`` or ``new``), what its receiver is and what that receiver
was declared as. ``scala_resolver`` then binds those facts across files.

The receiver's declared type is found by walking the call's enclosing scopes
for the nearest binding of the receiver name. Anything that binds the name in a
way this module cannot type (a lambda parameter, a pattern variable, a
generator, a tuple pattern, a ``def``) is recorded as a local binding of unknown
type, so the resolver never mistakes such a value for an object of the same
name elsewhere in the repository.
"""

from __future__ import annotations

import threading
from typing import Optional

# Bounds, so a pathological file cannot make one call cost unbounded work.
_MAX_SCOPE_DEPTH = 200
_MAX_PATH_SEGMENTS = 12

_STATE = threading.local()

_TYPE_KINDS = {
    "class_definition": "class",
    "trait_definition": "trait",
    "object_definition": "object",
    "enum_definition": "enum",
}

_PATTERN_BINDER_PARENTS = frozenset({
    "case_clause", "enumerator", "val_definition", "var_definition",
})


def begin_file() -> None:
    """Start a per-file scope cache. Call before walking one file's tree."""
    _STATE.cache = {}


def end_file() -> None:
    """Drop the per-file cache."""
    _STATE.cache = None


def _cache() -> dict:
    cache = getattr(_STATE, "cache", None)
    if cache is None:
        cache = {}
        _STATE.cache = cache
    return cache


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _has_child(node, node_type: str) -> bool:
    return any(child.type == node_type for child in node.children)


# ---------------------------------------------------------------------------
# Packages and declarations
# ---------------------------------------------------------------------------


def _package_name(clause) -> str:
    for child in clause.children:
        if child.type == "package_identifier":
            return _text(child)
    return ""


def package_scopes(node) -> list[str]:
    """Packages whose members *node* sees, innermost first.

    ``package a.b`` names one package (``["a.b"]``); ``package a`` followed by
    ``package b`` also exposes the members of ``a`` (``["a.b", "a"]``). A braced
    ``package a { package b { ... } }`` nests the same way.
    """
    clauses: list[str] = []
    current = node.parent
    braced: list[str] = []
    depth = 0
    while current is not None and depth < _MAX_SCOPE_DEPTH:
        if current.type == "package_clause":
            braced.append(_package_name(current))
        elif current.type == "compilation_unit":
            for sibling in current.children:
                if sibling.end_byte > node.start_byte:
                    break
                if sibling.type == "package_clause" and not _has_child(
                    sibling, "template_body",
                ):
                    clauses.append(_package_name(sibling))
        current = current.parent
        depth += 1
    clauses.extend(reversed(braced))
    clauses = [clause for clause in clauses if clause]
    scopes: list[str] = []
    prefix = ""
    for clause in clauses:
        prefix = f"{prefix}.{clause}" if prefix else clause
        scopes.append(prefix)
    scopes.reverse()
    return scopes


def class_extra(node) -> dict:
    """``extra`` for a class, trait, object or enum node."""
    extra: dict = {}
    kind = _TYPE_KINDS.get(node.type)
    if kind is None:
        return extra
    if _has_child(node, "case"):
        kind = f"case_{kind}"
    extra["scala_kind"] = kind
    parents = _parent_types(node)
    if parents:
        extra["scala_parents"] = parents
    scopes = package_scopes(node)
    if scopes:
        extra["scala_package"] = scopes[0]
        if len(scopes) > 1:
            extra["scala_package_scopes"] = scopes
    return extra


def _parent_types(node) -> list[str]:
    """Types in the ``extends`` clause, as written; ``?`` for one that is no plain name."""
    parents: list[str] = []
    for clause in node.children:
        if clause.type != "extends_clause":
            continue
        for child in clause.children:
            if child.type in ("extends", "with", "arguments", ",", "comment"):
                continue
            if child.type in ("type_identifier", "stable_type_identifier", "generic_type"):
                parents.append(type_path(child) or "?")
            elif child.is_named:
                parents.append("?")
    return parents


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def function_extra(node) -> dict:
    """``extra`` for a Scala ``def``: whether subclasses can see it, and ``override``.

    A plain ``private`` or ``private[this]`` member is not inherited, so a call
    in a subclass cannot mean it. ``private[pkg]`` is visible inside ``pkg``.
    """
    extra: dict = {}
    for child in node.children:
        if child.type != "modifiers":
            continue
        for modifier in child.children:
            if modifier.type == "access_modifier" and _text(modifier).replace(
                " ", "",
            ) in ("private", "private[this]"):
                extra["scala_private"] = True
            elif modifier.type == "override":
                extra["scala_override"] = True
    return extra


def import_specs(node) -> list[dict]:
    """What one ``import`` statement brings in.

    Each entry has ``path`` (dotted, the imported name or, for a wildcard, its
    prefix) and optionally ``alias`` (the local name of a rename), ``wildcard``
    and ``given``. ``{A => _}`` hides a name and yields nothing.
    """
    parts: list[str] = []
    selectors = None
    wildcard = None
    for child in node.children:
        if child.type == "identifier":
            parts.append(_text(child))
        elif child.type == "namespace_selectors":
            selectors = child
        elif child.type == "namespace_wildcard":
            wildcard = child
    base = ".".join(parts)
    if not base:
        return []
    specs: list[dict] = []
    if selectors is not None:
        for sub in selectors.children:
            if sub.type == "identifier":
                specs.append({"path": f"{base}.{_text(sub)}"})
            elif sub.type in ("arrow_renamed_identifier", "as_renamed_identifier"):
                names = [c for c in sub.children if c.type == "identifier"]
                if len(names) == 2:
                    specs.append({
                        "path": f"{base}.{_text(names[0])}",
                        "alias": _text(names[1]),
                    })
                # ``A => _`` ends in a wildcard, not an identifier: a hiding.
            elif sub.type == "namespace_wildcard":
                specs.append(_wildcard_spec(base, sub))
    elif wildcard is not None:
        specs.append(_wildcard_spec(base, wildcard))
    else:
        specs.append({"path": base})
    return specs


def _wildcard_spec(base: str, node) -> dict:
    spec: dict = {"path": base, "wildcard": True}
    if _text(node) == "given":
        spec["given"] = True
    return spec


def import_edge_target(spec: dict) -> str:
    """The raw target string an import edge has always carried."""
    return f"{spec['path']}.*" if spec.get("wildcard") else spec["path"]


def import_edge_extra(spec: dict) -> dict:
    extra: dict = {"scala_import": spec["path"]}
    if spec.get("alias"):
        extra["scala_alias"] = spec["alias"]
    if spec.get("wildcard"):
        extra["scala_wildcard"] = True
    if spec.get("given"):
        extra["scala_given"] = True
    return extra


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


def type_path(node) -> Optional[str]:
    """The dotted name a type expression denotes, or ``None`` when it is not one.

    ``Foo``, ``a.b.Foo``, ``Foo[A]`` and the by-name ``=> Foo`` give a name.
    Function types, tuples, unions, projections, singleton types and repeated
    parameters do not: the value is not an instance of one nameable type.
    """
    if node is None:
        return None
    kind = node.type
    if kind == "type_identifier":
        return _text(node)
    if kind == "stable_type_identifier":
        text = _text(node)
        segments = text.split(".")
        if len(segments) > _MAX_PATH_SEGMENTS or not all(
            segment.isidentifier() for segment in segments
        ):
            return None
        return text
    if kind == "generic_type":
        for child in node.children:
            if child.type in ("type_identifier", "stable_type_identifier"):
                return type_path(child)
        return None
    if kind == "lazy_parameter_type":
        for child in node.children:
            if child.type != "=>":
                return type_path(child)
    return None


def _instantiated_type(node) -> Optional[str]:
    for child in node.children:
        if child.type in ("type_identifier", "stable_type_identifier", "generic_type"):
            return type_path(child)
    return None


# ---------------------------------------------------------------------------
# Scope walk: what a receiver name is bound to
# ---------------------------------------------------------------------------


def _pattern_identifiers(node, found: list) -> None:
    """Every variable a pattern binds (identifiers, not type names)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "identifier":
            found.append(current)
            continue
        if current.type in ("arguments", "type_arguments", "block", "guard"):
            continue
        stack.extend(current.children)


def _value_binder(definition) -> list[tuple[str, dict]]:
    """Names a val/var definition binds, with what is known of each."""
    pattern = definition.child_by_field_name("pattern")
    if pattern is None:
        return []
    if pattern.type != "identifier":
        found: list = []
        _pattern_identifiers(pattern, found)
        return [(_text(n), {"local": True}) for n in found]
    info: dict = {"local": True}
    declared = definition.child_by_field_name("type")
    value = definition.child_by_field_name("value")
    if declared is not None:
        path = type_path(declared)
        if path:
            info["type"] = path
    elif value is not None:
        if value.type == "instance_expression":
            path = _instantiated_type(value)
            if path and not _has_child(value, "template_body"):
                info["new"] = path
        elif value.type == "call_expression":
            callee = value.child_by_field_name("function")
            if callee is not None and callee.type == "identifier":
                info["apply"] = _text(callee)
    return [(_text(pattern), info)]


def _parameter_binders(parameters) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for param in parameters.children:
        if param.type not in ("parameter", "class_parameter", "binding"):
            continue
        name = None
        declared = None
        for child in param.children:
            if child.type == "identifier" and name is None:
                name = _text(child)
            elif child.type not in (":", "identifier", "val", "var", "="):
                if declared is None and child.type.endswith(
                    ("type", "type_identifier", "type_arguments"),
                ):
                    declared = child
        if name is None:
            continue
        info: dict = {"local": True}
        path = type_path(declared) if declared is not None else None
        if path:
            info["type"] = path
        found.append((name, info))
    return found


def _scope_table(scope) -> dict[str, list[tuple[int, dict]]]:
    """Names a scope node binds, each with the byte offset where it starts."""
    cache = _cache()
    key = (scope.id, scope.type)
    table = cache.get(key)
    if table is not None:
        return table
    table = {}

    def add(name: str, offset: int, info: dict) -> None:
        table.setdefault(name, []).append((offset, info))

    kind = scope.type
    if kind in ("block", "indented_block", "template_body", "compilation_unit"):
        for stmt in scope.children:
            if stmt.type in ("val_definition", "var_definition"):
                for name, info in _value_binder(stmt):
                    add(name, stmt.start_byte, info)
            elif stmt.type == "function_definition":
                name_node = stmt.child_by_field_name("name")
                if name_node is not None:
                    add(_text(name_node), -1, {"local": True})
            elif stmt.type == "given_definition":
                name_node = next(
                    (c for c in stmt.children if c.type == "identifier"), None,
                )
                if name_node is not None:
                    add(_text(name_node), -1, {"local": True})
    elif kind == "function_definition":
        for child in scope.children:
            if child.type == "parameters":
                for name, info in _parameter_binders(child):
                    add(name, -1, info)
    elif kind == "class_definition":
        for child in scope.children:
            if child.type == "class_parameters":
                for name, info in _parameter_binders(child):
                    add(name, -1, info)
    elif kind == "lambda_expression":
        for child in scope.children:
            if child.type == "bindings":
                for name, info in _parameter_binders(child):
                    add(name, -1, info)
            elif child.type == "identifier":
                add(_text(child), -1, {"local": True})
            elif child.type == "=>":
                break
    elif kind == "case_clause":
        found: list = []
        for child in scope.children:
            if child.type == "=>":
                break
            if child.type != "case":
                _pattern_identifiers(child, found)
        for ident in found:
            add(_text(ident), -1, {"local": True})
    elif kind == "enumerators":
        found = []
        for enumerator in scope.children:
            if enumerator.type != "enumerator":
                continue
            for child in enumerator.children:
                if child.type in ("<-", "="):
                    break
                _pattern_identifiers(child, found)
        for ident in found:
            add(_text(ident), -1, {"local": True})
    cache[key] = table
    return table


def lookup_binding(node, name: str) -> Optional[dict]:
    """The nearest binding of *name* visible at *node*, or ``None`` if unbound.

    Returns a dict with ``local`` and, when declared, ``type``, ``new`` or
    ``apply``.
    """
    current = node
    depth = 0
    while current.parent is not None and depth < _MAX_SCOPE_DEPTH:
        parent = current.parent
        table = _scope_table(parent)
        entries = table.get(name)
        if entries:
            for offset, info in entries:
                # Block-local values must precede the use; members, parameters
                # and local defs are visible throughout.
                if offset == -1 or offset < node.start_byte:
                    return info
        current = parent
        depth += 1
    return None


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


def _path_text(node) -> Optional[str]:
    """Dotted identifier path of a receiver, or ``None`` for any other expression."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "field_expression":
        parts: list[str] = []
        current = node
        while current is not None and current.type == "field_expression":
            field = current.child_by_field_name("field")
            if field is None or field.type != "identifier":
                return None
            parts.append(_text(field))
            current = current.child_by_field_name("value")
            if len(parts) > _MAX_PATH_SEGMENTS:
                return None
        if current is None or current.type != "identifier":
            return None
        parts.append(_text(current))
        return ".".join(reversed(parts))
    return None


def _argument_count(node) -> Optional[int]:
    """Arguments in the first argument list, or ``None`` when it is not countable.

    ``f(xs: _*)`` spreads a sequence, so the count says nothing about arity.
    """
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        arguments = next((c for c in node.children if c.type == "arguments"), None)
    if arguments is None:
        for child in node.children:
            if child.type in ("block", "case_block", "indented_block"):
                return 1  # ``f { ... }`` passes its block as the one argument.
        return None
    count = 0
    for child in arguments.named_children:
        if child.type in ("comment", "block_comment"):
            continue
        if child.type == "typed_expression" and _text(child).rstrip().endswith(("_*", "*")):
            return None
        count += 1
    return count


def call_extra(node, name: str) -> dict:
    """``extra`` for one Scala call or instantiation node.

    ``scala_form`` is always present on a call this parser version read, which
    is how the resolver tells an edge it can interpret from one stored by an
    older release.
    """
    extra: dict = {"scala_name": name}
    argc = _argument_count(node)
    if argc is not None:
        extra["scala_argc"] = argc
    if node.type == "instance_expression":
        extra["scala_form"] = "new"
        path = _instantiated_type(node)
        if path:
            extra["scala_type"] = path
        return extra
    callee = node.child_by_field_name("function")
    while callee is not None and callee.type == "generic_function":
        callee = callee.child_by_field_name("function")
    if callee is None:
        extra["scala_form"] = "other"
        return extra
    if callee.type == "identifier":
        extra["scala_form"] = "bare"
        if lookup_binding(node, _text(callee)) is not None:
            extra["scala_shadowed"] = True
        return extra
    if callee.type != "field_expression":
        extra["scala_form"] = "other"
        return extra
    extra["scala_form"] = "member"
    receiver = callee.child_by_field_name("value")
    path = _path_text(receiver) if receiver is not None else None
    if not path:
        if receiver is not None and receiver.type == "instance_expression":
            built = _instantiated_type(receiver)
            if built and not _has_child(receiver, "template_body"):
                extra["scala_receiver_expr_new"] = built
        return extra
    extra["scala_receiver"] = path
    head = path.split(".", 1)[0]
    binding = lookup_binding(node, head)
    if binding is None:
        return extra
    extra["scala_receiver_local"] = True
    if "." in path:
        return extra  # A field of a value: nothing here types it.
    for key in ("type", "new", "apply"):
        if key in binding:
            extra[f"scala_receiver_{key}"] = binding[key]
    return extra
