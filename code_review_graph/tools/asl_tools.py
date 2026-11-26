"""Step Functions tools: every transition of a state machine in one call."""

from __future__ import annotations

from typing import Any

from ..graph import GraphNode, GraphStore, _sanitize_name
from ..paging import graph_stamp, make_scope, paginate
from ._common import _get_store
from .query import _asl_states_of, _choice_condition

#: Rows in one reply, however large ``max_results`` is.
_MAX_TRANSITIONS = 500
_DEFAULT_TRANSITIONS = 200
_MAX_ERRORS_SHOWN = 5

#: What an edge's ``via`` is called in a state machine definition.
_KINDS = {
    "next": "Next", "choice": "Choice", "default": "Default", "catch": "Catch",
    "branch": "Branch", "map": "Map",
}
_MAX_CANDIDATES = 10


def _machines_for(store: GraphStore, root: Any, machine: str) -> list[GraphNode]:
    """The state machine(s) a name, qualified name or definition file points at."""
    from ..parser import normalize_file_path

    node = store.get_node(machine)
    if node is None and machine:
        path, sep, symbol = machine.partition("::")
        node = store.get_node(f"{normalize_file_path(root / path)}{sep}{symbol}")
    if node is not None:
        if node.kind == "File":
            found = [
                store.get_node(e.target_qualified)
                for e in store.iter_edges_by_source(node.qualified_name)
                if e.kind == "CONTAINS"
            ]
            return [n for n in found if n is not None and n.kind == "Class"]
        if node.kind == "Class" and node.language == "asl":
            return [node]
        return []
    named = store.search_nodes(machine, limit=50)
    return [
        n for n in named if n.kind == "Class" and n.language == "asl" and n.name == machine
    ]


def transitions_of_func(
    machine: str,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = _DEFAULT_TRANSITIONS,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Every transition of one Step Functions state machine, in one reply.

    ``machine`` is a machine name, its qualified name (``<file>::<machine>``) or its
    ``*.asl.json`` file. Rows follow the file order. Standard rows are
    ``{from, to, kind, condition?, line, errors?}``; minimal rows are the same as one string,
    ``"from -> to | kind | condition | Lline"``. ``kind`` is Next, Choice, Default, Catch,
    Branch or Map.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")
    store, root = _get_store(repo_root)
    try:
        machines = _machines_for(store, root, machine)
        if not machines:
            return {
                "status": "not_found",
                "summary": f"No Step Functions state machine matches '{_sanitize_name(machine)}'.",
            }
        if len(machines) > 1:
            return {
                "status": "ambiguous",
                "summary": (
                    f"'{_sanitize_name(machine)}' matches {len(machines)} state machines. "
                    "Re-run with one qualified_name."
                ),
                "candidates": [
                    {"qualified_name": m.qualified_name, "file_path": m.file_path}
                    for m in machines[:_MAX_CANDIDATES]
                ],
            }
        target = machines[0]
        states = _asl_states_of(store, target)
        prefix = f"{target.name}/"

        def label(qualified: str) -> str:
            tail = qualified.split("::", 1)[1] if "::" in qualified else qualified
            return tail[len(prefix):] if tail.startswith(prefix) else tail

        conditions_by_file: dict[str, Any] = {}
        unreadable = 0
        rows: list[dict[str, Any]] = []
        for state in states:
            edges = [
                e for e in store.get_edges_by_source(state.qualified_name)
                if e.kind == "TRANSITIONS_TO"
            ]
            edges.sort(key=lambda e: (e.line, e.extra.get("index", -1)))
            for edge in edges:
                row: dict[str, Any] = {
                    "from": label(edge.source_qualified),
                    "to": label(edge.target_qualified),
                    "kind": _KINDS.get(str(edge.extra.get("via")), str(edge.extra.get("via"))),
                }
                text, reason = _choice_condition(store, root, conditions_by_file, edge)
                if text:
                    row["condition"] = text
                elif reason:
                    unreadable += 1
                row["line"] = edge.line
                errors = edge.extra.get("error_equals")
                if errors:
                    row["errors"] = list(errors)[:_MAX_ERRORS_SHOWN]
                rows.append(row)

        minimal = detail_level == "minimal"
        scope = make_scope("transitions_of_tool", machine=target.qualified_name)
        page = paginate(
            rows, max_results, _MAX_TRANSITIONS, cursor,
            scope=scope, stamp=graph_stamp(store),
        )
        if minimal:
            shown: list[Any] = [
                " | ".join(
                    [f"{r['from']} -> {r['to']}", r["kind"]]
                    + ([r["condition"]] if "condition" in r else [])
                    + [f"L{r['line']}"]
                )
                for r in page.items
            ]
        else:
            shown = page.items
        response: dict[str, Any] = {
            "status": "ok",
            "machine": target.qualified_name,
            "summary": (
                f"{page.total} transition(s) between {len(states)} state(s) of "
                f"'{_sanitize_name(target.name)}'"
            ),
            "state_count": len(states),
            "transition_count": page.total,
            "transitions": shown,
            **page.fields(),
        }
        if unreadable:
            response["note"] = (
                f"{unreadable} Choice condition(s) not shown: the file changed since it was "
                "indexed, or cannot be read."
            )
        return response
    finally:
        store.close()
