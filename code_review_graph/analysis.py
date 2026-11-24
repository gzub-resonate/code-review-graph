"""Graph analysis: hub detection, bridge nodes, knowledge gaps, import cycles,
surprise scoring, suggested questions."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .constants import (
    IMPORT_CONTEXT_DEFERRED,
    IMPORT_CONTEXT_KEY,
    IMPORT_CONTEXT_PARENT_PACKAGE,
    IMPORT_CONTEXT_TYPE_ONLY,
)
from .graph import (
    RESOLUTION_UNRESOLVED,
    GraphEdge,
    GraphNode,
    GraphStore,
    _sanitize_name,
)
from .migrations import TARGET_RESOLUTION_KINDS
from .parser import is_test_file, normalize_file_path

logger = logging.getLogger(__name__)

# How much of one import cycle is spelled out. A cycle can hold hundreds of files
# and each row carries paths, so the rest is told as a count.
_MAX_CYCLE_FILES = 10
_MAX_CYCLE_HOPS = 10
# The untested-hotspot list stops here; the tool never shows more.
_MAX_UNTESTED_HOTSPOTS = 20
# Files a package manager writes, whatever the ecosystem names them.
_LOCKFILE_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "pipfile.lock", "cargo.lock", "gemfile.lock",
    "composer.lock", "go.sum", "gradle.lockfile", "packages.lock.json", "flake.lock",
})
_LOCKFILE_SUFFIXES = (".lock", ".lock.hcl", ".lockfile", "-lock.json", "-lock.yaml")
# Folders that hold tooling around the code, not the code.
_NON_CODE_DIRS = frozenset({".storybook", ".github"})


#: Kinds that describe a configuration or data document, not code. A key in a JSON or YAML
#: file, a Liquid output or a Spring property is often a leaf by nature, so it is never a
#: knowledge gap just because nothing links to it.
CONFIG_DOCUMENT_KINDS = frozenset({"JsonPath", "YamlPath", "Output", "ConfigProperty"})


def _is_test_node(node: Any, repo_root: str | None) -> bool:
    """Whether *node* is test code: a Test node, a flagged one, or one in a test file."""
    return bool(
        node.kind == "Test"
        or node.is_test
        or is_test_file(node.file_path, repo_root)
    )


def _is_non_code(node: GraphNode) -> bool:
    """Is this node documentation, a lockfile entry or tooling, not code?

    Such nodes are isolated or unreferenced by nature (an HTML anchor, a provider
    pinned in ``.terraform.lock.hcl``, a Storybook mock, a Dockerfile stage), so
    listing them as gaps tells the reader nothing.
    """
    if node.kind == "DocSection" or node.name.startswith("stage:"):
        return True
    parts = node.file_path.replace("\\", "/").lower().split("/")
    base = parts[-1]
    return (
        base in _LOCKFILE_NAMES
        or base.endswith(_LOCKFILE_SUFFIXES)
        or base.startswith("dockerfile")
        or base.endswith(".dockerfile")
        or base == "containerfile"
        or any(part in _NON_CODE_DIRS for part in parts[:-1])
    )




def _is_constructor(node: GraphNode) -> bool:
    """Is this a constructor: a function named for the class that holds it?

    Java, Kotlin, C# and Scala write constructors that way, and the parser flags
    Java's. Frameworks (Guice, Spring) call them, so no call edge names them.
    """
    if node.kind != "Function":
        return False
    if node.extra.get("java_constructor"):
        return True
    return bool(node.parent_name) and node.name == node.parent_name.rsplit(".", 1)[-1]


# Edges that say a node builds on another one.
_OUTGOING_USE_KINDS = frozenset({"CALLS", "REFERENCES"})


def _is_bare_name_edge(edge: GraphEdge, known: set[str]) -> bool:
    """Is this call or reference bound to nothing but a bare name?

    ``unresolved`` is what ``query_graph_tool`` counts as a guess: no node answers
    to the target, so it is a name like ``get`` or ``parse`` that says nothing
    about which symbol was meant. A graph that stores no verdict falls back to the
    same node-existence test.
    """
    if edge.target_resolution is not None:
        return edge.target_resolution == RESOLUTION_UNRESOLVED
    return edge.kind in TARGET_RESOLUTION_KINDS and edge.target_qualified not in known


def _traced_edges(
    edges: list[GraphEdge], known: set[str],
) -> tuple[list[GraphEdge], dict[str, int]]:
    """The edges that name a node, and how many bare-name edges touch each node."""
    traced: list[GraphEdge] = []
    bare: dict[str, int] = Counter()
    for e in edges:
        if _is_bare_name_edge(e, known):
            bare[e.source_qualified] += 1
            bare[e.target_qualified] += 1
        else:
            traced.append(e)
    return traced, bare


def find_hub_nodes(
    store: GraphStore,
    top_n: int = 10,
    include_tests: bool = True,
    exclude_leaf_primitives: bool = False,
    include_config: bool = True,
) -> list[dict]:
    """Find the most connected nodes (highest in+out degree), excluding File nodes.

    Test nodes are left out unless *include_tests* is true, before ranking.
    Config document nodes (``$`` of a JSON file, YAML paths, outputs, properties) are
    left out the same way unless *include_config* is true: one of them is touched by
    every key beneath it and says nothing about structure.

    Edges that are only a bare-name match (an unresolved call target such as
    ``get`` or ``parse``) are not counted: they inflate whichever symbol makes
    many calls and say nothing about structure. ``resolution_split`` tells how
    many edges of each kind touch the node.

    With *exclude_leaf_primitives*, nodes that call or reference no indexed
    non-test node are skipped: a design-system component resolves correctly and
    still tells a reviewer nothing.

    Returns list of dicts with: name, qualified_name, kind, file,
    in_degree, out_degree, total_degree, community_id, resolution_split
    (``direct``: edges counted, ``bare_name``: edges left out)
    """
    # Get all non-File nodes
    nodes = store.get_all_nodes(exclude_files=True)
    node_map = {n.qualified_name: n for n in nodes}
    edges, bare = _traced_edges(store.get_all_edges(), set(node_map))
    in_degree: dict[str, int] = Counter()
    out_degree: dict[str, int] = Counter()
    builds_on: set[str] = set()
    for e in edges:
        out_degree[e.source_qualified] += 1
        in_degree[e.target_qualified] += 1
        if e.kind in _OUTGOING_USE_KINDS:
            target = node_map.get(e.target_qualified)
            if target is not None and not target.is_test:
                builds_on.add(e.source_qualified)
    community_map = store.get_all_community_ids()
    root = None if include_tests else store.get_repo_root()

    scored: list[dict[str, Any]] = []
    for n in nodes:
        if not include_tests and _is_test_node(n, root):
            continue
        if not include_config and n.kind in CONFIG_DOCUMENT_KINDS:
            continue
        qn = n.qualified_name
        ind = in_degree.get(qn, 0)
        outd = out_degree.get(qn, 0)
        total = ind + outd
        if total == 0:
            continue
        if exclude_leaf_primitives and qn not in builds_on:
            continue
        scored.append({
            "name": _sanitize_name(n.name),
            "qualified_name": n.qualified_name,
            "kind": n.kind,
            "file": n.file_path,
            "in_degree": ind,
            "out_degree": outd,
            "total_degree": total,
            "community_id": community_map.get(qn),
            "resolution_split": {"direct": total, "bare_name": bare.get(qn, 0)},
        })

    # Equal degrees are ordered by qualified name, so a ranking read in pages never
    # repeats or skips a node.
    scored.sort(key=lambda x: (-x["total_degree"], x["qualified_name"]))
    return scored[:top_n]


def find_bridge_nodes(
    store: GraphStore, top_n: int = 10, include_tests: bool = True,
) -> list[dict]:
    """Find nodes with highest betweenness centrality.

    These are architectural chokepoints that sit on shortest paths
    between many node pairs. If they break, multiple communities
    lose connectivity. Test nodes are left out unless *include_tests* is true; they still
    count as paths while betweenness is computed, and are dropped before ranking.

    Returns list of dicts with: name, qualified_name, kind, file,
    betweenness, community_id
    """
    import networkx as nx

    # Build the graph — use cached version if available. A call to a bare name
    # is no path: through it, every caller of ``get`` would sit next to every
    # other, and the shortest paths between regions would run through names.
    full = store._build_networkx_graph()
    nxg = full.edge_subgraph(
        (u, v) for u, v, d in full.edges(data=True) if not d.get("unresolved_target")
    ).copy()

    # Compute betweenness centrality (approximate for large graphs)
    n_nodes = nxg.number_of_nodes()
    if n_nodes > 5000:
        # Sample-based approximation for large graphs
        k = min(500, n_nodes)
        # A fixed seed keeps the sample, and so the ranking, the same on every call:
        # a ranking read in pages must not be redrawn between pages.
        bc = nx.betweenness_centrality(nxg, k=k, normalized=True, seed=0)
    elif n_nodes > 0:
        bc = nx.betweenness_centrality(nxg, normalized=True)
    else:
        return []

    community_map = store.get_all_community_ids()
    node_map = {
        n.qualified_name: n
        for n in store.get_all_nodes(exclude_files=True)
    }

    root = None if include_tests else store.get_repo_root()
    results = []
    for qn, score in bc.items():
        if score <= 0 or qn not in node_map:
            continue
        n = node_map[qn]
        if n.kind == "File":
            continue
        if not include_tests and _is_test_node(n, root):
            continue
        results.append({
            "name": _sanitize_name(n.name),
            "qualified_name": n.qualified_name,
            "kind": n.kind,
            "file": n.file_path,
            "betweenness": round(score, 6),
            "community_id": community_map.get(qn),
        })

    # Equal scores are ordered by qualified name, so a ranking read in pages never
    # repeats or skips a node.
    results.sort(key=lambda x: (-float(x["betweenness"]), str(x["qualified_name"])))  # type: ignore[arg-type]
    del nxg, full, bc
    store.shrink_memory(clear_nx_cache=True)
    return results[:top_n]


def _shortest_loop(adjacency: dict[str, set[str]], start: str) -> list[str]:
    """Return the fewest files that lead from *start* back to itself."""
    came_from: dict[str, str] = {}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbour in sorted(adjacency.get(current, ())):
            if neighbour == start:
                loop = [current]
                while loop[-1] != start:
                    loop.append(came_from[loop[-1]])
                return loop[::-1]
            if neighbour not in came_from:
                came_from[neighbour] = current
                queue.append(neighbour)
    return [start]


def find_import_cycles(
    store: GraphStore,
    edges: list[GraphEdge] | None = None,
    repo_root: Path | None = None,
) -> list[dict]:
    """Find groups of files that import each other, worst first.

    Only imports that run count: one under ``if TYPE_CHECKING`` or written
    ``import type`` never does. Only imports that name a file count too, so Go
    packages, which the compiler already keeps free of cycles, and C# namespaces
    are not followed.

    Returns list of dicts with: name (one loop, written out), size (files in the
    group), load_time (the loop can fail as the files load; False means every
    loop has an import that runs late), files, loop_length, and cycle (that
    loop's imports with their lines). Paths are relative to *repo_root* when it
    is given. Long lists are cut: size and loop_length keep the real counts.
    """
    import networkx as nx

    files = set(store.get_all_files())
    all_imports: dict[tuple[str, str], int] = {}
    load_imports: dict[tuple[str, str], int] = {}
    for edge in store.get_all_edges() if edges is None else edges:
        source, target = edge.source_qualified, edge.target_qualified
        if (
            edge.kind != "IMPORTS_FROM"
            or source == target
            or source not in files
            or target not in files
        ):
            continue
        context = edge.extra.get(IMPORT_CONTEXT_KEY)
        if context in (IMPORT_CONTEXT_TYPE_ONLY, IMPORT_CONTEXT_PARENT_PACKAGE):
            continue
        pair = (source, target)
        all_imports[pair] = min(all_imports.get(pair, edge.line), edge.line)
        if context != IMPORT_CONTEXT_DEFERRED:
            load_imports[pair] = min(load_imports.get(pair, edge.line), edge.line)

    groups = [
        group
        for group in nx.strongly_connected_components(nx.DiGraph(list(all_imports)))
        if len(group) > 1
    ]
    if not groups:
        return []
    load_groups = sorted(
        (
            group
            for group in nx.strongly_connected_components(nx.DiGraph(list(load_imports)))
            if len(group) > 1
        ),
        key=lambda group: (-len(group), min(group)),
    )

    prefix = f"{normalize_file_path(repo_root)}/" if repo_root else ""

    def short(path: str) -> str:
        """Drop the repository's own folder: it would repeat in every row."""
        return _sanitize_name(path.removeprefix(prefix))

    def adjacency(imports: dict[tuple[str, str], int]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for source, target in imports:
            result[source].add(target)
        return result

    all_adjacency = adjacency(all_imports)
    load_adjacency = adjacency(load_imports)

    cycles: list[dict] = []
    for group in groups:
        # A loop that runs at load is the one that can fail, so show it first.
        load_group = next((g for g in load_groups if g <= group), None)
        if load_group is not None:
            loop = _shortest_loop(load_adjacency, min(load_group))
        else:
            loop = _shortest_loop(all_adjacency, min(group))
        hops = list(zip(loop, loop[1:] + loop[:1]))
        cycles.append({
            "name": _sanitize_name(" -> ".join(short(path) for path in [*loop, loop[0]])),
            "size": len(group),
            "load_time": load_group is not None,
            "files": [short(path) for path in sorted(group)[:_MAX_CYCLE_FILES]],
            "loop_length": len(loop),
            "cycle": [
                {
                    "file": short(source),
                    "line": load_imports.get((source, target), all_imports[(source, target)]),
                    "imports": short(target),
                }
                for source, target in hops[:_MAX_CYCLE_HOPS]
            ],
        })
    cycles.sort(key=lambda c: (not c["load_time"], -c["size"], c["name"]))
    return cycles


def find_knowledge_gaps(
    store: GraphStore,
    repo_root: Path | None = None,
    max_isolated: int | None = 50,
    max_untested: int | None = _MAX_UNTESTED_HOTSPOTS,
    include_tests: bool = True,
    include_non_code: bool = False,
) -> dict[str, list[dict]]:
    """Identify structural weaknesses in the codebase graph.

    Documentation sections, lockfile entries, Dockerfile stages and anything under
    ``.storybook`` or ``.github`` are left out of ``isolated_nodes`` and
    ``untested_hotspots`` unless *include_non_code* is set. So are constructors
    held by their class: a framework such as Guice calls them, so nothing in the
    graph does. A constructor with no CONTAINS edge is still reported.

    Returns dict with categories:
    - isolated_nodes: degree <= 1, disconnected from graph
    - thin_communities: fewer than 3 members
    - untested_hotspots: high-degree nodes with no TESTED_BY edges
    - single_file_communities: entire community in one file
    - import_cycles: files that import each other (see find_import_cycles)

    Configuration-document nodes (``CONFIG_DOCUMENT_KINDS``) are never isolated nodes.
    Unless *include_tests* is true, test nodes are left out of the node lists and a
    community made only of test nodes is left out of the community lists.

    Isolated nodes and untested hotspots are cut at *max_isolated* and *max_untested*
    (``None`` keeps every row, for a caller that pages). Every list has a fixed order.
    """
    edges = store.get_all_edges()
    # Table order made explicit: the isolated list is read in node id order.
    all_nodes = sorted(store.get_all_nodes(exclude_files=True), key=lambda n: n.id)
    community_map = store.get_all_community_ids()
    test_root = None if include_tests else store.get_repo_root()
    test_qns = {n.qualified_name for n in all_nodes if _is_test_node(n, test_root)}
    nodes = all_nodes if include_tests else [
        n for n in all_nodes if n.qualified_name not in test_qns
    ]

    # Build degree map
    degree: dict[str, int] = Counter()
    contained: set[str] = set()
    for e in edges:
        degree[e.source_qualified] += 1
        degree[e.target_qualified] += 1
        if e.kind == "CONTAINS":
            contained.add(e.target_qualified)

    def expected(n: GraphNode) -> bool:
        """Never a gap: non-code, or a constructor its class contains (wired by a framework)."""
        if include_non_code:
            return False
        return _is_non_code(n) or (_is_constructor(n) and n.qualified_name in contained)

    # 1. Isolated nodes (degree <= 1, not File)
    isolated = []
    for n in nodes:
        d = degree.get(n.qualified_name, 0)
        # A config key is never a gap, except a non-code one (a lockfile entry) that
        # include_non_code asked for.
        config_key = n.kind in CONFIG_DOCUMENT_KINDS and not (
            include_non_code and _is_non_code(n)
        )
        if d <= 1 and not config_key and not expected(n):
            isolated.append({
                "name": _sanitize_name(n.name),
                "qualified_name": n.qualified_name,
                "kind": n.kind,
                "file": n.file_path,
                "degree": d,
            })

    # 2. Build community sizes and file maps from node data
    comm_sizes: Counter[int] = Counter()
    comm_files: dict[int, set[str]] = defaultdict(set)
    # Communities are structural, so their size counts every member; but a community
    # whose members are all tests says nothing about production code.
    comm_prod: set[int] = set()
    comm_test: set[int] = set()
    comm_code: set[int] = set()
    for n in all_nodes:
        cid = community_map.get(n.qualified_name)
        if cid is not None:
            comm_sizes[cid] += 1
            comm_files[cid].add(n.file_path)
            (comm_test if n.qualified_name in test_qns else comm_prod).add(cid)
            if n.kind not in CONFIG_DOCUMENT_KINDS:
                comm_code.add(cid)

    # Thin communities (< 3 members). A community made only of JSON or YAML
    # paths is one config document, which is a single file by nature.
    communities = sorted(
        (c for c in store.get_communities_list()
         if include_tests
         or int(c["id"]) not in comm_test - comm_prod),
        key=lambda c: int(c["id"]),
    )
    thin = []
    for c in communities:
        cid = int(c["id"])
        if cid not in comm_code and comm_sizes.get(cid, 0) > 0:
            continue
        size = comm_sizes.get(cid, 0)
        if size < 3:
            thin.append({
                "community_id": cid,
                "name": str(c["name"]),
                "size": size,
            })

    # 3. Untested hotspots (degree >= 5, no covering test). "Tested" is what
    # tests_for answers: a direct test, or one that reaches the node through
    # test helpers. A test of something the node merely calls is not coverage.
    # A hotspot is ranked like a hub: by the edges that name a node.
    named: dict[str, int] = Counter()
    for e in _traced_edges(edges, {n.qualified_name for n in nodes})[0]:
        named[e.source_qualified] += 1
        named[e.target_qualified] += 1
    candidates = [
        n for n in nodes
        if named.get(n.qualified_name, 0) >= 5 and not n.is_test
        and not expected(n)
    ]
    candidates.sort(key=lambda n: (-named.get(n.qualified_name, 0), n.qualified_name))
    untested_hotspots: list[dict] = []
    for n in candidates:
        if max_untested is not None and len(untested_hotspots) >= max_untested:
            break
        if store.get_transitive_tests(n.qualified_name, include_callee_routes=False):
            continue
        untested_hotspots.append({
            "name": _sanitize_name(n.name),
            "qualified_name": n.qualified_name,
            "kind": n.kind,
            "file": n.file_path,
            "degree": named[n.qualified_name],
        })

    # 4. Single-file communities
    single_file = []
    for c in communities:
        cid = int(c["id"])
        files = comm_files.get(cid, set())
        size = comm_sizes.get(cid, 0)
        if cid not in comm_code:
            continue
        if len(files) == 1 and size >= 3:
            single_file.append({
                "community_id": cid,
                "name": str(c["name"]),
                "size": size,
                "file": next(iter(files)),
            })

    return {
        "isolated_nodes": isolated[:max_isolated],
        "thin_communities": thin,
        "untested_hotspots": untested_hotspots[:max_untested],
        "single_file_communities": single_file,
        "import_cycles": find_import_cycles(store, edges, repo_root),
    }


def find_surprising_connections(
    store: GraphStore, top_n: int = 15, include_tests: bool = True,
) -> list[dict]:
    """Find edges with high surprise scores.

    Detects unexpected architectural coupling based on:
    - Cross-community: source and target in different communities
    - Cross-language: different file languages
    - Peripheral-to-hub: low-degree node to high-degree node
    - Cross-file-type: test calling production or vice versa
    - Non-standard edge kind for the node types

    Unless *include_tests* is true, an edge with a test node at either end is dropped
    before scoring and ranking.
    """
    nodes = store.get_all_nodes(exclude_files=True)
    community_map = store.get_all_community_ids()

    node_map = {n.qualified_name: n for n in nodes}
    root = None if include_tests else store.get_repo_root()
    # A bare-name match is no coupling, and one call made in five places is one
    # row, not five: rank each (source, target, kind) once.
    edges: list[GraphEdge] = []
    row_keys: set[tuple[str, str, str]] = set()
    traced, _bare = _traced_edges(store.get_all_edges(), set(node_map))
    for e in traced:
        key = (e.source_qualified, e.target_qualified, e.kind)
        if key not in row_keys:
            row_keys.add(key)
            edges.append(e)

    # A class holding its method, or a JSON key holding its child, is how the
    # code is laid out, never a coupling. Leave those edges out of the candidates
    # (and so out of the totals). Degrees still count them: they are real links.
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.kind == "CONTAINS":
            parent_of.setdefault(e.target_qualified, e.source_qualified)
    for qn, node in node_map.items():
        if node.parent_name and qn not in parent_of:
            owner = f"{node.file_path}::{node.parent_name}"
            if owner in node_map:
                parent_of[qn] = owner

    def _is_ancestor(ancestor: str, qn: str) -> bool:
        seen: set[str] = set()
        while qn in parent_of and qn not in seen:
            seen.add(qn)
            qn = parent_of[qn]
            if qn == ancestor:
                return True
        return False

    # Build degree map from every traced edge, as the hub ranking counts them.
    degree: dict[str, int] = Counter()
    for e in traced:
        degree[e.source_qualified] += 1
        degree[e.target_qualified] += 1

    edges = [
        e for e in edges
        if e.kind != "CONTAINS"
        and not _is_ancestor(e.source_qualified, e.target_qualified)
        and not _is_ancestor(e.target_qualified, e.source_qualified)
    ]

    # Median degree for peripheral detection
    degrees = [d for d in degree.values() if d > 0]
    if not degrees:
        return []
    median_deg = sorted(degrees)[len(degrees) // 2]
    high_deg_threshold = max(median_deg * 3, 10)

    scored_edges: list[tuple[int, dict[str, Any]]] = []
    for e in edges:
        src = node_map.get(e.source_qualified)
        tgt = node_map.get(e.target_qualified)
        if not src or not tgt:
            continue
        if src.kind == "File" or tgt.kind == "File":
            continue
        if not include_tests and (_is_test_node(src, root) or _is_test_node(tgt, root)):
            continue

        score = 0.0
        reasons = []

        # Cross-community (+0.3)
        src_cid = community_map.get(e.source_qualified)
        tgt_cid = community_map.get(e.target_qualified)
        if (src_cid is not None
                and tgt_cid is not None
                and src_cid != tgt_cid):
            score += 0.3
            reasons.append("cross-community")

        # Cross-language (+0.2)
        src_lang = (
            src.file_path.rsplit(".", 1)[-1]
            if "." in src.file_path else ""
        )
        tgt_lang = (
            tgt.file_path.rsplit(".", 1)[-1]
            if "." in tgt.file_path else ""
        )
        if src_lang and tgt_lang and src_lang != tgt_lang:
            score += 0.2
            reasons.append("cross-language")

        # Peripheral-to-hub (+0.2)
        src_deg = degree.get(e.source_qualified, 0)
        tgt_deg = degree.get(e.target_qualified, 0)
        if ((src_deg <= 2 and tgt_deg >= high_deg_threshold)
                or (tgt_deg <= 2
                    and src_deg >= high_deg_threshold)):
            score += 0.2
            reasons.append("peripheral-to-hub")

        # Cross-file-type: test <-> non-test (+0.15)
        if src.is_test != tgt.is_test and e.kind == "CALLS":
            score += 0.15
            reasons.append("cross-test-boundary")

        # Non-standard edge kind (+0.15)
        if e.kind == "CALLS" and src.kind == "Type":
            score += 0.15
            reasons.append("unusual-edge-kind")

        if score > 0:
            scored_edges.append((e.id, {
                "source": _sanitize_name(src.name),
                "source_qualified": e.source_qualified,
                "target": _sanitize_name(tgt.name),
                "target_qualified": e.target_qualified,
                "source_file": src.file_path,
                "target_file": tgt.file_path,
                "edge_kind": e.kind,
                "surprise_score": round(score, 2),
                "reasons": reasons,
                "source_community": src_cid,
                "target_community": tgt_cid,
            }))

    # Equal scores are ordered by endpoints, edge kind and edge id (two edges can join
    # the same pair), so a ranking read in pages never repeats or skips an edge.
    scored_edges.sort(key=lambda item: (
        -float(item[1]["surprise_score"]), item[1]["source_qualified"],
        item[1]["target_qualified"], item[1]["edge_kind"], item[0],
    ))
    return [row for _, row in scored_edges[:top_n]]


def generate_suggested_questions(
    store: GraphStore, include_tests: bool = True,
) -> list[dict]:
    """Auto-generate review questions from graph analysis.

    Categories:
    - bridge_node: Why does X connect communities A and B?
    - isolated_node: Is X dead code or dynamically invoked?
    - low_cohesion: Should community X be split?
    - hub_risk: Does hub node X have adequate test coverage?
    - surprising: Why does A call B across community boundary?
    """
    questions = []

    # Bridge node questions
    bridges = find_bridge_nodes(store, top_n=3, include_tests=include_tests)
    for b in bridges:
        questions.append({
            "category": "bridge_node",
            "question": (
                f"'{b['name']}' is a critical connector "
                f"between multiple code regions. Is it "
                f"adequately tested and documented?"
            ),
            "target": b["qualified_name"],
            "priority": "high",
        })

    # Hub risk questions
    hubs = find_hub_nodes(store, top_n=3, include_tests=include_tests)
    edges = store.get_all_edges()
    tested = {
        e.source_qualified
        for e in edges if e.kind == "TESTED_BY"
    }
    for h in hubs:
        if h["qualified_name"] not in tested:
            questions.append({
                "category": "hub_risk",
                "question": (
                    f"Hub node '{h['name']}' has "
                    f"{h['total_degree']} connections but no "
                    f"direct test coverage. Should it be "
                    f"tested?"
                ),
                "target": h["qualified_name"],
                "priority": "high",
            })

    # Surprising connection questions
    surprises = find_surprising_connections(store, top_n=3, include_tests=include_tests)
    for s in surprises:
        if "cross-community" in s["reasons"]:
            questions.append({
                "category": "surprising_connection",
                "question": (
                    f"'{s['source']}' (community "
                    f"{s['source_community']}) calls "
                    f"'{s['target']}' (community "
                    f"{s['target_community']}). Is this "
                    f"coupling intentional?"
                ),
                "target": s["source_qualified"],
                "priority": "medium",
            })

    # Knowledge gap questions
    gaps = find_knowledge_gaps(store, include_tests=include_tests)

    for c in gaps["thin_communities"][:2]:
        questions.append({
            "category": "thin_community",
            "question": (
                f"Community '{c['name']}' has only "
                f"{c['size']} member(s). Should it be "
                f"merged with a neighbor?"
            ),
            "target": f"community:{c['community_id']}",
            "priority": "low",
        })

    for h in gaps["untested_hotspots"][:2]:
        questions.append({
            "category": "untested_hotspot",
            "question": (
                f"'{h['name']}' has {h['degree']} "
                f"connections but no test coverage. "
                f"Is this a risk?"
            ),
            "target": h["qualified_name"],
            "priority": "medium",
        })

    return questions
