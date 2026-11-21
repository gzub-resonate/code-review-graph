"""Link Scala imports and calls across files, from declarations and scopes.

The parser records, per Scala file, its package clauses, the kind of each type
(``object``, ``class``, ``trait``, ``case_class``...), every import (with renames
and wildcards) and, on every call, how it is written and what its receiver was
declared as (see ``scala_parse``). This pass runs after the whole graph is
stored and binds those facts across files. It is a structural binder, not a
compiler: it never executes project code, never reads outside the graph, and
leaves anything it cannot prove as it was.

Resolution order for a simple name (types and objects)
------------------------------------------------------

1. A type nested in an enclosing type, innermost first.
2. A top-level type of the same file, in the file's packages.
3. An explicit import (``import a.b.Foo``, ``import a.b.{Foo => Bar}``). When it
   names something that is not in the repository the name is external and
   nothing below it is tried: a library name is never linked.
4. A wildcard import of an in-repository package or object.
5. A top-level type of another file, in the file's packages (innermost first).

Calls, in this order of evidence (each rule needs a unique answer):

* ``new Foo(...)``: the class ``Foo`` resolves by the order above.
* ``Foo(...)``: ``apply`` of the object ``Foo`` (inherited ones included), else
  the case class ``Foo`` itself.
* ``Foo.bar(...)`` and ``a.b.Foo.bar(...)``: the receiver resolves to an
  in-repository object and ``bar`` is a member of it or of an in-repository
  ancestor.
* ``x.bar(...)``: ``x`` is declared in scope as ``Foo`` (parameter, field, local
  ``val x: Foo`` or ``val x = new Foo``), and ``bar`` is a member of ``Foo`` or
  of an in-repository ancestor. A value of unknown type links nothing.
* ``this.bar(...)`` and ``super.bar(...)``: members of the enclosing type, or of
  its parents.
* ``bar(...)``: a member of the enclosing type chain (own or inherited from an
  in-repository parent), else a member brought in by an explicit or wildcard
  import of an in-repository object. If an enclosing type has a parent outside
  the repository the walk stops there, because that parent could declare the
  name.

Several candidates (overloads, an object declared twice, a member inherited from
two parents) are recorded as ``ambiguous_targets`` and never linked. A name is
never matched by itself alone. Edges this pass binds carry
``extra.scala_resolved`` (the rule) and the raw name in ``extra.scala_name``, so
every run recomputes them from the declarations and an incremental update ends
where a full build does.

Approximations, by design: an import anywhere in a file applies to the whole file;
``package object`` members, type aliases, ``given`` values, Scala 3 top-level
definitions and values reached through a field (``a.field.bar()``) are not
followed.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 20
_MAX_INHERITANCE_DEPTH = 25
_MAX_CHAIN_DEPTH = 50

_OBJECT_KINDS = frozenset({"object", "case_object"})
_FLOWS_DIRTY_KEY = "scala_flows_dirty"

_RESOLVED_KEYS = ("scala_resolved", "scala_via", "scala_ambiguous")
# Written by the store when a name is shared by several definitions; a bound
# call must not carry them, and neither must one handed back to its raw name.
_COLLISION_KEYS = (
    "ambiguous_targets", "ambiguous_target_count", "ambiguous_targets_truncated",
    "collision_endpoint", "confidence", "confidence_tier",
)


def _arity_bounds(params: Optional[str]) -> Optional[tuple[int, float]]:
    """Fewest and most arguments the first parameter list accepts, if readable."""
    if params is None:
        return None
    text = params.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return None
    inner = text[1:-1].strip()
    if inner.startswith("implicit ") or inner.startswith("using "):
        return None
    if not inner:
        return 0, 0
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for index, char in enumerate(inner):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    low = 0
    high: float = 0
    for part in parts:
        text_part = part.strip()
        if not text_part:
            continue
        high += 1
        if text_part.endswith("*") and ":" in text_part:
            high = float("inf")
            continue
        has_default = False
        nest = 0
        for position, char in enumerate(text_part):
            if char in "([{":
                nest += 1
            elif char in ")]}":
                nest -= 1
            elif (
                char == "=" and nest == 0
                and text_part[position + 1:position + 2] != ">"
                and text_part[position - 1:position] not in ("=", "!", "<", ">")
            ):
                has_default = True
                break
        if not has_default:
            low += 1
    return low, high


class _Type:
    __slots__ = (
        "qn", "name", "file", "kind", "scopes", "start", "end", "owner", "fqn",
    )

    def __init__(self, qn, name, file, kind, scopes, start, end):
        self.qn = qn
        self.name = name
        self.file = file
        self.kind = kind
        self.scopes = scopes
        self.start = start
        self.end = end
        self.owner: Optional[str] = None
        self.fqn: Optional[str] = None

    @property
    def is_object(self) -> bool:
        return self.kind in _OBJECT_KINDS


class _Import:
    __slots__ = ("edge_id", "path", "alias", "wildcard", "given", "line")

    def __init__(self, edge_id, path, alias, wildcard, given, line):
        self.edge_id = edge_id
        self.path = path
        self.alias = alias
        self.wildcard = wildcard
        self.given = given
        self.line = line

    @property
    def bound_name(self) -> str:
        return self.alias or self.path.rsplit(".", 1)[-1]


class _Context:
    """Where a name is used: file, enclosing types (innermost first), packages."""

    __slots__ = ("file", "chain", "scopes", "unknown_enclosing")

    def __init__(self, file, chain, scopes, unknown_enclosing=False):
        self.file = file
        self.chain = chain
        self.scopes = scopes
        self.unknown_enclosing = unknown_enclosing

    def key(self) -> tuple:
        return (self.file, self.chain[0].qn if self.chain else "", self.scopes)


def _extra(raw: Optional[str]) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class _Index:
    """Declarations of every Scala file in the graph, and name lookups over them."""

    def __init__(self, store: GraphStore, files: set[str]) -> None:
        conn = store._conn
        self.types: dict[str, _Type] = {}
        self.file_types: dict[str, list[_Type]] = {}
        self.functions: dict[str, tuple[str, str]] = {}  # qn -> (name, file)
        self.private: set[str] = set()  # members subclasses cannot see
        self.arity: dict[str, tuple[int, float]] = {}  # qn -> (min, max) arguments
        self.members: dict[tuple[str, str], list[str]] = {}
        self.nested: dict[tuple[str, str], list[_Type]] = {}
        self.contained_by: dict[str, str] = {}
        self.inherits: dict[str, list[str]] = {}
        self.written_parents: dict[str, list[str]] = {}
        self.all_qns: set[str] = set()
        self.imports: dict[str, list[_Import]] = {}
        self._lookup_cache: dict[tuple, tuple[str, tuple]] = {}
        self._parents_cache: dict[str, tuple[list[_Type], bool]] = {}
        self._member_cache: dict[tuple[str, str], tuple[list[str], bool]] = {}

        for row in conn.execute(
            "SELECT qualified_name, name, file_path, kind, line_start, line_end, extra, params "
            "FROM nodes WHERE language = 'scala' AND kind IN ('Class', 'Function', 'Test') "
            "ORDER BY qualified_name",
        ).fetchall():
            self.all_qns.add(row["qualified_name"])
            extra = _extra(row["extra"])
            if row["kind"] != "Class":
                self.functions[row["qualified_name"]] = (row["name"], row["file_path"])
                if extra.get("scala_private"):
                    self.private.add(row["qualified_name"])
                bounds = _arity_bounds(row["params"])
                if bounds is not None:
                    self.arity[row["qualified_name"]] = bounds
                continue
            kind = extra.get("scala_kind")
            if not isinstance(kind, str):
                continue
            scopes = extra.get("scala_package_scopes")
            if not isinstance(scopes, list):
                package = extra.get("scala_package")
                scopes = [package] if isinstance(package, str) and package else []
            node = _Type(
                row["qualified_name"], row["name"], row["file_path"], kind,
                tuple(str(s) for s in scopes), row["line_start"], row["line_end"],
            )
            written = extra.get("scala_parents")
            if isinstance(written, list):
                self.written_parents[node.qn] = [str(p) for p in written]
            self.types[node.qn] = node
            self.file_types.setdefault(node.file, []).append(node)

        for row in conn.execute(
            "SELECT e.source_qualified, e.target_qualified FROM edges e "
            "JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
            "AND f.language = 'scala' WHERE e.kind = 'CONTAINS' "
            "ORDER BY e.source_qualified, e.target_qualified",
        ).fetchall():
            source, target = row["source_qualified"], row["target_qualified"]
            if target in self.types and source in self.types:
                self.types[target].owner = source
            if target in self.functions or target in self.types:
                self.contained_by.setdefault(target, source)
            if source in self.types and target in self.functions:
                self.members.setdefault((source, self.functions[target][0]), []).append(target)
        for node in self.types.values():
            if node.owner:
                self.nested.setdefault((node.owner, node.name), []).append(node)

        for row in conn.execute(
            "SELECT e.source_qualified, e.target_qualified FROM edges e "
            "JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
            "AND f.language = 'scala' WHERE e.kind = 'INHERITS' "
            "ORDER BY e.id",
        ).fetchall():
            self.inherits.setdefault(row["source_qualified"], []).append(
                row["target_qualified"],
            )

        for row in conn.execute(
            "SELECT e.id, e.file_path, e.line, e.extra FROM edges e "
            "JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
            "AND f.language = 'scala' WHERE e.kind = 'IMPORTS_FROM' "
            "AND e.extra LIKE '%\"scala_import\"%' ORDER BY e.id",
        ).fetchall():
            extra = _extra(row["extra"])
            path = extra.get("scala_import")
            if not isinstance(path, str) or not path:
                continue
            alias = extra.get("scala_alias")
            self.imports.setdefault(row["file_path"], []).append(_Import(
                row["id"], path, alias if isinstance(alias, str) else None,
                bool(extra.get("scala_wildcard")), bool(extra.get("scala_given")),
                row["line"],
            ))

        self.fqn_index: dict[str, list[_Type]] = {}
        self.prefixes: set[str] = set()
        for node in sorted(self.types.values(), key=lambda t: t.qn):
            node.fqn = self._fqn(node)
            if node.fqn is None:
                continue
            self.fqn_index.setdefault(node.fqn, []).append(node)
            if node.is_object:
                self.prefixes.add(node.fqn)
            if not node.owner and node.scopes:
                parts = node.scopes[0].split(".")
                for end in range(1, len(parts) + 1):
                    self.prefixes.add(".".join(parts[:end]))

    # -- identity ---------------------------------------------------------

    def _fqn(self, node: _Type) -> Optional[str]:
        parts = [node.name]
        current = node
        for _ in range(_MAX_CHAIN_DEPTH):
            if not current.owner:
                package = current.scopes[0] if current.scopes else ""
                return ".".join([package, *reversed(parts)]) if package else ".".join(
                    reversed(parts),
                )
            owner = self.types.get(current.owner)
            if owner is None or not owner.is_object:
                return None  # Inner type of a class: no static path reaches it.
            parts.append(owner.name)
            current = owner
        return None

    # -- contexts ---------------------------------------------------------

    def file_scopes(self, file: str) -> tuple[str, ...]:
        scopes = {t.scopes for t in self.file_types.get(file, ())}
        return next(iter(scopes)) if len(scopes) == 1 else ()

    def context_for(self, file: str, line: int, caller: str) -> _Context:
        owner = self.contained_by.get(caller)
        for _ in range(_MAX_CHAIN_DEPTH):
            if owner is None or owner in self.types:
                break
            owner = self.contained_by.get(owner)
        chain: list[_Type] = []
        unknown = False
        if owner in self.types:
            current: Optional[str] = owner
        else:
            spans = [
                t for t in self.file_types.get(file, ())
                if t.start <= line <= t.end
            ]
            current = None
            if spans:
                spans.sort(key=lambda t: (t.end - t.start, t.qn))
                best = spans[0]
                tied = [t for t in spans if (t.end - t.start) == (best.end - best.start)]
                if len(tied) > 1 and len({t.owner for t in tied} | {None}) > 1:
                    # Distinct declarations on the same lines: nothing says which
                    # one holds the call.
                    unknown = True
                else:
                    current = best.qn
        for _ in range(_MAX_CHAIN_DEPTH):
            if current is None or current not in self.types:
                break
            node = self.types[current]
            chain.append(node)
            current = node.owner
        scopes = chain[0].scopes if chain else self.file_scopes(file)
        return _Context(file, chain, tuple(scopes), unknown)

    # -- names ------------------------------------------------------------

    def resolve_path(self, path: str, scopes: tuple[str, ...]) -> list[_Type]:
        """Types whose full name is *path*, read from the nearest package out."""
        for variant in [*(f"{s}.{path}" for s in scopes), path]:
            hits = self.fqn_index.get(variant)
            if hits:
                return hits
        return []

    def resolve_prefix(self, path: str, scopes: tuple[str, ...]) -> Optional[str]:
        for variant in [*(f"{s}.{path}" for s in scopes), path]:
            if variant in self.prefixes:
                return variant
        return None

    def lookup(self, name: str, ctx: _Context) -> tuple[str, tuple[_Type, ...]]:
        """Resolve a simple name: ``found`` (types), ``external`` or ``none``."""
        key = (*ctx.key(), name)
        cached = self._lookup_cache.get(key)
        if cached is not None:
            return cached
        result = self._lookup(name, ctx)
        self._lookup_cache[key] = result
        return result

    def _lookup(self, name: str, ctx: _Context) -> tuple[str, tuple[_Type, ...]]:
        for enclosing in ctx.chain:
            hits = self.nested.get((enclosing.qn, name))
            if hits:
                return "found", tuple(hits)
        scopes = (*ctx.scopes, "")
        for scope in scopes:
            fqn = f"{scope}.{name}" if scope else name
            same = [t for t in self.fqn_index.get(fqn, ()) if t.file == ctx.file]
            if same:
                return "found", tuple(same)
        imports = self.imports.get(ctx.file, ())
        explicit = [
            i for i in imports
            if not i.wildcard and not i.given and i.bound_name == name
        ]
        if explicit:
            found: dict[str, _Type] = {}
            for imp in explicit:
                for node in self.resolve_path(imp.path, ctx.scopes):
                    found[node.qn] = node
            if found:
                return "found", tuple(found[qn] for qn in sorted(found))
            return "external", ()
        found = {}
        for imp in imports:
            if not imp.wildcard or imp.given:
                continue
            prefix = self.resolve_prefix(imp.path, ctx.scopes)
            if prefix is None:
                continue
            for node in self.fqn_index.get(f"{prefix}.{name}", ()):
                found[node.qn] = node
        if found:
            return "found", tuple(found[qn] for qn in sorted(found))
        for scope in scopes:
            fqn = f"{scope}.{name}" if scope else name
            hits = self.fqn_index.get(fqn)
            if hits:
                return "found", tuple(hits)
        return "none", ()

    def resolve_type_path(self, path: str, ctx: _Context) -> tuple[str, tuple[_Type, ...]]:
        head, _, rest = path.partition(".")
        if not rest:
            return self.lookup(path, ctx)
        status, nodes = self.lookup(head, ctx)
        if status == "found":
            objects = [n for n in nodes if n.is_object]
            for segment in rest.split("."):
                nxt = [c for o in objects for c in self.nested.get((o.qn, segment), ())]
                objects = nxt
            return ("found", tuple(objects)) if objects else ("none", ())
        if status == "external":
            return "external", ()
        hits = self.resolve_path(path, ctx.scopes)
        return ("found", tuple(hits)) if hits else ("none", ())

    # -- inheritance and members ------------------------------------------

    def parents(self, node: _Type) -> tuple[list[_Type], bool]:
        """In-repository parents of a type, and whether any parent is not one."""
        cached = self._parents_cache.get(node.qn)
        if cached is not None:
            return cached
        result: list[_Type] = []
        unknown = False
        chain: list[_Type] = []
        current = node.owner
        for _ in range(_MAX_CHAIN_DEPTH):
            if current is None or current not in self.types:
                break
            chain.append(self.types[current])
            current = self.types[current].owner
        ctx = _Context(node.file, chain, node.scopes)
        raws = self.written_parents.get(node.qn)
        if raws is None:
            raws = self.inherits.get(node.qn, [])
        for raw in raws:
            if raw == "?":
                unknown = True
                continue
            if raw in self.types:
                result.append(self.types[raw])
                continue
            status, hits = self.lookup(raw, ctx) if "." not in raw else (
                self.resolve_type_path(raw, ctx)
            )
            kinds = [h for h in hits if not h.is_object and h.qn != node.qn]
            if status == "found" and len(kinds) == 1:
                result.append(kinds[0])
            else:
                unknown = True
        value = (result, unknown)
        self._parents_cache[node.qn] = value
        return value

    def find_members(self, node: _Type, name: str) -> tuple[list[str], bool]:
        """Functions named *name* on a type or its in-repository ancestors.

        Returns the distinct declarations and whether the walk met a parent
        outside the repository before finding any.
        """
        key = (node.qn, name)
        cached = self._member_cache.get(key)
        if cached is not None:
            return cached
        value = self._find_members(node, name, set(), 0)
        self._member_cache[key] = value
        return value

    def _find_members(
        self, node: _Type, name: str, seen: set[str], depth: int,
    ) -> tuple[list[str], bool]:
        if node.qn in seen or depth > _MAX_INHERITANCE_DEPTH:
            return [], False
        seen.add(node.qn)
        own = self.members.get((node.qn, name))
        if own and depth:
            own = [qn for qn in own if qn not in self.private]
        if own:
            return sorted(set(own)), False
        parents, unknown = self.parents(node)
        found: set[str] = set()
        for parent in parents:
            hits, parent_unknown = self._find_members(parent, name, seen, depth + 1)
            found.update(hits)
            unknown = unknown or parent_unknown
        return sorted(found), unknown


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class _Outcome:
    __slots__ = ("targets", "rule", "via", "ambiguous")

    def __init__(self, targets, rule, via="", ambiguous=False):
        self.targets = targets
        self.rule = rule
        self.via = via
        self.ambiguous = ambiguous


def _objects(nodes) -> list[_Type]:
    return [n for n in nodes if n.is_object]


def _classes(nodes) -> list[_Type]:
    return [n for n in nodes if not n.is_object]


def _fits(index: _Index, qns, argc: Optional[int]) -> list[str]:
    """Declarations that can take *argc* arguments (all of them if unknown)."""
    if argc is None:
        return list(qns)
    return [
        qn for qn in qns
        if qn not in index.arity or index.arity[qn][0] <= argc <= index.arity[qn][1]
    ]


def _members_outcome(
    index: _Index, owners, name: str, rule: str, via: str, argc: Optional[int] = None,
) -> Optional[_Outcome]:
    owners = sorted(set(owners), key=lambda t: t.qn)
    found: set[str] = set()
    for owner in owners:
        hits, _unknown = index.find_members(owner, name)
        found.update(hits)
    # A declaration that cannot take this many arguments is another overload
    # (often one an out-of-repository parent declares).
    found = set(_fits(index, found, argc))
    if not found:
        return None
    targets = sorted(found)
    if len(targets) == 1 and len(owners) == 1:
        return _Outcome(targets, rule, via)
    return _Outcome(targets[:_MAX_CANDIDATES], rule, via, ambiguous=True)


def _receiver_objects(index: _Index, path: str, ctx: _Context) -> list[_Type]:
    status, nodes = index.resolve_type_path(path, ctx)
    return _objects(nodes) if status == "found" else []


def _declared_type(index: _Index, extra: dict, ctx: _Context) -> list[_Type]:
    """In-repository classes a receiver value was declared (or built) as."""
    declared = extra.get("scala_receiver_type") or extra.get("scala_receiver_new")
    if isinstance(declared, str):
        status, nodes = index.resolve_type_path(declared, ctx)
        return _classes(nodes) if status == "found" else []
    applied = extra.get("scala_receiver_apply")
    if isinstance(applied, str):
        status, nodes = index.lookup(applied, ctx)
        if status == "found":
            cases = [n for n in nodes if n.kind == "case_class"]
            if len(cases) == 1 and not [n for n in nodes if n.is_object and (
                index.members.get((n.qn, "apply"))
            )]:
                return cases
    return []


def _apply_outcome(index: _Index, nodes, argc: Optional[int] = None) -> Optional[_Outcome]:
    """``Foo(...)``: an object's ``apply``, or a case class's own constructor.

    A case class with an explicit ``apply`` on its companion has two: the one
    written and the one the compiler adds. Nothing at the call says which.
    """
    objects = _objects(nodes)
    cases = [n for n in nodes if n.kind == "case_class"]
    explicit = (
        _members_outcome(
            index, objects, "apply", "apply", ",".join(o.qn for o in objects), argc,
        )
        if objects else None
    )
    if cases and len(cases) == 1:
        if explicit is None:
            return _Outcome([cases[0].qn], "case_class_constructor", cases[0].qn)
        targets = sorted({*explicit.targets, cases[0].qn})
        return _Outcome(targets[:_MAX_CANDIDATES], "apply", ambiguous=True)
    return explicit


def _resolve_call(index: _Index, extra: dict, ctx: _Context) -> Optional[_Outcome]:
    form = extra.get("scala_form")
    name = extra.get("scala_name")
    if not isinstance(name, str) or not name:
        return None
    argc = extra.get("scala_argc")
    argc = argc if isinstance(argc, int) else None
    if form == "new":
        path = extra.get("scala_type")
        if not isinstance(path, str):
            return None
        status, nodes = index.resolve_type_path(path, ctx)
        classes = _classes(nodes) if status == "found" else []
        if len(classes) == 1:
            return _Outcome([classes[0].qn], "constructor", classes[0].qn)
        if len(classes) > 1:
            return _Outcome(
                sorted(c.qn for c in classes)[:_MAX_CANDIDATES], "constructor",
                ambiguous=True,
            )
        return None
    if form == "bare":
        if extra.get("scala_shadowed"):
            return None
        return _resolve_bare(index, name, ctx, argc)
    if form != "member":
        return None
    receiver = extra.get("scala_receiver")
    if not isinstance(receiver, str) or not receiver:
        built = extra.get("scala_receiver_expr_new")
        if isinstance(built, str):
            owners = _declared_type(index, {"scala_receiver_new": built}, ctx)
            if owners:
                return _members_outcome(
                    index, owners, name, "typed_receiver",
                    ",".join(sorted(o.qn for o in owners)), argc,
                )
        return None
    if receiver == "this":
        if ctx.unknown_enclosing or not ctx.chain:
            return None
        return _members_outcome(
            index, [ctx.chain[0]], name, "this_member", ctx.chain[0].qn, argc,
        )
    if receiver == "super":
        if ctx.unknown_enclosing or not ctx.chain:
            return None
        parents, _unknown = index.parents(ctx.chain[0])
        return _members_outcome(
            index, parents, name, "super_member", ctx.chain[0].qn, argc,
        )
    if extra.get("scala_receiver_local"):
        if "." in receiver:
            return None
        owners = _declared_type(index, extra, ctx)
        if not owners:
            return None
        return _members_outcome(
            index, owners, name, "typed_receiver", ",".join(sorted(o.qn for o in owners)),
            argc,
        )
    objects = _receiver_objects(index, receiver, ctx)
    if not objects:
        return None
    outcome = _members_outcome(
        index, objects, name, "object_member", ",".join(sorted(o.qn for o in objects)),
        argc,
    )
    if name == "apply":
        found = index.resolve_type_path(receiver, ctx)[1]
        if any(n.kind == "case_class" for n in found):
            return _apply_outcome(index, found, argc)
    return outcome


def _resolve_bare(
    index: _Index, name: str, ctx: _Context, argc: Optional[int] = None,
) -> Optional[_Outcome]:
    # Foo(...): apply of an object, or the constructor of a case class.
    status, nodes = index.lookup(name, ctx)
    if status == "found" and name[:1].isupper():
        groups = {n.fqn or n.qn for n in nodes}
        if len(groups) > 1:
            return _Outcome(
                sorted(n.qn for n in nodes)[:_MAX_CANDIDATES], "apply", ambiguous=True,
            )
        return _apply_outcome(index, nodes, argc)
    if ctx.unknown_enclosing:
        return None
    # Members of the enclosing types, innermost first.
    chain_unknown = False
    for enclosing in ctx.chain:
        found, unknown = index.find_members(enclosing, name)
        hits = _fits(index, found, argc)
        if found and not hits:
            # Declared here, but not for this many arguments: another overload,
            # most likely an out-of-repository parent's.
            return None
        if hits:
            if len(hits) == 1:
                return _Outcome(hits, "enclosing_member", enclosing.qn)
            return _Outcome(hits[:_MAX_CANDIDATES], "enclosing_member", ambiguous=True)
        _known, has_unknown = index.parents(enclosing)
        if unknown or has_unknown:
            # A parent outside the repository may declare the name, and an
            # inherited member beats anything imported.
            chain_unknown = True
            break
    if chain_unknown:
        return None
    imports = index.imports.get(ctx.file, ())
    for imp in imports:
        if imp.wildcard or imp.given or imp.bound_name != name:
            continue
        prefix = imp.path.rpartition(".")[0]
        owners = _objects(index.resolve_path(prefix, ctx.scopes)) if prefix else []
        if not owners:
            return None  # Imported from outside the repository.
        return _members_outcome(
            index, owners, imp.path.rsplit(".", 1)[-1], "imported_member",
            ",".join(sorted(o.qn for o in owners)), argc,
        )
    owners_by_import: dict[str, _Type] = {}
    for imp in imports:
        if not imp.wildcard or imp.given:
            continue
        owner_path = index.resolve_path(imp.path, ctx.scopes)
        for owner in _objects(owner_path):
            owners_by_import[owner.qn] = owner
    if not owners_by_import:
        return None
    return _members_outcome(
        index, list(owners_by_import.values()), name, "wildcard_member",
        ",".join(sorted(owners_by_import)), argc,
    )


def _resolve_import(index: _Index, imp: _Import, scopes: tuple[str, ...]) -> Optional[str]:
    """The one file a Scala import brings in, or ``None``."""
    if imp.given:
        return None
    files: set[str] = set()
    if imp.wildcard:
        files = {t.file for t in _objects(index.resolve_path(imp.path, scopes))}
    else:
        files = {t.file for t in index.resolve_path(imp.path, scopes)}
        if not files:
            prefix = imp.path.rpartition(".")[0]
            owners = _objects(index.resolve_path(prefix, scopes)) if prefix else []
            name = imp.path.rsplit(".", 1)[-1]
            for owner in owners:
                if index.find_members(owner, name)[0]:
                    files.add(owner.file)
    return next(iter(files)) if len(files) == 1 else None


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def resolve_scala_calls(store: GraphStore) -> dict[str, int]:
    """Bind Scala imports and calls across files. Safe to run repeatedly."""
    conn = store._conn
    files = {
        row["file_path"]
        for row in conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File' AND language = 'scala'",
        ).fetchall()
    }
    stats = {
        "files_indexed": len(files),
        "imports_resolved": 0,
        "calls_resolved": 0,
        "calls_ambiguous": 0,
        "calls_restored": 0,
    }
    if not files:
        return stats
    index = _Index(store, files)

    updates: list[tuple] = []
    mirrors: list[tuple] = []
    changed_calls = False

    # Imports first: a raw edge stays raw when nothing in the repository matches.
    for file in sorted(index.imports):
        scopes = index.file_scopes(file)
        for imp in index.imports[file]:
            row = conn.execute(
                "SELECT target_qualified, extra FROM edges WHERE id = ?", (imp.edge_id,),
            ).fetchone()
            if row is None:
                continue
            extra = _extra(row["extra"])
            raw = f"{imp.path}.*" if imp.wildcard else imp.path
            resolved = _resolve_import(index, imp, scopes)
            desired_extra = {k: v for k, v in extra.items() if k != "import_resolution"}
            if resolved is not None:
                desired_target = resolved
                desired_extra["import_resolution"] = "scala_scope"
                stats["imports_resolved"] += 1
            else:
                desired_target = raw
            if desired_target != row["target_qualified"] or desired_extra != extra:
                updates.append((
                    desired_target, json.dumps(desired_extra, sort_keys=True), imp.edge_id,
                ))

    contexts: dict[tuple, _Context] = {}
    call_rows = conn.execute(
        "SELECT e.id, e.source_qualified, e.target_qualified, e.file_path, e.line, e.extra "
        "FROM edges e JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
        "AND f.language = 'scala' WHERE e.kind = 'CALLS' "
        "AND e.extra LIKE '%\"scala_form\"%' ORDER BY e.id",
    ).fetchall()
    call_updates: list[tuple] = []
    for row in call_rows:
        extra = _extra(row["extra"])
        owned = "scala_resolved" in extra or "scala_ambiguous" in extra
        current = row["target_qualified"]
        parser_direct = not owned and current in index.all_qns
        form = extra.get("scala_form")
        if parser_direct and form != "member":
            continue
        ctx_key = (row["file_path"], row["line"], row["source_qualified"])
        ctx = contexts.get(ctx_key)
        if ctx is None:
            ctx = index.context_for(*ctx_key)
            contexts[ctx_key] = ctx
        outcome = _resolve_call(index, extra, ctx)
        raw_name = extra.get("scala_name", current)
        desired_extra = {k: v for k, v in extra.items() if k not in _RESOLVED_KEYS}
        if "scala_ambiguous" in extra or outcome is not None:
            for key in _COLLISION_KEYS:
                desired_extra.pop(key, None)
        if outcome is not None and not outcome.ambiguous and len(outcome.targets) == 1:
            target = outcome.targets[0]
            if parser_direct and target == current:
                continue
            desired_extra["scala_resolved"] = outcome.rule
            if outcome.via:
                desired_extra["scala_via"] = outcome.via
            stats["calls_resolved"] += 1
        elif outcome is not None and outcome.ambiguous and not parser_direct:
            target = raw_name
            desired_extra["scala_ambiguous"] = outcome.rule
            desired_extra["ambiguous_targets"] = outcome.targets[:_MAX_CANDIDATES]
            desired_extra["ambiguous_target_count"] = len(outcome.targets)
            desired_extra["ambiguous_targets_truncated"] = len(outcome.targets) > _MAX_CANDIDATES
            stats["calls_ambiguous"] += 1
        else:
            if parser_direct:
                continue
            target = raw_name if owned else current
            if owned:
                stats["calls_restored"] += 1
        if target == current and desired_extra == extra:
            continue
        call_updates.append((
            target, json.dumps(desired_extra, sort_keys=True), row["id"],
        ))
        if target != current:
            changed_calls = True
            # A mirror of a call this pass leaves raw keeps what the parser
            # wrote, so an update and a rebuild store the same row.
            mirror_extra = desired_extra if "scala_resolved" in desired_extra else {
                k: v for k, v in extra.items()
                if k not in _RESOLVED_KEYS and k not in _COLLISION_KEYS
            }
            mirrors.append((
                target, json.dumps(mirror_extra, sort_keys=True), current,
                row["source_qualified"], row["file_path"], row["line"], row["extra"],
            ))

    if updates or call_updates:
        store._begin_immediate()
        try:
            conn.executemany(
                "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?", updates,
            )
            conn.executemany(
                "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
                call_updates,
            )
            conn.executemany(
                "UPDATE edges SET source_qualified = ?, extra = ? WHERE kind = 'TESTED_BY' "
                "AND source_qualified = ? AND target_qualified = ? AND file_path = ? "
                "AND line = ? AND extra = ?",
                mirrors,
            )
            if changed_calls:
                conn.execute(
                    "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, '1')",
                    (_FLOWS_DIRTY_KEY,),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        store._invalidate_cache()
    logger.log(
        logging.INFO if updates or call_updates else logging.DEBUG,
        "Scala resolution: %s", stats,
    )
    return stats
