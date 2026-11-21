"""Post-build Spring DI call resolver.

After tree-sitter parsing, Java CALLS edges whose target is a bare method
name (e.g. ``calculate``) carry ``extra.receiver`` naming the local variable
that was called on (e.g. ``invoiceCalculationService``).  This module
resolves those receivers through the INJECTS map to their declared type, then
optionally to the unique concrete implementation via INHERITS edges.

Resolution chain:
    receiver variable name
        → injected interface/class (from INJECTS.extra.field_name)
        → concrete implementation (from INHERITS, when unique)

When an interface has several implementations, one is chosen from the injection
point's ``@Qualifier``, then a single ``@Primary`` class, then the injection
point's name matching a bean name (see ``_select_implementation``).

Only Java files are processed.  Edges that are already qualified (contain
``::``) or have no ``receiver`` extra key are skipped.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

#: One injection point: target type, declared Java type, ``@Qualifier`` value,
#: and whether a ``@Qualifier`` was present with a value that is not a literal.
_Injection = tuple[str, Optional[str], Optional[str], bool]


def _default_bean_name(local_name: str) -> str:
    """Spring's default bean name: ``Introspector.decapitalize`` of the short name."""
    if len(local_name) > 1 and local_name[0].isupper() and local_name[1].isupper():
        return local_name
    return local_name[:1].lower() + local_name[1:]


def _bean_identity(local_name: str, node_extra: dict) -> dict:
    """Bean name, qualifier values and ``@Primary`` flag of a class node."""
    annotations = node_extra.get("spring_annotations") or []
    bean_name: Optional[str] = None
    if node_extra.get("spring_stereotype") and not node_extra.get("spring_bean_name_dynamic"):
        explicit = node_extra.get("spring_bean_name")
        bean_name = explicit if isinstance(explicit, str) and explicit else _default_bean_name(
            local_name
        )
    return {
        "bean_name": bean_name,
        "qualifiers": frozenset(node_extra.get("spring_qualifiers") or ()),
        "primary": "Primary" in annotations,
        "conditional": any(
            isinstance(a, str) and (a.startswith("Conditional") or a == "Profile")
            for a in annotations
        ),
    }


_NO_BEAN: dict = {
    "bean_name": None, "qualifiers": frozenset(), "primary": False, "conditional": False,
}


def _identity(beans: dict[str, dict], impl: str) -> dict:
    """Bean identity of a class; classes without Spring metadata match nothing."""
    return beans.get(impl, _NO_BEAN)


def _select_implementation(
    impls: list[str],
    beans: dict[str, dict],
    qualifier: Optional[str],
    qualifier_dynamic: bool,
    point_name: str,
) -> Optional[tuple[str, dict]]:
    """Pick the one implementation Spring would inject, or ``None`` when unsure.

    Rules, stopping at the first that leaves exactly one candidate:

    1. ``@Qualifier`` on the injection point equals a candidate's bean name or
       a ``@Qualifier`` on the candidate class.  A qualifier that matches zero
       or several candidates ends the search: Spring would not fall back.
    2. Without a qualifier, exactly one candidate is ``@Primary``.  Rules 2 and 3
       are skipped when any candidate is conditional (``@Profile``,
       ``@ConditionalOn*``), because the bean that exists then depends on
       runtime configuration.
    3. Without a qualifier, the injection point's name equals exactly one
       candidate's bean name.

    Returns the chosen class and the ``extra`` entries describing the rule.
    """
    if qualifier_dynamic:
        return None
    if qualifier is not None:
        matched = [
            impl for impl in impls
            if qualifier in _identity(beans, impl)["qualifiers"]
            or _identity(beans, impl)["bean_name"] == qualifier
        ]
        if len(matched) == 1:
            return matched[0], {"resolution": "spring_qualifier", "qualifier": qualifier}
        return None
    if any(_identity(beans, impl)["conditional"] for impl in impls):
        # Which beans exist depends on runtime configuration (@Profile,
        # @ConditionalOn*), so @Primary and name matching prove nothing.
        return None
    primary = [impl for impl in impls if _identity(beans, impl)["primary"]]
    if len(primary) == 1:
        return primary[0], {"resolution": "spring_primary"}
    named = [impl for impl in impls if _identity(beans, impl)["bean_name"] == point_name]
    if len(named) == 1:
        return named[0], {"resolution": "spring_bean_name", "bean_name": point_name}
    return None


def resolve_spring_di_calls(store: GraphStore) -> dict:
    """Resolve Java CALLS edges whose receiver is a Spring-injected field.

    Safe to call multiple times — already-resolved edges (targets containing
    ``::``) are skipped.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    # Only process Java files
    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    if not java_files:
        return {"files_indexed": 0, "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build field_map: (source_qualified_class, field_name) → injected_type
    # from INJECTS edges that carry extra.field_name
    # -----------------------------------------------------------------------
    field_map: dict[tuple[str, str], _Injection] = {}
    injects_rows = conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'INJECTS'"
    ).fetchall()
    for row in injects_rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        fname = extra.get("field_name")
        if not fname:
            continue
        # source_qualified is the full class qualified name
        class_qual = row["source_qualified"]
        java_type = extra.get("java_type")
        qualifier = extra.get("qualifier")
        field_map[(class_qual, fname)] = (
            row["target_qualified"],
            java_type if isinstance(java_type, str) else None,
            qualifier if isinstance(qualifier, str) and qualifier else None,
            bool(extra.get("qualifier_dynamic")),
        )

    if not field_map:
        logger.info("Spring resolver: no INJECTS edges with field_name found, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build package-qualified Java type indexes from class nodes.  Bare names
    # remain available only for old graphs that predate the Java metadata.
    # -----------------------------------------------------------------------
    java_kinds: dict[str, set[str]] = {}
    class_quals_by_type: dict[str, set[str]] = {}
    spring_beans: dict[str, dict] = {}
    types_by_simple_name: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, extra FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        try:
            node_extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            node_extra = {}
        package = node_extra.get("java_package")
        # The enclosing types belong to the identity: app.Outer.Sink is not app.Sink.
        local_name = qual.split("::", 1)[-1]
        java_type = f"{package}.{local_name}" if isinstance(package, str) and package else bare
        class_quals_by_type.setdefault(java_type, set()).add(qual)
        types_by_simple_name.setdefault(bare, set()).add(java_type)
        spring_beans[qual] = _bean_identity(local_name, node_extra)
        java_kind = node_extra.get("java_kind")
        if isinstance(java_kind, str):
            java_kinds.setdefault(java_type, set()).add(java_kind)
            # Older graphs do not have java_package.  Keep a bare-name index
            # for their compatibility fallback, but never use it to override
            # an unambiguous package-qualified identity.
            if java_type != bare:
                java_kinds.setdefault(bare, set()).add(java_kind)

    # Also index Function nodes so selected implementations resolve to their
    # exact file/class method, without homonym collisions.
    method_to_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name, file_path FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' AND parent_name IS NOT NULL"
    ).fetchall():
        class_qual = f"{row['file_path']}::{row['parent_name']}"
        method_to_qual[(class_qual, row["name"])] = row["qualified_name"]
        bare_key = (row["parent_name"].split(".")[-1], row["name"])
        previous = method_to_qual.get(bare_key)
        if previous is None or len(row["qualified_name"]) < len(previous):
            method_to_qual[bare_key] = row["qualified_name"]

    node_is_test = {
        row["qualified_name"]: bool(row["is_test"])
        for row in conn.execute("SELECT qualified_name, is_test FROM nodes").fetchall()
    }

    # -----------------------------------------------------------------------
    # Build implementors: package-qualified Java type → implementing class quals
    # from INHERITS edges (Java uses INHERITS for both extends and implements).
    # JVM edges without Java metadata stay keyed by their historical bare name.
    # -----------------------------------------------------------------------
    implementors: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT e.source_qualified, e.target_qualified, e.extra FROM edges e "
        "JOIN nodes n ON n.qualified_name = e.source_qualified "
        "WHERE e.kind = 'INHERITS' AND n.language IN ('java', 'kotlin', 'scala')"
    ).fetchall():
        try:
            edge_extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            edge_extra = {}
        java_type = edge_extra.get("java_type")
        iface = java_type if isinstance(java_type, str) else row["target_qualified"]
        impl = row["source_qualified"]
        implementors.setdefault(iface, set()).add(impl)

    # -----------------------------------------------------------------------
    # Resolve CALLS edges
    # -----------------------------------------------------------------------
    java_file_list = sorted(java_files)
    calls_rows = []
    for start in range(0, len(java_file_list), 450):
        batch = java_file_list[start : start + 450]
        placeholders = ",".join("?" for _ in batch)
        calls_rows.extend(
            conn.execute(
                "SELECT id, source_qualified, target_qualified, extra, file_path "
                f"FROM edges WHERE kind = 'CALLS' AND file_path IN ({placeholders})",
                batch,
            ).fetchall()
        )

    resolved = 0

    for row in calls_rows:
        if row["file_path"] not in java_files:
            continue

        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}

        receiver = extra.get("receiver")
        if not receiver:
            continue

        # Skip edges already spring-resolved in a previous pass
        if extra.get("spring_resolved"):
            continue

        # Strip any prior (possibly wrong) qualification — we have a receiver so
        # we can do a better resolution.  E.g. "file::ClassName.method" → "method"
        raw_target = row["target_qualified"]
        if "::" in raw_target:
            after = raw_target.split("::", 1)[1]
            method_name = after.split(".")[-1] if "." in after else after
        else:
            method_name = raw_target
        source_qual = row["source_qualified"]

        # Derive the enclosing class qualified name from source
        # source_qual format: "file_path::ClassName.method_name"
        enclosing_class_qual: str | None = None
        if "::" in source_qual:
            after_sep = source_qual.split("::", 1)[1]
            if "." in after_sep:
                class_part = after_sep.split(".")[0]
                prefix = source_qual.split("::")[0]
                enclosing_class_qual = f"{prefix}::{class_part}"
            else:
                enclosing_class_qual = source_qual

        if not enclosing_class_qual:
            continue

        # Look up receiver in field_map for this class
        injection = field_map.get((enclosing_class_qual, receiver))
        if not injection:
            continue
        injected_type, java_type, qualifier, qualifier_dynamic = injection
        type_key = java_type or injected_type
        # A bare identity (wildcard import, older graph) names a repository type
        # only when exactly one type carries that simple name.
        same_name = types_by_simple_name.get(type_key, ())
        if len(same_name) == 1:
            type_key = next(iter(same_name))

        source_is_test = node_is_test.get(source_qual, False)
        kind_values = java_kinds.get(type_key)
        if not kind_values and type_key != injected_type:
            # A graph written before qualified Java metadata existed can still
            # resolve through its historical bare-name indexes.
            kind_values = java_kinds.get(injected_type)
        java_kind = next(iter(kind_values)) if kind_values and len(kind_values) == 1 else None
        # Kotlin/Scala edges and Java files with wildcard imports stay under the
        # bare name, so they count alongside the qualified candidates: otherwise
        # one Java implementor beside a Kotlin one would read as the only one.
        impl_values = implementors.get(type_key, set()) | implementors.get(injected_type, set())
        impls = (
            sorted(impl_values or [])
            if java_kind is None or java_kind in {"interface", "abstract"}
            else []
        )
        if not source_is_test:
            impls = [
                impl
                for impl in impls
                if not node_is_test.get(impl, False)
            ]
        spring_extra: dict = {}
        new_target: Optional[str] = None
        if len(impls) == 1:
            fallback = f"{impls[0]}.{method_name}"
            new_target = method_to_qual.get((impls[0], method_name)) or fallback
        elif len(impls) > 1:
            # Bind only to a method node that exists, so the relink pass keeps the edge.
            chosen = _select_implementation(
                impls, spring_beans, qualifier, qualifier_dynamic, receiver,
            )
            if chosen is not None:
                chosen_target = method_to_qual.get((chosen[0], method_name))
                if chosen_target:
                    new_target, spring_extra = chosen_target, chosen[1]
        if new_target is None:
            type_bare = injected_type.rsplit(".", 1)[-1]
            declared_classes = class_quals_by_type.get(type_key, set())
            if len(declared_classes) == 1:
                declared_class = next(iter(declared_classes))
                new_target = method_to_qual.get(
                    (declared_class, method_name),
                ) or f"{declared_class}.{method_name}"
            else:
                fallback = f"{injected_type}.{method_name}"
                new_target = method_to_qual.get((type_bare, method_name)) or fallback

        extra["spring_resolved"] = True
        extra.update(spring_extra)
        extra["injected_type"] = injected_type
        if java_type:
            extra["java_type"] = java_type
        new_extra = json.dumps(extra)

        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            (new_target, new_extra, row["id"]),
        )
        resolved += 1
        logger.debug(
            "Spring resolved: %s → %s (was %s, receiver=%s)",
            source_qual, new_target, method_name, receiver,
        )

    if resolved:
        store.commit()

    logger.info("Spring DI resolver: resolved %d CALLS edges in %d Java files",
                resolved, len(java_files))
    return {"files_indexed": len(java_files), "calls_resolved": resolved}
