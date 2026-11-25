"""Post-build link from a call on a Java interface to its only implementation.

A call on an interface-typed receiver binds to the interface's method (that is the parse
side's job). When the repository has exactly one class implementing that interface, the
call also runs that class's method, so this pass adds a second ``CALLS`` edge from the same
caller to it, marked ``extra.via = "interface"`` (``extra.interface`` names the interface
method). Callers, flows and dead-code checks then see through the interface.

Nothing is guessed. No edge is added when:

* the interface has no implementor in the repository, or more than one (``@Primary`` and
  ``@Qualifier`` choices on injected fields are made by ``spring_resolver``, which binds the
  call itself and needs no second edge),
* the only implementor is abstract, or is another interface, or the class does not declare
  the method exactly once (inherited methods and overloads are left alone),
* the caller is a test.

The pass owns every edge it wrote: each run deletes them and rebuilds the set from the
current graph, so a removed or added implementation never leaves a stale link behind.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

#: ``extra.via`` of the edges this module writes.
VIA = "interface"

_BATCH = 450


def _loads(raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clear(store: GraphStore) -> int:
    cursor = store._conn.execute(
        "DELETE FROM edges WHERE kind = 'CALLS' AND json_valid(extra) "
        "AND json_extract(extra, '$.via') = ?",
        (VIA,),
    )
    return cursor.rowcount


def resolve_java_interface_calls(store: GraphStore) -> dict[str, int]:
    """Add a ``via: interface`` CALLS edge from each call on a single-implementor interface."""
    conn = store._conn
    removed = _clear(store)

    classes: dict[str, dict] = {}
    by_type: dict[str, set[str]] = {}
    by_bare: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, extra, is_test FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        extra = _loads(row["extra"])
        qual = row["qualified_name"]
        local = qual.split("::", 1)[-1]
        package = extra.get("java_package")
        java_type = f"{package}.{local}" if isinstance(package, str) and package else row["name"]
        classes[qual] = {"kind": extra.get("java_kind"), "test": bool(row["is_test"])}
        by_type.setdefault(java_type, set()).add(qual)
        by_bare.setdefault(row["name"], set()).add(qual)

    interfaces = {q for q, info in classes.items() if info["kind"] == "interface"}
    if not interfaces:
        if removed:
            store.commit()
        return {"interfaces": 0, "calls_added": 0}

    def interface_of(edge_target: str, extra: dict) -> str | None:
        java_type = extra.get("java_type")
        for key, index in ((java_type, by_type), (edge_target, by_type), (edge_target, by_bare)):
            found = index.get(key) if isinstance(key, str) else None
            if found and len(found) == 1:
                qual = next(iter(found))
                return qual if qual in interfaces else None
        return None

    implementors: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT e.source_qualified, e.target_qualified, e.extra FROM edges e "
        "JOIN nodes n ON n.qualified_name = e.source_qualified "
        "WHERE e.kind = 'INHERITS' AND n.kind = 'Class' "
        "AND n.language IN ('java', 'kotlin', 'scala')"
    ).fetchall():
        impl = row["source_qualified"]
        if classes.get(impl, {}).get("test"):
            continue
        iface = interface_of(row["target_qualified"], _loads(row["extra"]))
        if iface is not None:
            implementors.setdefault(iface, set()).add(impl)

    methods: dict[tuple[str, str], list[str]] = {}
    iface_methods: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name, file_path FROM nodes "
        "WHERE kind = 'Function' AND language = 'java' AND parent_name IS NOT NULL"
    ).fetchall():
        class_qual = f"{row['file_path']}::{row['parent_name']}"
        methods.setdefault((class_qual, row["name"]), []).append(row["qualified_name"])
        if class_qual in interfaces:
            iface_methods[row["qualified_name"]] = (class_qual, row["name"])

    # Interface -> its one concrete implementation, where there is exactly one.
    sole: dict[str, str] = {}
    for iface, impls in implementors.items():
        if len(impls) == 1:
            impl = next(iter(impls))
            # A Kotlin or Scala implementor is not in ``classes``: it is a second one.
            if classes.get(impl, {}).get("kind") == "concrete":
                sole[iface] = impl

    targets = sorted(qn for qn, (iface, _) in iface_methods.items() if iface in sole)
    added = 0
    for start in range(0, len(targets), _BATCH):
        batch = targets[start:start + _BATCH]
        rows = conn.execute(
            "SELECT e.source_qualified, e.target_qualified, e.file_path, e.line, e.extra, "
            "n.is_test FROM edges e JOIN nodes n ON n.qualified_name = e.source_qualified "
            "WHERE e.kind = 'CALLS' AND n.language = 'java' "
            f"AND e.target_qualified IN ({','.join('?' * len(batch))})",  # nosec B608
            batch,
        ).fetchall()
        for row in rows:
            if row["is_test"]:
                continue
            iface, name = iface_methods[row["target_qualified"]]
            impl_methods = methods.get((sole[iface], name), [])
            if len(impl_methods) != 1:
                continue
            impl_target = impl_methods[0]
            if conn.execute(
                "SELECT 1 FROM edges WHERE kind = 'CALLS' AND source_qualified = ? "
                "AND target_qualified = ? LIMIT 1",
                (row["source_qualified"], impl_target),
            ).fetchone():
                continue
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=row["source_qualified"],
                target=impl_target,
                file_path=row["file_path"],
                line=row["line"],
                extra={
                    "via": VIA,
                    "interface": row["target_qualified"],
                    "resolution": "java_single_implementation",
                    "confidence": 0.8,
                    "confidence_tier": "INFERRED",
                },
            ))
            added += 1

    if added or removed:
        store.commit()
    logger.log(
        logging.INFO if added or removed else logging.DEBUG,
        "Java interface resolver: %d CALLS edges added through single implementations",
        added,
    )
    return {"interfaces": len(interfaces), "calls_added": added}
