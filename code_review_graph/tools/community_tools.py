"""Tools 13, 14, 15: community listing, detail, architecture overview."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..communities import (
    count_hidden_config_communities,
    get_architecture_overview,
    get_communities,
)
from ..context_history import CallInfo
from ..context_savings import attach_context_savings
from ..graph import node_to_dict
from ..hints import generate_hints, get_session
from ..paging import Page, graph_stamp, make_scope, paginate, shown_range
from ._common import _bounded, _get_store, _page_together, _validate_positive_int

# ---------------------------------------------------------------------------
# Tool 13: list_communities  [EXPLORE]
# ---------------------------------------------------------------------------

# Hard ceilings. ``get_communities`` embeds every member's qualified name in
# each community, so a repo with one 3000-member community produced a 200k+
# token response before this cap (the same class of bug as #849).
_MAX_COMMUNITIES = 200
# A member entry is one absolute qualified name, ~36 tokens. 25 per
# community keeps a 200-community overview inside a single response.
_MAX_MEMBERS = 25
# A per-edge cross-community row carries two absolute qualified names,
# ~100 tokens. 200 rows is the most a standard overview can afford.
_MAX_CROSS_EDGES = 200


def _cap_members(
    community: dict[str, Any], max_members: int,
) -> dict[str, Any]:
    """Return a copy of *community* whose member lists are bounded.

    ``size`` already carries the true member count, so the caller never
    loses the total; ``members_truncated`` marks that the list was cut.
    """
    capped = dict(community)
    for key in ("members", "member_details"):
        rows = capped.get(key)
        if not isinstance(rows, list):
            continue
        visible, total, truncated = _bounded(rows, max_members, _MAX_MEMBERS)
        capped[key] = visible
        if truncated:
            capped[f"{key}_total"] = total
            capped["members_truncated"] = True
    return capped


def _page_members(
    community: dict[str, Any], max_members: int, cursor: str | None, store: Any, *, scope: str,
) -> tuple[dict[str, Any], Page]:
    """Like ``_cap_members``, but reads the page a cursor points at.

    ``members`` and ``member_details`` share one offset. The first page is what
    ``_cap_members`` returns. ``members_truncated`` is set while members remain after
    the page; a later page also carries ``<key>_total``, the full length of its list.
    """
    keys = [k for k in ("members", "member_details") if isinstance(community.get(k), list)]
    shown, page = _page_together(
        {k: community[k] for k in keys}, max_members, _MAX_MEMBERS, cursor,
        scope=scope, stamp=graph_stamp(store),
    )
    paged = dict(community)
    for key in keys:
        paged[key] = shown[key]
        if page.offset or len(community[key]) > len(shown[key]) + page.offset:
            paged[f"{key}_total"] = len(community[key])
    if page.truncated:
        paged["members_truncated"] = True
    return paged, page


def list_communities_func(
    repo_root: str | None = None,
    sort_by: str = "size",
    min_size: int = 0,
    detail_level: str = "standard",
    max_results: int = 50,
    max_members: int = 10,
    cursor: str | None = None,
    include_config: bool = False,
) -> dict[str, Any]:
    """List detected code communities in the codebase.

    [EXPLORE] Retrieves stored communities from the knowledge graph.
    Each community represents a cluster of related code entities
    (functions, classes) detected via the Leiden algorithm or
    file-based grouping.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include (default: 0).
        detail_level: "standard" (default) returns full community data;
                      "minimal" returns only name, size, and cohesion
                      per community.
        max_results: Maximum communities to return (default 50, capped at
                     200). ``total`` reports the untruncated count.
        max_members: Maximum member names listed per community in standard
                     mode (default 10, capped at 100). Each community's
                     ``size`` still reports its true member count.
        cursor: ``next_cursor`` from the previous reply, to read the next page
                of the same list. Omit it for the first page. A cursor is opaque
                and only valid for the same arguments on the same graph.
        include_config: Also list the communities made only of JSON and YAML
                        document nodes (default False). They are often hundreds
                        of equal slices of data files and bury the code. Their
                        number is reported as ``config_communities_hidden``.
                        A cursor is only valid for the value it was made with.

    Returns:
        Communities with size and cohesion scores, plus ``total`` and
        ``truncated``. When more communities remain, ``next_cursor`` is the
        ``cursor`` that returns the next page.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_members, "max_members")

    store, root = _get_store(repo_root)
    try:
        items = get_communities(
            store, sort_by=sort_by, min_size=min_size, include_config=include_config,
        )
        # A repository of data files has no code communities. Hiding config would
        # leave an empty list, so the config ones are listed instead and the reply says so.
        config_only = False
        if not include_config and not items and count_hidden_config_communities(
            store, min_size,
        ):
            items = get_communities(
                store, sort_by=sort_by, min_size=min_size, include_config=True,
            )
            config_only = True
        page = paginate(
            items,
            max_results, _MAX_COMMUNITIES, cursor,
            scope=make_scope(
                "list_communities_tool", sort_by=sort_by, min_size=min_size,
                include_config=include_config or config_only,
            ),
            stamp=graph_stamp(store),
        )
        communities, total = page.items, page.total
        if detail_level == "minimal":
            communities = [
                {"name": c["name"], "size": c["size"], "cohesion": c["cohesion"]}
                for c in communities
            ]
        else:
            communities = [_cap_members(c, max_members) for c in communities]
        result: dict[str, object] = {
            "status": "ok",
            "summary": (
                f"Found {total} communities"
                + shown_range(page)
            ),
            "communities": communities,
            "total": total,
            "truncated": page.truncated,
            **page.fields(),
        }
        if config_only:
            result["note"] = _CONFIG_ONLY_NOTE
        elif not include_config:
            hidden = count_hidden_config_communities(store, min_size)
            if hidden:
                result["config_communities_hidden"] = hidden
        result["_hints"] = generate_hints(
            "list_communities_tool", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 14: get_community  [EXPLORE]
# ---------------------------------------------------------------------------


def get_community_func(
    community_name: str | None = None,
    community_id: int | None = None,
    include_members: bool = False,
    repo_root: str | None = None,
    max_members: int = 25,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Get details of a single code community.

    [EXPLORE] Retrieves a community by its database ID or by name match.
    Optionally includes the full list of member nodes.

    Args:
        community_name: Name to search for (partial match). Ignored if
                        community_id given.
        community_id: Database ID of the community.
        include_members: If True, include full member node details.
        repo_root: Repository root path. Auto-detected if omitted.
        max_members: Maximum member entries to include (default 25, capped
                     at 100). The community's ``size`` still reports its
                     true member count and ``members_truncated`` marks the
                     cut. Without this bound a single 3000-member community
                     serialized to >130k tokens.
        cursor: ``next_cursor`` from the previous reply, to read the next
                members of the same community. Omit it for the first page.
                A cursor is opaque and only valid for the same community on
                the same graph. ``members`` and ``member_details`` advance
                together.

    Returns:
        Community details, or not_found status. When members remain,
        ``next_cursor`` is the ``cursor`` that returns the next ones; a page
        after the first adds ``offset`` and ``members_total``.
    """
    _validate_positive_int(max_members, "max_members")

    store, root = _get_store(repo_root)
    try:
        community: dict | None = None
        all_communities = get_communities(store)

        if community_id is not None:
            for c in all_communities:
                if c.get("id") == community_id:
                    community = c
                    break
        elif community_name is not None:
            for c in all_communities:
                if community_name.lower() in c["name"].lower():
                    community = c
                    break

        if community is None:
            return {
                "status": "not_found",
                "summary": (
                    "No community found matching the given criteria."
                ),
            }

        if include_members:
            cid = community.get("id")
            if cid is not None:
                member_nodes = store.get_nodes_by_community_id(cid)
                members = [node_to_dict(n) for n in member_nodes]
                community["member_details"] = members

        community, page = _page_members(
            community, max_members, cursor, store,
            scope=make_scope(
                "get_community_tool", community_id=community.get("id"),
                include_members=include_members,
            ),
        )

        result = {
            "status": "ok",
            "summary": (
                f"Community '{community['name']}': "
                f"{community['size']} nodes, "
                f"cohesion {community['cohesion']:.4f}"
            ),
            "community": community,
            **page.fields(),
        }
        result["_hints"] = generate_hints(
            "get_community_tool", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 15: get_architecture_overview  [EXPLORE]
# ---------------------------------------------------------------------------


#: Said when a repository has only config communities, so hiding them would leave nothing.
_CONFIG_ONLY_NOTE = (
    "This repository has no code communities, so the communities made of JSON and YAML "
    "documents are shown. Pass include_config=true to ask for them explicitly."
)

_MINIMAL_COMMUNITY_FIELDS = ("id", "name", "size", "cohesion", "dominant_language")


def _minimal_overview(overview: dict[str, Any]) -> dict[str, Any]:
    """Compress overview for ``detail_level="minimal"``.

    The full overview can exceed 600KB on medium repos because it embeds
    every community's member list and every individual cross-community
    edge. Minimal mode drops member lists and aggregates the edge list
    to one row per community pair with a count and the top edge kinds —
    enough to spot coupling smells without exploding token budgets.
    """
    communities = [
        {k: c[k] for k in _MINIMAL_COMMUNITY_FIELDS if k in c}
        for c in overview.get("communities", [])
    ]
    id_to_name = {c["id"]: c["name"] for c in communities if "id" in c}

    edge_pair_counts: Counter[tuple[int, int]] = Counter()
    edge_pair_kinds: dict[tuple[int, int], Counter[str]] = {}
    for e in overview.get("cross_community_edges", []):
        # Use canonical (low, high) ordering so A↔B and B↔A aggregate together.
        a, b = e["source_community"], e["target_community"]
        pair = (a, b) if a <= b else (b, a)
        edge_pair_counts[pair] += 1
        edge_pair_kinds.setdefault(pair, Counter())[e["edge_kind"]] += 1

    cross_pairs = [
        {
            "source_community": id_to_name.get(a, f"community-{a}"),
            "target_community": id_to_name.get(b, f"community-{b}"),
            "edge_count": count,
            "top_kinds": [k for k, _ in edge_pair_kinds[(a, b)].most_common(3)],
        }
        for (a, b), count in edge_pair_counts.most_common()
    ]
    return {
        "communities": communities,
        "cross_community_edges": cross_pairs,
        "warnings": overview.get("warnings", []),
    }


def get_architecture_overview_func(
    repo_root: str | None = None,
    detail_level: str = "minimal",
    max_results: int = 100,
    max_members: int = 10,
    cursor: str | None = None,
    include_config: bool = False,
) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    [EXPLORE] Builds a high-level view of the codebase architecture by
    analyzing community boundaries and cross-community coupling.
    Includes warnings for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "minimal" (default) drops community member lists
                      and aggregates edges to one row per community pair
                      (typical reduction: 600KB -> <5KB);
                      "standard" returns the full overview including
                      per-edge cross-community detail.
        max_results: Maximum cross-community rows and warnings to return
                     (default 100, capped at 200).
                     ``cross_community_edges_total`` reports the
                     untruncated count.
        max_members: (standard only) Maximum member names per community
                     (default 10, capped at 25). Standard mode returned
                     >600k tokens on this repo without these bounds.
        cursor: ``next_cursor`` from the previous reply, to read the next
                cross-community rows and warnings. Omit it for the first
                page. A cursor is opaque and only valid for the same
                ``detail_level`` on the same graph. The two lists advance
                together; the community list and each community's member
                names are not paged (read a community's members with
                ``get_community_tool``).
        include_config: Also include the communities made only of JSON and YAML
                document nodes, with the coupling rows and warnings about them
                (default False). Their number is reported as
                ``config_communities_hidden``. A cursor is only valid for the
                value it was made with.

    Returns:
        Architecture overview with communities, cross-community edges,
        warnings, and a ``truncated`` flag. When rows remain,
        ``next_cursor`` is the ``cursor`` that returns the next ones.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_members, "max_members")

    store, root = _get_store(repo_root)
    try:
        full_overview = get_architecture_overview(store, include_config=include_config)
        config_only = False
        if (
            not include_config
            and not full_overview["communities"]
            and count_hidden_config_communities(store)
        ):
            full_overview = get_architecture_overview(store, include_config=True)
            config_only = True
        overview = full_overview
        if detail_level == "minimal":
            overview = _minimal_overview(full_overview)
        else:
            overview = dict(full_overview)
            overview["communities"] = [
                _cap_members(c, max_members)
                for c in overview.get("communities", [])
            ]
        cross_total = len(overview["cross_community_edges"])
        warn_total = len(overview["warnings"])
        # One warning per highly-coupled community pair grows quadratically
        # with the community count, so it needs the same bound. Both lists
        # advance together under one cursor.
        shown, page = _page_together(
            {"cross": overview["cross_community_edges"], "warnings": overview["warnings"]},
            max_results, _MAX_CROSS_EDGES, cursor,
            scope=make_scope(
                "get_architecture_overview_tool", detail_level=detail_level,
                include_config=include_config or config_only,
            ),
            stamp=graph_stamp(store),
        )
        cross = shown["cross"]
        overview["cross_community_edges"] = cross
        overview["warnings"] = shown["warnings"]
        truncated = page.truncated
        n_communities = len(overview["communities"])
        n_warnings = warn_total
        cross_label = (
            "community pairs"
            if detail_level == "minimal"
            else "cross-community edges"
        )
        result = {
            "status": "ok",
            "summary": (
                f"Architecture: {n_communities} communities, "
                f"{cross_total} {cross_label}"
                + shown_range(Page(cross, cross_total, page.offset, None)) + ", "
                f"{n_warnings} warning(s)"
            ),
            **overview,
            "cross_community_edges_total": cross_total,
            "truncated": truncated,
            **page.fields(),
        }
        if config_only:
            result["note"] = _CONFIG_ONLY_NOTE
        elif not include_config:
            hidden = count_hidden_config_communities(store)
            if hidden:
                result["config_communities_hidden"] = hidden
        result["_hints"] = generate_hints(
            "get_architecture_overview_tool", result, get_session()
        )
        if detail_level == "minimal":
            attach_context_savings(
                result,
                original_context=full_overview,
                call=CallInfo(
                    data_dir=store.db_path.parent,
                    tool="get_architecture_overview_tool",
                    detail_level=detail_level,
                ),
            )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
