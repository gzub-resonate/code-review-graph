"""Tools 10, 11: list_flows, get_flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..analysis_snapshot import cached_on_snapshot
from ..flows import ENTRY_KINDS, get_flow_by_id, get_flows, group_duplicate_flows
from ..graph_counts import graph_stamp
from ..hints import generate_hints, get_session
from ..incremental import followed_files, path_within_repo
from ..paging import make_scope, paginate, shown_range
from ._common import _get_store, _relative_to_root, _validate_positive_int

# ---------------------------------------------------------------------------
# Tool 10: list_flows  [EXPLORE]
# ---------------------------------------------------------------------------

# ``get_flows`` slices in SQL, so asking for "all" is how the tool counts the
# untruncated total before keeping a bounded prefix.
_FETCH_ALL = 10**9

# Hard ceilings. Flow counts scale with entry points, and ``get_flow``
# embeds one record per step (plus optional source), so both need bounds.
_MAX_FLOWS = 200
_MAX_FLOW_STEPS = 200
_MAX_FLOW_SOURCE_LINES = 2000


def list_flows(
    repo_root: str | None = None,
    sort_by: str = "criticality",
    limit: int = 50,
    kind: str | None = None,
    detail_level: str = "standard",
    cursor: str | None = None,
    entry_kind: str | None = None,
    group_duplicates: bool = True,
) -> dict[str, Any]:
    """List execution flows in the codebase, sorted by criticality.

    [EXPLORE] Retrieves stored execution flows from the knowledge graph.
    Each flow represents a call chain starting from an entry point
    (e.g. HTTP handler, CLI command, test function). A Java flow is named
    ``Class.method``; every flow has an ``entry_kind`` (``http``, ``scheduler``,
    ``queue_listener``, ``event``, ``main`` or ``other``) and, where they apply, ``route``,
    ``schedule`` (the ``@Scheduled`` timing as written) and ``queues``.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: criticality, depth, node_count, file_count,
                 or name.
        limit: Maximum flows to return (default: 50, capped at 200).
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        detail_level: "standard" (default) returns full flow data;
                      "minimal" returns only name, qualified_name and
                      file (the entry point's, relative to the repository
                      root), criticality, and node_count per flow.
        cursor: ``next_cursor`` from the previous reply, to read the next page
                of the same list. Omit it for the first page. A cursor is opaque
                and only valid for the same arguments on the same graph.
                Every flow has ``flow_key`` (its entry node's qualified name, relative to
                the repository) and ``file``; unlike ``id`` they survive a reindex, and
                ``get_flow(flow_key=...)`` finds the flow by them.
        entry_kind: Optional filter by how the flow starts: ``http``, ``scheduler``,
                ``queue_listener``, ``event``, ``main`` or ``other``.
        group_duplicates: When true (default), flows whose call tree below the entry is
                the same set of nodes (a public and an admin controller calling one
                service method) are one flow: the most critical stands for the group and
                lists every entry point under ``entry_points``. ``total`` counts groups. When
                merging removed flows, ``total_before_grouping`` and the summary give the
                count without it; ``group_duplicates=false`` lists one row per entry point.

    Returns:
        Flows with criticality scores, plus ``total`` and ``truncated``. When more
        flows remain, ``next_cursor`` is the ``cursor`` that returns the next page.
    """
    _validate_positive_int(limit, "limit")
    if entry_kind is not None and entry_kind not in ENTRY_KINDS:
        return {
            "status": "error",
            "error": f"entry_kind must be one of {', '.join(ENTRY_KINDS)}",
        }

    store, root = _get_store(repo_root)
    try:
        def compute() -> tuple[list[dict[str, Any]], int | None]:
            # Count every matching flow, then keep the bounded prefix: the same
            # "count all, return a prefix" contract query.py uses for max_results.
            found = get_flows(
                store, sort_by=sort_by, limit=_FETCH_ALL, with_entry_metadata=True,
            )
            if entry_kind:
                found = [f for f in found if f.get("entry_kind") == entry_kind]

            def _of_kind(items: list[dict]) -> list[dict]:
                if not kind:
                    return items
                filtered = []
                for f in items:
                    ep_id = f.get("entry_point_id")
                    if ep_id is not None:
                        node_kind = store.get_node_kind_by_id(ep_id)
                        if node_kind == kind:
                            filtered.append(f)
                return filtered

            before_grouping: int | None = None
            if group_duplicates:
                ungrouped = found
                found = group_duplicate_flows(found)
                if len(found) != len(ungrouped):
                    before_grouping = len(_of_kind(ungrouped))
            return _of_kind(found), before_grouping

        # Every flow with its path and entry metadata is read once per graph state, on an
        # in-memory copy, and every cursor page reads the same list.
        flows, raw_total = cached_on_snapshot(
            store, "list_flows", (sort_by, kind, entry_kind, group_duplicates), compute,
        )

        page = paginate(
            flows, limit, _MAX_FLOWS, cursor,
            scope=make_scope(
                "list_flows_tool", sort_by=sort_by, kind=kind, entry_kind=entry_kind,
                group_duplicates=group_duplicates,
            ),
            stamp=graph_stamp(store),
        )
        flows, total = page.items, page.total

        # Two flows can share an entry point name, so each row says where its
        # entry point is. Only the rows kept are looked up.
        for f in flows:
            entry = (
                store.get_node_by_id(f["entry_point_id"])
                if f.get("entry_point_id") is not None else None
            )
            if entry is not None:
                f["qualified_name"] = _relative_to_root(entry.qualified_name, root)
                f["file"] = _relative_to_root(entry.file_path, root)
                f["flow_key"] = f["qualified_name"]

        if detail_level == "minimal":
            flows = [
                {
                    k: f[k]
                    for k in (
                        "name", "qualified_name", "file", "flow_key", "criticality",
                        "node_count", "entry_kind", "group_size",
                    )
                    if k in f
                }
                for f in flows
            ]

        merged_note = ""
        if raw_total is not None and raw_total != total:
            merged_note = (
                f" ({raw_total} before merging duplicates; "
                "group_duplicates=false lists one per entry point)"
            )
        result: dict[str, object] = {
            "status": "ok",
            "summary": (
                f"Found {total} execution flow(s)"
                + merged_note
                + shown_range(page)
            ),
            "flows": flows,
            "total": total,
            "truncated": page.truncated,
            **page.fields(),
        }
        if raw_total is not None and raw_total != total:
            result["total_before_grouping"] = raw_total
        result["_hints"] = generate_hints(
            "list_flows_tool", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 11: get_flow  [EXPLORE]
# ---------------------------------------------------------------------------


def attach_flow_keys(store: Any, root: Path | None, flows: list[dict[str, Any]]) -> None:
    """Give each flow ``flow_key`` (its entry node's qualified name) and ``file`` (its entry file).

    A flow's ``id`` is a row number that changes when the flows are rebuilt, so a reindex turns
    flow #323 into flow #1. The entry node's qualified name does not change, and the ids of
    the nodes behind it are read at query time, so nothing is stored. Both values are relative
    to *root* like the other flow rows. A flow whose entry node is gone gets neither.
    """
    for flow in flows:
        entry_id = flow.get("entry_point_id")
        entry = store.get_node_by_id(entry_id) if entry_id is not None else None
        if entry is None:
            continue
        flow["flow_key"] = _relative_to_root(entry.qualified_name, root)
        flow.setdefault("file", _relative_to_root(entry.file_path, root))


def _flow_id_for_key(store: Any, root: Path, key: str) -> int | None:
    """The id of the flow whose entry node has this qualified name (relative or absolute)."""
    from ..parser import normalize_file_path

    names = {key, f"{normalize_file_path(root)}/{key}"}
    row = store._conn.execute(
        "SELECT f.id FROM flows f JOIN nodes n ON n.id = f.entry_point_id "
        f"WHERE n.qualified_name IN ({','.join('?' * len(names))}) "  # nosec B608
        "ORDER BY f.criticality DESC, f.id LIMIT 1",
        sorted(names),
    ).fetchone()
    return int(row[0]) if row else None


def get_flow(
    flow_id: int | None = None,
    flow_name: str | None = None,
    include_source: bool = False,
    repo_root: str | None = None,
    max_steps: int = 50,
    max_source_lines: int = 400,
    cursor: str | None = None,
    flow_key: str | None = None,
) -> dict[str, Any]:
    """Get details of a single execution flow.

    [EXPLORE] Retrieves full path details for a flow, including each step's
    function name, file, and line numbers.  Optionally includes source
    snippets for every step in the path.

    Args:
        flow_id: Database ID of the flow (from list_flows). Ids are renumbered when the
            flows are rebuilt (a reindex), so use ``flow_key`` to keep a reference.
        flow_key: The flow's stable key, as ``list_flows`` returns it: its entry node's
            qualified name, relative to the repository (``path/File.java::Class.method``).
            Used when ``flow_id`` is not given; takes precedence over ``flow_name``. No match
            is ``not_found``.
        flow_name: Name to search for: exact name, then qualified-name
                   segment/suffix, then partial match. Ignored if flow_id
                   given.
        include_source: If True, include source code snippets for each step.
        repo_root: Repository root path. Auto-detected if omitted.
        max_steps: Maximum steps to return (default 50, capped at 200).
            ``flow["total_steps"]`` reports the untruncated count.
        max_source_lines: Total source lines emitted across all steps when
            ``include_source`` is set (default 400, capped at 2000). Without
            this, one deep flow inlines every function body it touches.
            The budget is spent per reply, so each page gets a fresh one.
        cursor: ``next_cursor`` from the previous reply, to read the next
            steps of the same flow. Omit it for the first page. A cursor is
            opaque and only valid for the same flow on the same graph.

    Returns:
        Flow details with steps, or not_found status. ``flow["truncated"]``
        marks that ``max_steps`` or the source budget cut the response. When
        steps remain, ``next_cursor`` is the ``cursor`` that returns the next
        ones; a page after the first adds ``offset``. ``flow["path"]`` lists
        every step's node id on each page, so it is not paged.
    """
    _validate_positive_int(max_steps, "max_steps")
    _validate_positive_int(max_source_lines, "max_source_lines")

    store, root = _get_store(repo_root)
    try:
        flow: dict | None = None
        matches_total: int | None = None

        if flow_id is not None:
            flow = get_flow_by_id(store, flow_id)
        elif flow_key is not None:
            found_id = _flow_id_for_key(store, root, flow_key)
            flow = get_flow_by_id(store, found_id) if found_id is not None else None
        elif flow_name is not None:
            # Exact name, then qualified-name segment/suffix, then partial.
            # Highest criticality wins within a tier (list is sorted by it).
            all_flows = get_flows(
                store, sort_by="criticality", limit=500
            )
            needle = flow_name.lower()
            qns = {
                r["id"]: (r["qualified_name"] or "").lower()
                for r in store._conn.execute(
                    "SELECT id, qualified_name FROM nodes WHERE id IN (%s)"
                    % ",".join("?" * len(all_flows)),
                    [f["entry_point_id"] for f in all_flows],
                )
            } if all_flows else {}

            def _qn_match(f: dict) -> bool:
                qn = qns.get(f["entry_point_id"], "")
                tail = qn.rsplit("::", 1)[-1]
                if tail == needle or tail.endswith("." + needle):
                    return True
                segments = qn.replace("::", "/").replace(".", "/").split("/")
                return needle in segments

            tiers = (
                [f for f in all_flows if f["name"].lower() == needle],
                [f for f in all_flows if _qn_match(f)],
                [f for f in all_flows if needle in f["name"].lower()],
            )
            for matches in tiers:
                if matches:
                    flow = get_flow_by_id(store, matches[0]["id"])
                    matches_total = len(matches)
                    break

        if flow is None:
            return {
                "status": "not_found",
                "summary": "No flow found matching the given criteria.",
            }

        page = paginate(
            flow.get("steps") or [], max_steps, _MAX_FLOW_STEPS, cursor,
            scope=make_scope(
                "get_flow_tool",
                flow_id=flow.get("id", flow_id if flow_id is not None else flow_name),
            ),
            stamp=graph_stamp(store),
        )
        steps, total_steps = page.items, page.total
        attach_flow_keys(store, root, [flow])
        flow["steps"] = steps
        flow["total_steps"] = total_steps
        flow["truncated"] = page.truncated

        # Optionally include source snippets for each step, spending a shared
        # line budget so a deep flow cannot inline the whole call chain.
        if include_source:
            budget = min(max_source_lines, _MAX_FLOW_SOURCE_LINES)
            followed = followed_files(store)
            for step in steps:
                if budget <= 0:
                    flow["truncated"] = True
                    flow["source_truncated"] = True
                    break
                fp = Path(step["file"]) if step.get("file") else None
                if fp is not None and not fp.is_absolute():
                    fp = root / fp
                file_path = fp
                if (
                    file_path
                    and file_path.is_file()
                    and path_within_repo(root, file_path, followed)
                ):
                    try:
                        lines = file_path.read_text(encoding="utf-8",
                            errors="replace"
                        ).splitlines()
                        start = max(
                            0, (step.get("line_start") or 1) - 1
                        )
                        end = min(
                            len(lines),
                            step.get("line_end") or len(lines),
                            start + budget,
                        )
                        step["source"] = "\n".join(
                            f"{i + 1}: {lines[i]}"
                            for i in range(start, end)
                        )
                        budget -= max(0, end - start)
                    except (OSError, UnicodeDecodeError):
                        step["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "summary": (
                f"Flow '{flow['name']}': {flow['node_count']} nodes, "
                f"depth {flow['depth']}, "
                f"criticality {flow['criticality']:.4f}"
                + shown_range(page)
            ),
            "flow": flow,
            **page.fields(),
        }
        if matches_total is not None:
            result["matches_total"] = matches_total
        result["_hints"] = generate_hints(
            "get_flow_tool", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
