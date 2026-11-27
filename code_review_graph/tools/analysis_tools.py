"""MCP tool wrappers for graph analysis features."""

from __future__ import annotations

from typing import Any

from ..analysis import (
    HUB_RANK_BY,
    find_bridge_nodes,
    find_hub_nodes,
    find_knowledge_gaps,
    find_surprising_connections,
    generate_suggested_questions,
)
from ..analysis_snapshot import RESULTS, file_state, in_memory_snapshot
from ..paging import graph_stamp, make_scope, paginate, shown_range
from ._common import (
    _get_store,
    _page_together,
    _relative_to_root,
    _validate_positive_int,
)

# The ranking helpers already score every candidate before slicing, so asking
# for "all" costs nothing extra and lets the tool report an honest ``total``.
_FETCH_ALL = 10**9

# Hard ceilings. A caller may raise ``top_n`` above the default, but never
# past these: an unbounded top_n returned >500k tokens before #849's sweep.
_MAX_HUB_NODES = 100
_MAX_BRIDGE_NODES = 100
_MAX_SURPRISING = 100
_MAX_GAPS_PER_CATEGORY = 50

_MINIMAL_HUB_FIELDS = (
    "name", "kind", "total_degree", "ranking_degree", "resolved_degree", "resolved_share",
    "data_type",
)
# Two rows can share a bare name, so a minimal row also says where it is.
_MINIMAL_BRIDGE_FIELDS = ("name", "qualified_name", "file", "kind", "betweenness")
_MINIMAL_SURPRISE_FIELDS = (
    "source", "source_qualified", "source_file",
    "target", "target_qualified", "target_file",
    "edge_kind", "surprise_score", "group_count",
)
# Fields holding a path, or a qualified name that starts with one.
_PATH_FIELDS = (
    "file", "qualified_name", "source_qualified", "source_file",
    "target_qualified", "target_file",
)
_MINIMAL_GAP_FIELDS = (
    "name", "qualified_name", "community_id", "size", "degree", "load_time",
)


def _generated_note(include_generated: bool, stats: dict[str, int]) -> str:
    """Summary suffix naming what generated-code filtering removed."""
    excluded = stats.get("generated_excluded", 0)
    if include_generated or not excluded:
        return ""
    return f"; {excluded} generated node(s) excluded (include_generated=true keeps them)"


def _data_types_note(exclude_data_types: bool, stats: dict[str, int]) -> str:
    """Summary suffix naming what data-type filtering removed."""
    excluded = stats.get("data_types_excluded", 0)
    if not exclude_data_types or not excluded:
        return ""
    return f"; {excluded} data type node(s) excluded (exclude_data_types=false keeps them)"


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


def _minimal(
    rows: list[dict[str, Any]], fields: tuple[str, ...], root: Any,
) -> list[dict[str, Any]]:
    """Minimal rows, with paths relative to the repository root."""
    return [
        {
            k: _relative_to_root(v, root) if k in _PATH_FIELDS and isinstance(v, str) else v
            for k, v in row.items()
        }
        for row in _project(rows, fields)
    ]


def get_hub_nodes_func(
    repo_root: str | None = None,
    top_n: int = 10,
    detail_level: str = "standard",
    cursor: str | None = None,
    include_tests: bool = False,
    exclude_leaf_primitives: bool = False,
    include_config: bool = False,
    include_generated: bool = False,
    exclude_data_types: bool = False,
    rank_by: str = "resolved",
) -> dict[str, Any]:
    """Find the most connected nodes in the codebase graph.

    Hub nodes have the highest total degree (in + out edges).
    These are architectural hotspots -- changes to them have
    disproportionate blast radius.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top hubs to return (default 10, capped at 100).
        detail_level: "standard" (default) returns full node data;
            "minimal" returns only name, kind, and total_degree.
        cursor: ``next_cursor`` from the previous reply, to read the next
            page of the ranking. Omit it for the first page. A cursor is
            opaque and only valid for the same graph.
        include_tests: Include test nodes (default False). Tests are left out
            before ranking, so ``total`` and ``next_cursor`` describe the
            filtered list. A cursor is only valid for the value it was made with.
        exclude_leaf_primitives: Skip nodes with no outgoing CALLS or REFERENCES
            to an indexed non-test node (a design-system component that many
            files use resolves correctly and tells a reviewer little). Default
            False.
        include_config: Include JSON, YAML, output and property document nodes
            (default False). Left out before ranking, like tests. A cursor is
            only valid for the value it was made with.
        include_generated: Include generated code such as jOOQ or protobuf output
            (default False). Generated files are left out before ranking, like tests,
            so ``total`` and ``next_cursor`` describe the filtered list and
            ``generated_excluded`` reports how many nodes that removed. A cursor is
            only valid for the value it was made with.
        exclude_data_types: Skip data carriers: enums, Java classes that are Lombok
            data holders or only accessors, Kotlin/C# data classes and records, Python
            dataclasses, NamedTuple, TypedDict and pydantic models without non-dunder
            methods, TS interfaces and type aliases without methods. Left out before
            ranking, like tests, so ``total`` and ``next_cursor`` describe the filtered
            list and ``data_types_excluded`` reports how many that removed. Rows carry
            ``data_type`` (the label, or null) either way. A cursor is only valid for the
            value it was made with. Needs a graph built after this filter existed for
            enums, Kotlin data classes and TS interfaces.
        rank_by: "resolved" (default) orders hubs by ``resolved_degree``: edges bound to an
            indexed node, leaving out bare-name matches and calls a resolver bound by name
            alone to a name many symbols share, so a generic ``insert`` or ``execute`` that only
            looks hot through name collisions drops. "total" keeps the older order by
            ``ranking_degree`` (name-matched edges damped). A cursor is only valid for the
            value it was made with.

    Returns:
        Ranked hubs. Each row carries ``resolved_degree``, ``unresolved_degree`` and
        ``resolved_share`` (resolved over resolved plus unresolved). Rows are ordered by
        ``resolved_degree`` (or ``ranking_degree`` with ``rank_by="total"``), so
        ``total_degree`` (the raw count) is not strictly descending. ``total`` reports
        the untruncated candidate count and ``truncated`` marks that ``top_n`` or the
        ceiling cut the list; then
        ``next_cursor`` is the ``cursor`` that returns the next page.
    """
    _validate_positive_int(top_n, "top_n")
    if rank_by not in HUB_RANK_BY:
        raise ValueError(f"rank_by must be one of {list(HUB_RANK_BY)}, got {rank_by!r}")

    store, _root = _get_store(repo_root or None)
    try:
        stats: dict[str, int] = {}
        ranked = find_hub_nodes(
            store, top_n=_FETCH_ALL, include_tests=include_tests,
            exclude_leaf_primitives=exclude_leaf_primitives,
            include_config=include_config, include_generated=include_generated, stats=stats,
            exclude_data_types=exclude_data_types, rank_by=rank_by,
        )
        # A repository of data files has no hub but its config nodes. Hiding them
        # would leave an empty ranking, so they are ranked instead and the reply says so.
        config_only = False
        if not include_config and not ranked:
            with_config = find_hub_nodes(
                store, top_n=_FETCH_ALL, include_tests=include_tests,
                exclude_leaf_primitives=exclude_leaf_primitives, include_config=True,
                include_generated=include_generated, stats=stats,
                exclude_data_types=exclude_data_types, rank_by=rank_by,
            )
            if with_config:
                ranked, config_only = with_config, True
        page = paginate(
            ranked, top_n, _MAX_HUB_NODES, cursor,
            scope=make_scope(
                "get_hub_nodes_tool", include_tests=include_tests,
                exclude_leaf_primitives=exclude_leaf_primitives,
                include_config=include_config or config_only,
                include_generated=include_generated,
                exclude_data_types=exclude_data_types, rank_by=rank_by,
            ),
            stamp=graph_stamp(store),
        )
        hubs, total = page.items, page.total
        if detail_level == "minimal":
            hubs = _project(hubs, _MINIMAL_HUB_FIELDS)
        result: dict[str, Any] = {
            "status": "ok",
            "summary": (
                f"{total} hub node(s) ranked by "
                + (
                    "resolved_degree (bare-name and name-matched edges left out; "
                    "resolved_share is the traced fraction of each hub's edges, so total_degree "
                    "is not strictly descending)"
                    if rank_by == "resolved"
                    else "ranking_degree (name-matched edges damped, "
                    "so total_degree is not strictly descending)"
                )
                + shown_range(page)
                + _generated_note(include_generated, stats)
                + _data_types_note(exclude_data_types, stats)
            ),
            "generated_excluded": stats.get("generated_excluded", 0),
            "data_types_excluded": stats.get("data_types_excluded", 0),
            "hub_nodes": hubs,
            "count": len(hubs),
            "total": total,
            "truncated": page.truncated,
            **page.fields(),
            "next_tool_suggestions": [
                "get_impact_radius_tool -- check blast radius of a hub",
                "query_graph_tool callers_of -- see what calls a hub",
                "get_bridge_nodes_tool -- find architectural chokepoints",
            ],
        }
        if config_only:
            result["note"] = (
                "This repository has no hub but config document nodes, so they are "
                "ranked. Pass include_config=true to ask for them explicitly."
            )
        return result
    finally:
        store.close()


def get_bridge_nodes_func(
    repo_root: str | None = None,
    top_n: int = 10,
    detail_level: str = "standard",
    cursor: str | None = None,
    include_tests: bool = False,
    include_config: bool = False,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Find architectural chokepoints via betweenness centrality.

    Bridge nodes sit on the shortest paths between many node
    pairs. If they break, multiple code regions lose
    connectivity.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top bridges to return (default 10, capped at 100).
        detail_level: "standard" (default) returns full node data;
            "minimal" returns only name, qualified_name, file (both
            relative to the repository root), kind, and betweenness.
        cursor: ``next_cursor`` from the previous reply, to read the next
            page of the ranking. Omit it for the first page. A cursor is
            opaque and only valid for the same graph.
        include_tests: Include test nodes (default False). Tests are left out
            before ranking, so ``total`` and ``next_cursor`` describe the
            filtered list. A cursor is only valid for the value it was made with.
        include_config: Include JSON, YAML, output and property document nodes
            (default False). Left out before ranking, like tests. A cursor is
            only valid for the value it was made with.
        include_generated: Include generated code such as jOOQ or protobuf output
            (default False). Generated files are left out before ranking, like tests,
            so ``total`` and ``next_cursor`` describe the filtered list and
            ``generated_excluded`` reports how many nodes that removed. A cursor is
            only valid for the value it was made with.

    Returns:
        Ranked bridges. ``total`` reports the untruncated candidate count and
        ``truncated`` marks that ``top_n`` or the ceiling cut the list; then
        ``next_cursor`` is the ``cursor`` that returns the next page.
    """
    _validate_positive_int(top_n, "top_n")

    store, root = _get_store(repo_root or None)
    try:
        stats: dict[str, int] = {}
        ranked = find_bridge_nodes(
            store, top_n=_FETCH_ALL, include_tests=include_tests,
            include_config=include_config, include_generated=include_generated, stats=stats,
        )
        # A repository of data files has no bridge but config nodes; rank those
        # instead of returning nothing, and say so.
        config_only = False
        if not include_config and not ranked:
            with_config = find_bridge_nodes(
                store, top_n=_FETCH_ALL, include_tests=include_tests, include_config=True,
                include_generated=include_generated, stats=stats,
            )
            if with_config:
                ranked, config_only = with_config, True
        page = paginate(
            ranked, top_n, _MAX_BRIDGE_NODES, cursor,
            scope=make_scope(
                "get_bridge_nodes_tool", include_tests=include_tests,
                include_config=include_config or config_only,
                include_generated=include_generated,
            ),
            stamp=graph_stamp(store),
        )
        bridges, total = page.items, page.total
        if detail_level == "minimal":
            bridges = _minimal(bridges, _MINIMAL_BRIDGE_FIELDS, root)
        result: dict[str, Any] = {
            "status": "ok",
            "summary": (
                f"{total} bridge node(s) ranked by betweenness"
                + shown_range(page)
                + _generated_note(include_generated, stats)
            ),
            "generated_excluded": stats.get("generated_excluded", 0),
            "bridge_nodes": bridges,
            "count": len(bridges),
            "total": total,
            "truncated": page.truncated,
            **page.fields(),
            "next_tool_suggestions": [
                "get_hub_nodes_tool -- find most connected nodes",
                "get_impact_radius_tool -- check blast radius",
                "detect_changes_tool -- see if bridges are affected",
            ],
        }
        if config_only:
            result["note"] = (
                "This repository has no bridge but config document nodes, so they are "
                "ranked. Pass include_config=true to ask for them explicitly."
            )
        return result
    finally:
        store.close()


def get_knowledge_gaps_func(
    repo_root: str | None = None,
    max_per_category: int = 15,
    detail_level: str = "standard",
    cursor: str | None = None,
    include_tests: bool = False,
    include_non_code: bool = False,
    include_generated: bool = False,
    exclude_data_types: bool = False,
) -> dict[str, Any]:
    """Identify structural weaknesses in the codebase.

    Finds: isolated nodes (disconnected), thin communities
    (< 3 members), untested hotspots (high-degree, no tests),
    single-file communities, and import cycles (files that
    import each other).

    Args:
        repo_root: Repository root (auto-detected if omitted).
        max_per_category: Maximum entries per gap category (default 15,
            capped at 50). ``summary`` and ``total_gaps`` always report the
            untruncated counts.
        detail_level: "standard" (default) returns full gap records;
            "minimal" drops file paths and keeps identifying fields only.
        cursor: ``next_cursor`` from the previous reply, to read the next
            page. Omit it for the first page. A cursor is opaque and only
            valid for the same graph. Every category advances together: a
            page holds the same row positions of each list.
        include_tests: Include test nodes (default False). Tests are left out
            of every category before totals and paging, so ``summary``,
            ``total_gaps`` and ``next_cursor`` describe the filtered lists.
            Configuration-document nodes (JsonPath, YamlPath, Output,
            ConfigProperty) are never reported as isolated nodes.
        include_non_code: Also report documentation sections, lockfile entries,
            Dockerfile stages and anything under ``.storybook`` or ``.github``
            as isolated nodes and untested hotspots, and also config document
            keys and Step Functions (ASL) states as untested hotspots and the
            dunder methods of a class (``__init__``, ``__repr__``) as isolated
            nodes (default False).
        include_generated: Include generated code such as jOOQ or protobuf output
            (default False). Generated files are left out before ranking, like tests,
            so ``summary`` and ``next_cursor`` describe the filtered list and
            ``generated_excluded`` reports the isolated nodes and hotspot candidates that
            removed (candidates are counted before their test coverage is looked up).
            A cursor is only valid for the value it was made with.
        exclude_data_types: Leave data carriers (enums, records, Lombok or
            accessor-only Java classes, dataclasses, TS interfaces and type aliases
            with no methods) out of ``untested_hotspots`` before ranking, as
            ``get_hub_nodes_tool`` does; ``data_types_excluded`` counts them. Default
            False. A cursor is only valid for the value it was made with.

    Returns:
        Gaps by category. ``summary`` maps each category to its untruncated
        count and ``truncated`` marks that at least one list was cut; then
        ``next_cursor`` is the ``cursor`` that returns the next rows of every
        category, and a category with no rows left is empty.
    """
    _validate_positive_int(max_per_category, "max_per_category")

    store, root = _get_store(repo_root or None)
    try:
        flags = (include_tests, include_non_code, include_generated, exclude_data_types)
        state = file_state(store.db_path)

        def compute() -> tuple[dict[str, list[dict]], dict[str, int], str]:
            # The analysis reads every edge and node. On the file that is hundreds of
            # thousands of page reads, so run it on an in-memory copy (same SQL, same rows).
            with in_memory_snapshot(store):
                found_stats: dict[str, int] = {}
                found = find_knowledge_gaps(
                    store, repo_root=root, max_isolated=None, max_untested=None,
                    include_tests=include_tests, include_non_code=include_non_code,
                    include_generated=include_generated, stats=found_stats,
                    exclude_data_types=exclude_data_types,
                )
                return found, found_stats, graph_stamp(store)

        # Every cursor page asks for the same untruncated lists, so keep them per graph state.
        key = None if state is None else ("knowledge_gaps", str(store.db_path), state, *flags)
        (raw, stats, stamp), _ = RESULTS.get_or_compute(key, compute)
        # Totals must come from the untruncated lists: the summary counts are
        # the whole point of the tool, and capping them would hide the gap.
        totals = {category: len(rows) for category, rows in raw.items()}

        gaps, page = _page_together(
            raw, max_per_category, _MAX_GAPS_PER_CATEGORY, cursor,
            scope=make_scope(
                "get_knowledge_gaps_tool", include_tests=include_tests,
                include_non_code=include_non_code, include_generated=include_generated,
                exclude_data_types=exclude_data_types,
            ),
            stamp=stamp,
        )
        if detail_level == "minimal":
            gaps = {c: _project(rows, _MINIMAL_GAP_FIELDS) for c, rows in gaps.items()}
        truncated = page.truncated

        total = sum(totals.values())
        return {
            "status": "ok",
            "summary": totals,
            "gaps": gaps,
            "total_gaps": total,
            "generated_excluded": {
                "isolated_nodes": stats.get("generated_isolated_excluded", 0),
                "untested_hotspot_candidates": stats.get(
                    "generated_hotspot_candidates_excluded", 0,
                ),
            },
            "data_types_excluded": stats.get("data_types_excluded", 0),
            "truncated": truncated,
            **page.fields(),
            "next_tool_suggestions": [
                "refactor_tool dead_code -- find unused symbols",
                "get_hub_nodes_tool -- find high-impact nodes",
                "get_suggested_questions_tool -- review prompts",
            ],
        }
    finally:
        store.close()


def get_surprising_connections_func(
    repo_root: str | None = None,
    top_n: int = 15,
    detail_level: str = "standard",
    cursor: str | None = None,
    include_tests: bool = False,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Find unexpected architectural coupling in the codebase.

    Scores edges by surprise factors: cross-community,
    cross-language, peripheral-to-hub, cross-test-boundary.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top surprises to return (default 15, capped at 100).
        detail_level: "standard" (default) returns full edge records;
            "minimal" returns only source and target (each with its
            qualified name and file, relative to the repository root),
            edge_kind, and score.
        cursor: ``next_cursor`` from the previous reply, to read the next
            page of the ranking. Omit it for the first page. A cursor is
            opaque and only valid for the same graph.
        include_tests: Include test nodes (default False). Tests are left out
            before ranking, so ``total`` and ``next_cursor`` describe the
            filtered list. A cursor is only valid for the value it was made with.
        include_generated: Include generated code such as jOOQ or protobuf output
            (default False). Generated files are left out before ranking, like tests,
            so ``total`` and ``next_cursor`` describe the filtered list and
            ``generated_excluded`` reports how many edges that removed. A cursor is
            only valid for the value it was made with.

    Returns:
        Ranked surprising edges. Equal scores are ordered by the combined degree of the two
        endpoints, then by name. ``group_count`` is the size of the source -> target-class
        family a row belongs to (no row is dropped for it). ``total`` reports the untruncated
        count and ``truncated`` marks that ``top_n`` or the ceiling cut the list; then
        ``next_cursor`` is the ``cursor`` that returns the next page.
    """
    _validate_positive_int(top_n, "top_n")

    store, root = _get_store(repo_root or None)
    try:
        stats: dict[str, int] = {}
        page = paginate(
            find_surprising_connections(
                store, top_n=_FETCH_ALL, include_tests=include_tests,
                include_generated=include_generated, stats=stats,
            ),
            top_n, _MAX_SURPRISING, cursor,
            scope=make_scope(
                "get_surprising_connections_tool", include_tests=include_tests,
                include_generated=include_generated,
            ),
            stamp=graph_stamp(store),
        )
        surprises, total = page.items, page.total
        if detail_level == "minimal":
            surprises = _minimal(surprises, _MINIMAL_SURPRISE_FIELDS, root)
        return {
            "status": "ok",
            "summary": (
                f"{total} surprising connection(s) ranked by surprise score, ties by "
                "combined endpoint degree"
                + shown_range(page)
                + _generated_note(include_generated, stats)
            ),
            "generated_excluded": stats.get("generated_excluded", 0),
            "surprising_connections": surprises,
            "count": len(surprises),
            "total": total,
            "truncated": page.truncated,
            **page.fields(),
            "next_tool_suggestions": [
                "get_architecture_overview_tool -- community structure",
                "query_graph_tool callers_of -- trace the coupling",
                "get_bridge_nodes_tool -- find chokepoints",
            ],
        }
    finally:
        store.close()


def get_suggested_questions_func(
    repo_root: str | None = None,
    include_tests: bool = False,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Auto-generate review questions from graph analysis.

    Produces questions about: bridge nodes, untested hubs,
    surprising connections, thin communities, and untested
    hotspots.

    The output is bounded by construction: ``generate_suggested_questions``
    draws at most 3 bridges, 3 hubs, 3 surprises, 2 thin communities and
    2 untested hotspots, so no result cap is needed here.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        include_tests: Include test nodes (default False), so the questions
            are about production code unless this is set.
        include_generated: Include generated code such as jOOQ output (default False).
            ``generated_excluded`` reports how many nodes and edges that removed.
    """
    store, _root = _get_store(repo_root or None)
    try:
        stats: dict[str, int] = {}
        questions = generate_suggested_questions(
            store, include_tests=include_tests,
            include_generated=include_generated, stats=stats,
        )
        by_priority: dict[str, list[dict[str, Any]]] = {
            "high": [], "medium": [], "low": [],
        }
        for q in questions:
            prio = q.get("priority", "medium")
            if prio in by_priority:
                by_priority[prio].append(q)
        return {
            "questions": questions,
            "count": len(questions),
            "generated_excluded": stats.get("generated_excluded", 0),
            "by_priority": {
                k: len(v) for k, v in by_priority.items()
            },
            "next_tool_suggestions": [
                "get_knowledge_gaps_tool -- structural weaknesses",
                "detect_changes_tool -- risk-scored review",
                "get_architecture_overview_tool -- community map",
            ],
        }
    finally:
        store.close()
