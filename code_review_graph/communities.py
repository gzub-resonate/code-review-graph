"""Community/cluster detection for the code knowledge graph.

Detects communities of related code nodes using the Leiden algorithm (via igraph,
optional) with a file-based grouping fallback when igraph is not installed.
"""

from __future__ import annotations

import importlib.util
import logging
import random
import re
from collections import Counter, defaultdict, namedtuple
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .constants import env_int
from .graph import GraphEdge, GraphNode, GraphStore, _sanitize_name

_CommunityEdge = namedtuple(
    "_CommunityEdge", ("kind", "source_qualified", "target_qualified")
)

# Fixed seed for igraph's RNG so Leiden community detection is reproducible
# across runs. Without this, two builds of the same graph produce different
# community IDs / sizes, breaking benchmark comparability. Override with
# CRG_LEIDEN_SEED env var if you need a different seed.
_LEIDEN_SEED = 42

logger = logging.getLogger(__name__)

# Stay well under SQLite's default 999-variable limit per statement.
_SQL_BATCH = 450
_SLUG_MAX_LEN = 30

# ---------------------------------------------------------------------------
# Optional igraph import
# ---------------------------------------------------------------------------

IGRAPH_AVAILABLE = importlib.util.find_spec("igraph") is not None
ig: Any = None

#: Metadata key naming the algorithm behind the stored communities, and its
#: values. Risk scores weigh callers from other communities, so a report has
#: to be able to say which grouping it was scored against.
DETECTOR_METADATA_KEY = "community_detector"
DETECTOR_LEIDEN = "leiden"
DETECTOR_FILE = "file"

# ---------------------------------------------------------------------------
# Edge weight mapping
# ---------------------------------------------------------------------------

EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "IMPORTS_FROM": 0.5,
    "INHERITS": 0.8,
    "IMPLEMENTS": 0.7,
    "CONTAINS": 0.3,
    "TESTED_BY": 0.4,
    "DEPENDS_ON": 0.6,
}

#: Node kinds that are rows of a JSON/YAML document rather than code. Every
#: environment's copy of a config file yields the same tree of these nodes, so
#: Leiden would give each copy its own community. They are kept out of Leiden
#: and grouped by directory instead (see ``_group_config_documents``).
CONFIG_DOCUMENT_KINDS = frozenset({"JsonPath", "YamlPath"})

#: Edge kinds that bind a member to its owner: a class to its methods, a method
#: to the endpoint it handles. The two ends are always detected together.
_PIN_EDGE_KINDS = frozenset({"CONTAINS", "HANDLES"})

#: Communities with fewer members than this are folded into a neighbour.
_MIN_COMMUNITY_MEMBERS = 3

#: Directory names that say nothing about what lives in them.
_GENERIC_DIRS = frozenset({
    "src", "main", "java", "kotlin", "scala", "lib", "app", "source", "sources",
    "python", "js", "ts", "com", "org", "net", "io", "code", "packages",
})

# Common words to filter when generating community names
_COMMON_WORDS = frozenset({
    "get", "set", "self", "init", "new", "create", "update", "delete",
    "add", "remove", "make", "build", "from", "to", "for", "with",
    "the", "and", "test", "main", "run", "do", "is", "has", "on",
    "of", "in", "at", "by", "my", "this", "that", "all", "none",
    "should", "when", "then", "given", "return", "returns", "raise",
    "raises", "expect", "expected", "assert", "tests", "be", "it", "if",
    "not",
})


# ---------------------------------------------------------------------------
# Community naming
# ---------------------------------------------------------------------------


def _is_test_node(node: GraphNode) -> bool:
    """Return whether a graph node represents test code."""
    return node.kind == "Test" or node.is_test


def _naming_members(members: list[GraphNode]) -> list[GraphNode]:
    """Prefer production nodes as the source of community name vocabulary."""
    production_members = [
        member for member in members
        if not _is_test_node(member) and not _is_test_file(member.file_path)
    ]
    return production_members or [m for m in members if not _is_test_node(m)] or members


_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|[^/]*(Tests?|IT|_test|\.test|\.spec)\.[^/.]+$"
)


def _is_test_file(file_path: str) -> bool:
    """Return whether a path looks like a test file (``FooTest.java``, ``test_foo.py``)."""
    return bool(_TEST_FILE_RE.search(file_path.replace("\\", "/")))


def _generate_community_name(members: list[GraphNode]) -> str:
    """Generate a meaningful name for a community of nodes.

    Algorithm (production members only when there are any):
    1. The dominant directory: the most common parent directory, skipping
       generic ones such as ``src`` or ``java``.
    2. If one file holds more than 40% of the members, name the community
       after the class in it (or the file stem), so a controller and its
       methods read as ``controller-hierarchy-wave-log-controller``.
    3. Otherwise add the most frequent keyword, taken from class names first
       and from all names only when there are no classes.
    4. Format: "{directory}-{class or keyword}"
    """
    if not members:
        return "empty"

    naming_members = _naming_members(members)

    prefix = _extract_file_prefix([m.file_path for m in naming_members])

    # Dominant file -> its class (or the file stem)
    file_counts = Counter(m.file_path for m in naming_members)
    top_file, top_count = min(file_counts.items(), key=lambda item: (-item[1], item[0]))
    if top_count > len(naming_members) * 0.4:
        classes_in_file = [
            m.name for m in naming_members
            if m.kind == "Class" and m.file_path == top_file
        ]
        if len(classes_in_file) == 1:
            subject = _to_slug(classes_in_file[0])
        else:
            subject = _to_slug(_file_stem(top_file))
        if subject and subject != prefix:
            return f"{prefix}-{subject}" if prefix else subject

    # Most frequent keyword; class names say more than method names.
    class_members = [m for m in naming_members if m.kind == "Class"]
    keywords = _extract_keywords(class_members) or _extract_keywords(naming_members)
    dir_share = max(
        (n for label, n in Counter(_dir_label(m.file_path) for m in naming_members).items()
         if label),
        default=0,
    ) / len(naming_members)
    if dir_share < 0.5 and len(keywords) >= 2:
        # A slice across layers (controller, service, repository of one
        # feature): no directory speaks for it, the shared vocabulary does.
        return "-".join(keywords[:2])
    prefix_words = set(prefix.split("-"))
    keyword = next((k for k in keywords if k not in prefix_words), "")

    if prefix and keyword:
        return f"{prefix}-{keyword}"
    if prefix:
        return prefix
    if keyword:
        return keyword
    return "cluster"


def _file_stem(file_path: str) -> str:
    """Return the file name without directories or extension."""
    return file_path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]


def _dir_label(file_path: str) -> str:
    """Return the slug of the nearest non-generic parent directory, or ``""``."""
    segments = [s for s in file_path.replace("\\", "/").split("/")[:-1] if s]
    for segment in reversed(segments):
        if segment.lower() not in _GENERIC_DIRS:
            slug = _to_slug(segment)
            if slug:
                return slug
    return ""


def _extract_file_prefix(file_paths: list[str]) -> str:
    """Find the dominant short directory (or, failing that, module) name."""
    if not file_paths:
        return ""
    labels = Counter(_dir_label(fp) for fp in file_paths)
    labels.pop("", None)
    if labels:
        return min(labels.items(), key=lambda item: (-item[1], item[0]))[0]
    stems = Counter(_to_slug(_file_stem(fp)) for fp in file_paths)
    stems.pop("", None)
    if stems:
        return min(stems.items(), key=lambda item: (-item[1], item[0]))[0]
    return ""


def _extract_keywords(members: list[GraphNode]) -> list[str]:
    """Extract the most frequent meaningful keywords from member names."""
    word_counts: Counter[str] = Counter()
    for m in members:
        if m.kind in ("Function", "Class", "Test", "Type"):
            words = _split_name(m.name)
            for w in words:
                wl = w.lower()
                if wl not in _COMMON_WORDS and len(wl) > 1:
                    word_counts[wl] += 1

    if not word_counts:
        return []
    return [w for w, _ in word_counts.most_common(5)]


def _split_name(name: str) -> list[str]:
    """Split a camelCase or snake_case name into words."""
    # Insert boundary before uppercase letters for camelCase
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    # Split on underscores, hyphens, dots
    return [p for p in re.split(r"[_\-.\s]+", s) if p]


def _to_slug(s: str) -> str:
    """Convert a string to a short lowercase slug at a word boundary."""
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", s)
    slug = "-".join(word.lower() for word in _split_name(normalized))
    if len(slug) <= _SLUG_MAX_LEN:
        return slug

    boundary = slug.rfind("-", 0, _SLUG_MAX_LEN + 1)
    if boundary > 0:
        return slug[:boundary]
    return slug[:_SLUG_MAX_LEN]


# ---------------------------------------------------------------------------
# Cohesion calculation
# ---------------------------------------------------------------------------


def _compute_cohesion_batch(
    community_member_qns: list[set[str]],
    all_edges: Sequence[GraphEdge | _CommunityEdge],
) -> list[float]:
    """Compute cohesion for multiple communities in a single O(edges) pass.

    Builds a ``qualified_name -> community_index`` reverse map (each node
    appears in at most one community since all callers produce partitions),
    then walks every edge exactly once, bucketing it into internal/external
    counters per community.

    Total work: O(edges + sum(|members|)) instead of
    O(edges * communities) for naive per-community cohesion.

    Returns a list of cohesion scores aligned with ``community_member_qns``.
    """
    qn_to_idx: dict[str, int] = {}
    for idx, members in enumerate(community_member_qns):
        for qn in members:
            qn_to_idx[qn] = idx

    n = len(community_member_qns)
    internal = [0] * n
    external = [0] * n

    for e in all_edges:
        sc = qn_to_idx.get(e.source_qualified)
        tc = qn_to_idx.get(e.target_qualified)
        if sc is None and tc is None:
            continue
        if sc == tc:
            # Safe: sc is not None here (sc == tc and not both None).
            assert sc is not None
            internal[sc] += 1
        else:
            if sc is not None:
                external[sc] += 1
            if tc is not None:
                external[tc] += 1

    results: list[float] = []
    for i in range(n):
        total = internal[i] + external[i]
        results.append(internal[i] / total if total > 0 else 0.0)
    return results


def _build_adjacency(edges: Sequence[GraphEdge | _CommunityEdge]) -> dict[str, list[str]]:
    """Build adjacency list from edges (one pass over all edges)."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e.source_qualified].append(e.target_qualified)
        adj[e.target_qualified].append(e.source_qualified)
    return adj


def _compute_cohesion(
    member_qns: set[str],
    all_edges: Sequence[GraphEdge | _CommunityEdge],
    adj: dict[str, list[str]] | None = None,
) -> float:
    """Compute cohesion: internal_edges / (internal_edges + external_edges).

    For multiple communities, prefer :func:`_compute_cohesion_batch`, which
    runs in O(edges) total instead of O(edges) per community.
    """
    return _compute_cohesion_batch([member_qns], all_edges)[0]


# ---------------------------------------------------------------------------
# Leiden-based community detection (igraph)
# ---------------------------------------------------------------------------


def _reassign_test_nodes(
    clusters: list[list[int]],
    idx_to_node: dict[int, GraphNode],
    qn_to_idx: dict[str, int],
    edges: Sequence[GraphEdge | _CommunityEdge],
    resolution_nodes: list[GraphNode] | None = None,
) -> list[list[int]]:
    """Move tests to the community containing most unique tested subjects.

    Edge direction is ignored, ambiguous bare names are skipped, ties retain
    the current cluster, and rebuilding from the original partition keeps the
    result deterministic without repeated linear-time list removals.
    """
    vertex_to_cluster = {
        vertex: cluster_id
        for cluster_id, cluster in enumerate(clusters)
        for vertex in cluster
    }

    names_to_qns: dict[str, str | None] = {}
    nodes_for_resolution = resolution_nodes or list(idx_to_node.values())
    for node in nodes_for_resolution:
        names_to_qns[node.name] = (
            None
            if node.name in names_to_qns
            else node.qualified_name
        )

    def _resolve(endpoint: str) -> int | None:
        exact = qn_to_idx.get(endpoint)
        if exact is not None:
            return exact
        qualified_name = names_to_qns.get(endpoint)
        if qualified_name is None:
            return None
        return qn_to_idx.get(qualified_name)

    subjects_by_test: dict[int, set[int]] = defaultdict(set)
    for edge in edges:
        if edge.kind != "TESTED_BY":
            continue
        source = _resolve(edge.source_qualified)
        target = _resolve(edge.target_qualified)
        if source is None or target is None:
            continue

        source_is_test = _is_test_node(idx_to_node[source])
        target_is_test = _is_test_node(idx_to_node[target])
        if source_is_test == target_is_test:
            continue

        test_index, subject_index = (
            (source, target) if source_is_test else (target, source)
        )
        subjects_by_test[test_index].add(subject_index)

    target_by_test: dict[int, int] = {}
    for test_index, subject_indices in subjects_by_test.items():
        current_cluster = vertex_to_cluster.get(test_index)
        if current_cluster is None:
            continue

        votes = Counter(
            vertex_to_cluster[subject_index]
            for subject_index in subject_indices
            if subject_index in vertex_to_cluster
        )
        if not votes:
            continue

        highest_vote = max(votes.values())
        tied_clusters = sorted(
            cluster_id
            for cluster_id, vote_count in votes.items()
            if vote_count == highest_vote
        )
        target_cluster = (
            current_cluster
            if current_cluster in tied_clusters
            else tied_clusters[0]
        )
        target_by_test[test_index] = target_cluster

    reassigned: list[list[int]] = [[] for _ in clusters]
    for current_cluster, cluster in enumerate(clusters):
        for vertex in cluster:
            target_cluster = target_by_test.get(vertex, current_cluster)
            reassigned[target_cluster].append(vertex)

    return reassigned


def _load_igraph() -> bool:
    """Import igraph lazily; a failed import disables the package for good.

    ``find_spec("igraph")`` only proves the package directory exists. The
    import itself can still fail when a native dependency is missing, so the
    flag is cleared here and every caller falls back to file-based grouping
    instead of returning an empty community set.
    """
    global ig, IGRAPH_AVAILABLE
    if not IGRAPH_AVAILABLE:
        return False
    if ig is not None:
        return True
    try:
        import igraph as _ig  # type: ignore[import-untyped]
        # A native build can import and still fail on first attribute access,
        # so touch the type we need while the handlers are still in scope.
        _ig.Graph
    except (ImportError, OSError, AttributeError) as exc:
        # A missing native library surfaces as OSError, not ImportError.
        logger.warning("igraph import failed (%s); using file-based communities", exc)
        IGRAPH_AVAILABLE = False
        return False
    ig = _ig
    return True


def _containment_roots(
    nodes: Sequence[GraphNode],
    qn_to_idx: dict[str, int],
    edges: Sequence[GraphEdge | _CommunityEdge],
) -> list[int]:
    """Map every node index to the index of the node that owns it.

    A class owns the methods it CONTAINS and a method owns the endpoint it
    HANDLES, transitively. Nodes with no owner are their own root. Leiden runs
    on one vertex per owner, so an owner and its members cannot be split, and
    the result does not depend on edge weights.
    """
    parent = list(range(len(nodes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in edges:
        if e.kind not in _PIN_EDGE_KINDS:
            continue
        owner = qn_to_idx.get(e.source_qualified)
        member = qn_to_idx.get(e.target_qualified)
        if owner is None or member is None or owner == member:
            continue
        owner_root, member_root = find(owner), find(member)
        if owner_root != member_root:
            # The owner's root wins, so a class stays the root of its group.
            parent[member_root] = owner_root
    return [find(i) for i in range(len(nodes))]


def _leiden_clusters(
    node_count: int,
    roots: list[int],
    qn_to_idx: dict[str, int],
    edges: Sequence[GraphEdge | _CommunityEdge],
    *,
    resolution: float,
    n_iterations: int | None = None,
) -> list[list[int]] | None:
    """Run Leiden on the graph with every owner and its members as one vertex.

    Returns clusters of node indices, or ``None`` when no edge joins two
    different nodes (the caller falls back to file grouping).
    """
    vertex_of_root: dict[int, int] = {}
    vertex_of: list[int] = []
    for i in range(node_count):
        vertex_of.append(vertex_of_root.setdefault(roots[i], len(vertex_of_root)))

    # One weight per node pair (the strongest edge kind between them), so
    # duplicate call sites cannot bias the result; pairs between two groups
    # then add up.
    best: dict[tuple[int, int], float] = {}
    for e in edges:
        src = qn_to_idx.get(e.source_qualified)
        tgt = qn_to_idx.get(e.target_qualified)
        if src is None or tgt is None or src == tgt:
            continue
        pair = (min(src, tgt), max(src, tgt))
        weight = EDGE_WEIGHTS.get(e.kind, 0.5)
        if weight > best.get(pair, 0.0):
            best[pair] = weight
    if not best:
        return None

    group_weights: dict[tuple[int, int], float] = {}
    strength = [0.0] * len(vertex_of_root)
    for (src, tgt), weight in best.items():
        a, b = vertex_of[src], vertex_of[tgt]
        # An edge inside a group is a self-loop. igraph's Leiden ignores
        # loops, but modularity needs the degree they add, so the vertex
        # strengths are kept and passed as node weights below.
        strength[a] += weight
        strength[b] += weight
        if a != b:
            key = (min(a, b), max(a, b))
            group_weights[key] = group_weights.get(key, 0.0) + weight

    group_edges = sorted(group_weights)
    g = ig.Graph(n=len(vertex_of_root), edges=group_edges, directed=False)
    g.es["weight"] = [group_weights[pair] for pair in group_edges]

    logger.info("Running Leiden on %d vertices, %d edges...", g.vcount(), g.ecount())
    seed = env_int("CRG_LEIDEN_SEED", _LEIDEN_SEED)
    # Deterministic seeding for benchmark reproducibility — community
    # detection is not a security-sensitive context. nosec B311.
    ig.set_random_number_generator(random.Random(seed))  # nosec B311
    kwargs: dict[str, Any] = {}
    if n_iterations is not None:
        kwargs["n_iterations"] = n_iterations
    # Modularity written as CPM: node weights = strength, resolution / 2m.
    partition = g.community_leiden(
        objective_function="CPM",
        weights="weight",
        node_weights=strength,
        resolution=resolution / sum(strength),
        **kwargs,
    )

    by_cluster: dict[int, list[int]] = {}
    for i in range(node_count):
        by_cluster.setdefault(partition.membership[vertex_of[i]], []).append(i)
    return [by_cluster[cid] for cid in sorted(by_cluster)]


def _repin_members(clusters: list[list[int]], roots: list[int]) -> list[list[int]]:
    """Put every member in the cluster of its owner.

    Test reassignment moves single nodes between clusters; a test method
    reassigned away from its test class would undo the grouping, so each
    group follows its root.
    """
    cluster_of_root: dict[int, int] = {}
    for cid, cluster in enumerate(clusters):
        for vertex in cluster:
            if roots[vertex] == vertex:
                cluster_of_root[vertex] = cid
    repinned: list[list[int]] = [[] for _ in clusters]
    for cid, cluster in enumerate(clusters):
        for vertex in cluster:
            repinned[cluster_of_root.get(roots[vertex], cid)].append(vertex)
    return [sorted(cluster) for cluster in repinned]


def _detect_leiden(
    nodes: list[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
    min_size: int,
    adj: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Detect communities using Leiden algorithm via igraph.

    Caps Leiden at ``n_iterations=2`` (sufficient for code dependency graphs)
    and skips the recursive sub-community splitting pass that caused
    exponential blow-up on large repos (>100k nodes).
    """
    if not _load_igraph():
        return []

    qn_to_idx: dict[str, int] = {}
    idx_to_node: dict[int, GraphNode] = {}
    for i, node in enumerate(nodes):
        qn_to_idx[node.qualified_name] = i
        idx_to_node[i] = node

    if not qn_to_idx:
        return []

    logger.info("Building igraph with %d nodes...", len(qn_to_idx))

    # Run Leiden -- scale resolution inversely with graph size to get
    # coarser clusters on large repos.  Default resolution=1.0 produces
    # thousands of tiny communities for 30k+ node graphs.
    import math
    resolution = max(0.05, 1.0 / math.log10(max(len(nodes), 10)))

    roots = _containment_roots(nodes, qn_to_idx, edges)
    clusters = _leiden_clusters(
        len(nodes), roots, qn_to_idx, edges, resolution=resolution, n_iterations=2,
    )
    if clusters is None:
        fallback = _detect_file_based(nodes, edges, min_size, adj=adj)
        for comm in fallback:
            comm["detector"] = DETECTOR_FILE
        return fallback

    logger.info(
        "Leiden complete, found %d partitions. Computing cohesion...",
        len(clusters),
    )

    clusters = _reassign_test_nodes(clusters, idx_to_node, qn_to_idx, edges)
    clusters = _repin_members(clusters, roots)

    pending: list[tuple[list[GraphNode], set[str]]] = []
    for cluster_ids in clusters:
        if len(cluster_ids) < min_size:
            continue
        members = [idx_to_node[i] for i in cluster_ids if i in idx_to_node]
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        pending.append((members, member_qns))

    cohesions = _compute_cohesion_batch([p[1] for p in pending], edges)

    communities: list[dict[str, Any]] = []
    for (members, member_qns), cohesion in zip(pending, cohesions):
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        communities.append({
            "name": name,
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"Community of {len(members)} nodes",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    logger.info("Community detection complete: %d communities", len(communities))
    return communities


# ---------------------------------------------------------------------------
# File-based fallback community detection
# ---------------------------------------------------------------------------


def _detect_file_based(
    nodes: list[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
    min_size: int,
    adj: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Group nodes by directory when Leiden is unavailable or over-fragments.

    Strips the longest common directory prefix from all file paths, then
    adaptively picks a grouping depth that yields 10-200 communities.
    """
    # Collect all directory paths (normalized, without filename)
    all_dir_parts: list[list[str]] = []
    for n in nodes:
        parts = n.file_path.replace("\\", "/").split("/")
        all_dir_parts.append([p for p in parts[:-1] if p])

    # Find the longest common prefix among directory parts
    prefix_len = 0
    if all_dir_parts:
        shortest = min(len(p) for p in all_dir_parts)
        for i in range(shortest):
            seg = all_dir_parts[0][i]
            if all(p[i] == seg for p in all_dir_parts):
                prefix_len = i + 1
            else:
                break

    def _group_at_depth(depth: int) -> dict[str, list[GraphNode]]:
        groups: dict[str, list[GraphNode]] = defaultdict(list)
        for n in nodes:
            parts = n.file_path.replace("\\", "/").split("/")
            dir_parts = [p for p in parts[:-1] if p]
            remainder = dir_parts[prefix_len:]
            if remainder:
                key = "/".join(remainder[:depth])
            else:
                key = parts[-1].rsplit(".", 1)[0] if parts else "root"
            groups[key].append(n)
        return groups

    # Try increasing depths until we get 10-200 qualifying groups
    max_depth = max((len(p) - prefix_len for p in all_dir_parts), default=0)
    best_groups = _group_at_depth(1)  # depth=1 always works (file stem fallback)
    for depth in range(1, max_depth + 1):
        groups = _group_at_depth(depth)
        qualifying = sum(1 for v in groups.values() if len(v) >= min_size)
        best_groups = groups
        if qualifying >= 10:
            break

    by_dir = best_groups

    # Pre-filter to communities meeting min_size and collect their member
    # sets so we can batch-compute all cohesions in a single O(edges) pass.
    # Without this, per-community cohesion is O(edges * files), which makes
    # community detection effectively hang on large repos.
    pending: list[tuple[str, list[GraphNode], set[str]]] = []
    for dir_path, members in by_dir.items():
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        pending.append((dir_path, members, member_qns))

    cohesions = _compute_cohesion_batch([p[2] for p in pending], edges)

    communities: list[dict[str, Any]] = []
    for (dir_path, members, member_qns), cohesion in zip(pending, cohesions):
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        communities.append({
            "name": name,
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"Directory-based community: {dir_path}",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    return communities


# ---------------------------------------------------------------------------
# Oversized community splitting
# ---------------------------------------------------------------------------


def _split_oversized(
    communities: list[dict],
    nodes: list[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
    threshold_pct: float = 0.25,
    min_split_size: int = 10,
) -> list[dict]:
    """Recursively split communities that exceed threshold_pct of total.

    Uses Leiden on the subgraph of oversized communities. If igraph is
    not available, returns communities unchanged.
    """
    if not _load_igraph():
        return communities

    total = sum(
        c.get("size", len(c.get("members", [])))
        for c in communities
    )
    if total == 0:
        return communities

    threshold = max(int(total * threshold_pct), min_split_size)
    result: list[dict] = []
    next_id = max(
        (c.get("id", 0) for c in communities), default=0
    ) + 1

    for comm in communities:
        members = set(comm.get("members", []))
        if len(members) <= threshold:
            result.append(comm)
            continue

        # Build subgraph for this community
        member_nodes = [
            n for n in nodes
            if n.qualified_name in members
        ]
        member_edges = [
            e for e in edges
            if (
                e.source_qualified in members
                and e.target_qualified in members
            )
        ]

        if len(member_nodes) < min_split_size:
            result.append(comm)
            continue

        # Run Leiden on subgraph
        qn_to_idx = {
            n.qualified_name: i
            for i, n in enumerate(member_nodes)
        }
        idx_to_node = {i: node for i, node in enumerate(member_nodes)}

        try:
            # GraphStore preserves one edge per call site; _leiden_clusters
            # keeps one weight per vertex pair, and keeps a class with its
            # methods, so a split cannot separate them.
            roots = _containment_roots(member_nodes, qn_to_idx, member_edges)
            partition_clusters = _leiden_clusters(
                len(member_nodes), roots, qn_to_idx, member_edges, resolution=0.5,
            )
            if partition_clusters is None:
                result.append(comm)
                continue

            reassigned_clusters = _reassign_test_nodes(
                partition_clusters,
                idx_to_node,
                qn_to_idx,
                edges,
                resolution_nodes=nodes,
            )
            reassigned_clusters = _repin_members(reassigned_clusters, roots)
            sub_communities = [
                [idx_to_node[idx] for idx in cluster]
                for cluster in reassigned_clusters
                if cluster
            ]

            if len(sub_communities) <= 1:
                result.append(comm)
                continue

            parent_id = comm.get("id", 0)
            comm_name = comm.get("name", "")
            sub_member_qns = [
                {node.qualified_name for node in sub_nodes}
                for sub_nodes in sub_communities
            ]
            cohesions = _compute_cohesion_batch(sub_member_qns, edges)

            for sub_nodes, member_qns, cohesion in zip(
                sub_communities, sub_member_qns, cohesions
            ):
                generated_name = _generate_community_name(sub_nodes)
                if generated_name in {"", "empty", "cluster"}:
                    generated_name = f"{comm_name}-{next_id}"
                sub_comm = {
                    "id": next_id,
                    "name": generated_name,
                    "level": comm.get("level", 0) + 1,
                    "parent_id": parent_id,
                    "members": [node.qualified_name for node in sub_nodes],
                    "size": len(member_qns),
                    "cohesion": cohesion,
                    "dominant_language": comm.get(
                        "dominant_language"
                    ),
                    "description": (
                        f"Split from {comm_name}"
                    ),
                }
                result.append(sub_comm)
                next_id += 1

            logger.info(
                "Split oversized community '%s' "
                "(%d members) into %d",
                comm_name,
                len(members),
                len(sub_communities),
            )
        except Exception:
            logger.warning(
                "Failed to split community '%s', "
                "keeping as-is",
                comm.get("name", ""),
                exc_info=True,
            )
            result.append(comm)

    return result


def _dedupe_community_names(
    communities: list[dict[str, Any]],
    nodes: list[GraphNode],
) -> None:
    """Disambiguate exact duplicate names while keeping the largest unchanged."""
    communities_by_name: dict[str, list[tuple[int, dict[str, Any]]]] = (
        defaultdict(list)
    )
    for position, community in enumerate(communities):
        communities_by_name[community.get("name", "")].append(
            (position, community)
        )

    nodes_by_qn = {node.qualified_name: node for node in nodes}
    taken_names = {
        community.get("name", "")
        for community in communities
        if community.get("name", "")
    }

    for base_name, duplicates in communities_by_name.items():
        if not base_name or len(duplicates) <= 1:
            continue

        ordered = sorted(
            duplicates,
            key=lambda item: (
                -item[1].get("size", len(item[1].get("members", []))),
                item[1].get("id", item[0]),
                item[0],
            ),
        )
        base_words = set(base_name.split("-"))

        for _, community in ordered[1:]:
            member_nodes = [
                nodes_by_qn[qualified_name]
                for qualified_name in community.get("members", [])
                if qualified_name in nodes_by_qn
            ]
            candidate_name = ""
            for keyword in _extract_keywords(_naming_members(member_nodes)):
                suffix = _to_slug(keyword)
                if not suffix or suffix in base_words:
                    continue
                candidate = f"{base_name}-{suffix}"
                if candidate not in taken_names:
                    candidate_name = candidate
                    break

            if not candidate_name:
                suffix_number = 2
                candidate_name = f"{base_name}-{suffix_number}"
                while candidate_name in taken_names:
                    suffix_number += 1
                    candidate_name = f"{base_name}-{suffix_number}"

            community["name"] = candidate_name
            taken_names.add(candidate_name)


# ---------------------------------------------------------------------------
# Config documents
# ---------------------------------------------------------------------------


def _group_config_documents(
    nodes: list[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
    min_size: int,
) -> list[dict[str, Any]]:
    """Group JSON/YAML document nodes into one community per directory and type.

    The same document is usually checked in once per environment (``taskdef-dev``,
    ``taskdef-qa``, ...). Leiden gives each copy a community of its own, and the
    copies are identical, so they are kept out of Leiden. Files that sit in the
    same directory and are the same type are one community instead, which is also
    how people look for them.
    """
    groups: dict[tuple[str, str], list[GraphNode]] = defaultdict(list)
    for node in nodes:
        path = node.file_path.replace("\\", "/")
        groups[(path.rsplit("/", 1)[0] if "/" in path else "", node.kind)].append(node)

    pending: list[tuple[tuple[str, str], list[GraphNode]]] = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) >= min_size:
            pending.append((key, members))

    cohesions = _compute_cohesion_batch(
        [{m.qualified_name for m in members} for _, members in pending], edges,
    )
    communities: list[dict[str, Any]] = []
    for ((directory, kind), members), cohesion in zip(pending, cohesions):
        label = _dir_label(directory + "/x") or "config"
        suffix = "json" if kind == "JsonPath" else "yaml"
        communities.append({
            "name": f"{label}-{suffix}",
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": _dominant_language(members),
            "description": "",
            "members": [m.qualified_name for m in members],
            "config_document": True,
        })
    return communities


def _dominant_language(members: Sequence[GraphNode]) -> str:
    lang_counts = Counter(m.language for m in members if m.language)
    if not lang_counts:
        return ""
    return min(lang_counts.items(), key=lambda item: (-item[1], item[0]))[0]


# ---------------------------------------------------------------------------
# Thin community merging
# ---------------------------------------------------------------------------


def _merge_thin_communities(
    communities: list[dict[str, Any]],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
    min_members: int = _MIN_COMMUNITY_MEMBERS,
) -> list[dict[str, Any]]:
    """Fold communities with fewer than *min_members* nodes into a neighbour.

    The target is the community the thin one shares the most edge weight with.
    With no edges to another community it is the one holding the most nodes of
    the same file, then of the same directory. A thin community with no such
    neighbour stays as it is; no community is ever created. Config-document
    communities only merge with each other. Communities that absorb another
    are marked ``"merged": True`` so the caller can rename them and refresh
    their cohesion.
    """
    thin = [i for i, c in enumerate(communities) if len(c["members"]) < min_members]
    if not thin or len(communities) < 2:
        return communities

    node_by_qn = {n.qualified_name: n for n in nodes}
    owner: dict[str, int] = {}
    for i, comm in enumerate(communities):
        for qn in comm["members"]:
            owner[qn] = i
    is_config = [bool(c.get("config_document")) for c in communities]
    members: list[list[str]] = [list(c["members"]) for c in communities]
    redirect: dict[int, int] = {}

    def resolve(i: int) -> int:
        while i in redirect:
            i = redirect[i]
        return i

    thin_qns = {qn for i in thin for qn in members[i]}
    neighbours: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        weight = EDGE_WEIGHTS.get(e.kind, 0.5)
        if e.source_qualified in thin_qns:
            neighbours[e.source_qualified].append((e.target_qualified, weight))
        if e.target_qualified in thin_qns:
            neighbours[e.target_qualified].append((e.source_qualified, weight))

    by_file: dict[str, Counter[int]] = defaultdict(Counter)
    by_dir: dict[str, Counter[int]] = defaultdict(Counter)
    for qn, i in owner.items():
        node = node_by_qn.get(qn)
        if node is None:
            continue
        path = node.file_path.replace("\\", "/")
        by_file[path][i] += 1
        by_dir[path.rsplit("/", 1)[0] if "/" in path else ""][i] += 1

    def best_target(i: int) -> int | None:
        def usable(j: int) -> bool:
            return j != i and is_config[j] == is_config[i]

        scores: dict[int, float] = defaultdict(float)
        for qn in members[i]:
            for other, weight in neighbours.get(qn, ()):
                j = owner.get(other)
                if j is not None:
                    j = resolve(j)
                    if usable(j):
                        scores[j] += weight
        if not scores:
            for index in (by_file, by_dir):
                counts: Counter[int] = Counter()
                for qn in members[i]:
                    node = node_by_qn.get(qn)
                    if node is None:
                        continue
                    path = node.file_path.replace("\\", "/")
                    key = path if index is by_file else (
                        path.rsplit("/", 1)[0] if "/" in path else ""
                    )
                    for j, count in index[key].items():
                        j = resolve(j)
                        if usable(j):
                            counts[j] += count
                if counts:
                    scores = {j: float(c) for j, c in counts.items()}
                    break
        if not scores:
            return None
        return min(scores, key=lambda j: (-scores[j], -len(members[j]), j))

    for i in sorted(thin, key=lambda i: (len(communities[i]["members"]), i)):
        if i in redirect:
            continue
        target = best_target(i)
        if target is None:
            continue
        members[target].extend(members[i])
        redirect[i] = target

    if not redirect:
        return communities

    merged: list[dict[str, Any]] = []
    for i, comm in enumerate(communities):
        if i in redirect:
            continue
        if len(members[i]) != len(comm["members"]):
            comm = dict(comm)
            comm["members"] = members[i]
            comm["size"] = len(members[i])
            comm["merged"] = True
        merged.append(comm)
    logger.info("Merged %d thin communities into neighbours", len(redirect))
    return merged


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


def _describe_community(
    member_nodes: Sequence[GraphNode],
    called_targets: set[str],
) -> str:
    """Summarise a community: size and node-kind mix, top files, entry points."""
    kinds = Counter(n.kind for n in member_nodes)
    mix = ", ".join(
        f"{count} {kind}"
        for kind, count in sorted(kinds.items(), key=lambda item: (-item[1], item[0]))[:4]
    )
    files = Counter(
        "/".join(n.file_path.replace("\\", "/").split("/")[-2:]) for n in member_nodes
    )
    top_files = ", ".join(
        f"{path} ({count})"
        for path, count in sorted(files.items(), key=lambda item: (-item[1], item[0]))[:3]
    )
    entry_points = sum(
        1 for n in member_nodes
        if n.kind == "Function" and not _is_test_node(n)
        and n.qualified_name not in called_targets
    )
    parts = [f"{len(member_nodes)} nodes ({mix})", f"top files: {top_files}"]
    endpoints = kinds.get("Endpoint", 0)
    if endpoints or entry_points:
        parts.append(f"{endpoints} endpoints, {entry_points} entry points")
    return _sanitize_name("; ".join(parts))


def _finalize_communities(
    communities: list[dict[str, Any]],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge | _CommunityEdge],
) -> None:
    """Refresh merged communities and give every community a real description."""
    node_by_qn = {n.qualified_name: n for n in nodes}

    merged = [c for c in communities if c.get("merged")]
    if merged:
        cohesions = _compute_cohesion_batch([set(c["members"]) for c in merged], edges)
        for comm, cohesion in zip(merged, cohesions):
            member_nodes = [node_by_qn[q] for q in comm["members"] if q in node_by_qn]
            comm["cohesion"] = round(cohesion, 4)
            comm["dominant_language"] = _dominant_language(member_nodes)
            if not comm.get("config_document"):
                generated = _generate_community_name(member_nodes)
                if generated not in {"", "empty", "cluster"}:
                    comm["name"] = generated

    called_targets = {e.target_qualified for e in edges if e.kind == "CALLS"}
    for comm in communities:
        member_nodes = [node_by_qn[q] for q in comm["members"] if q in node_by_qn]
        details = _describe_community(member_nodes, called_targets)
        previous = comm.get("description", "")
        # The file-based detector names its directory; keep that as the lead.
        comm["description"] = (
            f"{previous}; {details}" if previous.startswith("Directory-based community")
            else details
        )
        comm.pop("merged", None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_communities(
    store: GraphStore, min_size: int = 2
) -> list[dict[str, Any]]:
    """Detect communities in the code graph.

    Uses the Leiden algorithm via igraph if available, otherwise falls back to
    file-based grouping.

    Args:
        store: The GraphStore instance.
        min_size: Minimum number of nodes for a community to be included.

    Returns:
        List of community dicts with keys: name, level, size, cohesion,
        dominant_language, description, members, member_qns.
    """
    # Detection reads only kind and endpoints, so skip building a GraphEdge
    # (and decoding its JSON extra) for every edge in the graph.
    all_edges: Sequence[GraphEdge | _CommunityEdge] = [
        _CommunityEdge(row[0], row[1], row[2])
        for row in store._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges"
        ).fetchall()
    ]
    # Gather all nodes (exclude File nodes to focus on code entities)
    unique_nodes = store.get_all_nodes(exclude_files=True)

    logger.debug(
        "Loaded %d unique nodes, %d edges",
        len(unique_nodes), len(all_edges),
    )

    # JSON/YAML document rows are grouped by directory, not clustered: one
    # copy of a config file per environment would be one clone community each.
    config_nodes = [n for n in unique_nodes if n.kind in CONFIG_DOCUMENT_KINDS]
    code_nodes = [n for n in unique_nodes if n.kind not in CONFIG_DOCUMENT_KINDS]

    if _load_igraph():
        logger.info("Detecting communities with Leiden algorithm (igraph)")
        results = _detect_leiden(code_nodes, all_edges, min_size)
        detector = DETECTOR_LEIDEN
    else:
        logger.info(
            'Tip: pip install "code-review-graph[communities]" for finer communities '
            "(grouping by file for now)."
        )
        results = _detect_file_based(code_nodes, all_edges, min_size)
        detector = DETECTOR_FILE

    # Split oversized communities
    results = _split_oversized(
        results, code_nodes, all_edges,
    )
    results.extend(_group_config_documents(config_nodes, all_edges, min_size))
    results = _merge_thin_communities(results, unique_nodes, all_edges)
    _finalize_communities(results, unique_nodes, all_edges)
    for comm in results:
        comm.pop("config_document", None)
    _dedupe_community_names(results, unique_nodes)
    # Which algorithm made each community, for store_communities to record.
    # Leiden falls back to file grouping on a graph with no usable edges and
    # tags those itself; the parts of a split community are Leiden's.
    for comm in results:
        comm.setdefault("detector", detector)

    # Convert member_qns (internal set) to a list for serialization safety,
    # then strip it from the returned dicts to avoid leaking internal state.
    for comm in results:
        if "member_qns" in comm:
            comm["member_qns"] = list(comm["member_qns"])
            del comm["member_qns"]

    return results


def capture_community_assignments(
    store: GraphStore,
    file_paths: list[str],
) -> dict[str, int]:
    """Capture ``qualified_name -> community_id`` for nodes in *file_paths*.

    Must run **before** node replacement so assignments can be remapped onto
    the new node rows after IDs churn (#569).
    """
    if not file_paths:
        return {}

    conn = store._conn
    mapping: dict[str, int] = {}
    for i in range(0, len(file_paths), _SQL_BATCH):
        batch = file_paths[i:i + _SQL_BATCH]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT qualified_name, community_id FROM nodes "  # nosec B608
            f"WHERE community_id IS NOT NULL AND file_path IN ({placeholders})",
            batch,
        ).fetchall()
        for qn, cid in rows:
            if qn and cid is not None:
                mapping[qn] = int(cid)
    return mapping


def remap_community_assignments(
    store: GraphStore,
    community_by_qn: dict[str, int],
) -> int:
    """Re-apply captured community IDs onto current nodes by qualified name.

    Returns the number of nodes updated.
    """
    if not community_by_qn:
        return 0

    conn = store._conn
    updated = 0
    items = list(community_by_qn.items())
    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for qn, cid in items:
            cur = conn.execute(
                "UPDATE nodes SET community_id = ? WHERE qualified_name = ?",
                (cid, qn),
            )
            if cur.rowcount:
                updated += cur.rowcount
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return updated


def purge_empty_communities(store: GraphStore) -> int:
    """Delete community rows that no longer have any assigned nodes.

    Also refreshes ``size`` for communities that still have members so
    incremental deletion stays consistent with a fresh full rebuild.
    Returns the number of empty community rows removed.
    """
    conn = store._conn
    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Refresh sizes from live node assignments.
        conn.execute(
            "UPDATE communities SET size = ("
            "SELECT COUNT(*) FROM nodes "
            "WHERE nodes.community_id = communities.id)"
        )
        cur = conn.execute(
            "DELETE FROM communities WHERE id NOT IN ("
            "SELECT DISTINCT community_id FROM nodes "
            "WHERE community_id IS NOT NULL)"
        )
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted


def incremental_detect_communities(
    store: GraphStore,
    changed_files: list[str],
    min_size: int = 2,
    repo_root: str | Path | None = None,
    force: bool = False,
) -> int:
    """Re-detect communities only if changed files affect existing communities.

    If no existing communities contain nodes from changed files, skips
    re-detection unless changed files contain unassigned code nodes. That
    happens for a file new to the graph, which was in no community to be
    affected, and for a re-parsed file whose nodes were inserted without
    their old assignment. In that case, re-run full community detection to
    assign the new nodes, as a clean build would.

    Args:
        store: The GraphStore instance.
        changed_files: List of file paths that have changed.
        min_size: Minimum number of nodes for a community to be included.
        repo_root: Optional repo root used to expand relative/absolute paths.
        force: When True, always re-run detection (used after pre-replacement
            capture showed communities were affected even if path matching
            would otherwise miss them).

    Returns:
        Number of communities detected, or 0 if skipped.
    """
    if not changed_files and not force:
        return 0

    from .flows import expand_changed_file_paths

    expanded = expand_changed_file_paths(store, changed_files, repo_root=repo_root)
    conn = store._conn

    # Check if any communities are affected (batch to stay under SQLite limit)
    affected_count = 0
    unassigned_count = 0
    for i in range(0, len(expanded), _SQL_BATCH):
        batch = expanded[i:i + _SQL_BATCH]
        placeholders = ",".join("?" * len(batch))
        row = conn.execute(
            f"SELECT COUNT(DISTINCT community_id), "  # nosec B608
            f"SUM(CASE WHEN community_id IS NULL AND kind != 'File' "
            f"THEN 1 ELSE 0 END) FROM nodes "
            f"WHERE file_path IN ({placeholders})",
            batch,
        ).fetchone()
        if row:
            affected_count += row[0] or 0
            unassigned_count += row[1] or 0

    if not force and affected_count == 0 and unassigned_count == 0:
        return 0  # No communities affected, skip

    # Re-run full community detection (correct and fast enough)
    communities = detect_communities(store, min_size=min_size)
    return store_communities(store, communities)


def store_communities(
    store: GraphStore, communities: list[dict[str, Any]]
) -> int:
    """Store detected communities in the database.

    Clears existing communities and community_id assignments, then inserts
    the new communities and updates node community_id references.

    Args:
        store: The GraphStore instance.
        communities: List of community dicts from detect_communities().

    Returns:
        Number of communities stored.
    """
    # NOTE: store_communities uses _conn directly because it performs
    # multi-statement batch writes (DELETE + INSERT loop + UPDATE loop)
    # that are tightly coupled to the DB transaction lifecycle.
    conn = store._conn

    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    # Wrap in explicit transaction so the DELETE + INSERT + UPDATE
    # sequence is atomic — no partial community data on crash.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM communities")
        conn.execute("UPDATE nodes SET community_id = NULL")

        count = 0
        for comm in communities:
            cursor = conn.execute(
                """INSERT INTO communities
                   (name, level, cohesion, size, dominant_language, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    comm["name"],
                    comm.get("level", 0),
                    comm.get("cohesion", 0.0),
                    comm["size"],
                    comm.get("dominant_language", ""),
                    comm.get("description", ""),
                ),
            )
            community_id = cursor.lastrowid

            # Batch update community_id on member nodes
            member_qns = comm.get("members", [])
            for j in range(0, len(member_qns), _SQL_BATCH):
                batch = member_qns[j:j + _SQL_BATCH]
                placeholders = ",".join("?" * len(batch))
                conn.execute(
                    f"UPDATE nodes SET community_id = ? WHERE qualified_name IN ({placeholders})",  # nosec B608
                    [community_id] + batch,
                )
            count += 1

        # Recorded in the same transaction as the communities it describes,
        # so the two cannot disagree. Communities from a caller that does not
        # say how they were made leave no record rather than a guessed one.
        tags = [comm.get("detector") for comm in communities]
        if tags and all(tags):
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (DETECTOR_METADATA_KEY, "+".join(sorted({str(t) for t in tags}))),
            )
        else:
            conn.execute(
                "DELETE FROM metadata WHERE key = ?", (DETECTOR_METADATA_KEY,),
            )

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count


def get_communities(
    store: GraphStore,
    sort_by: str = "size",
    min_size: int = 0,
    members_by_community: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve stored communities from the database.

    Args:
        store: The GraphStore instance.
        sort_by: Column to sort by ("size", "cohesion", "name").
        min_size: Minimum community size to include.
        members_by_community: Members to report for each community id, for a
            caller that only wants some of them. Saves one query per community.

    Returns:
        List of community dicts.
    """
    valid_sorts = {"size", "cohesion", "name"}
    if sort_by not in valid_sorts:
        sort_by = "size"

    order = "DESC" if sort_by in ("size", "cohesion") else "ASC"

    # NOTE: get_communities reads the communities table which has no
    # dedicated GraphStore method (it's a domain-specific table managed
    # entirely by the communities module).  We use _conn for this query.
    rows = store._conn.execute(
        f"SELECT * FROM communities WHERE size >= ? ORDER BY {sort_by} {order}",  # nosec B608
        (min_size,),
    ).fetchall()

    communities: list[dict[str, Any]] = []
    for row in rows:
        # Fetch member qualified names for this community
        member_qns = (
            [
                _sanitize_name(qn)
                for qn in store.get_community_member_qns(row["id"])
            ]
            if members_by_community is None
            else members_by_community.get(row["id"], [])
        )

        communities.append({
            "id": row["id"],
            "name": _sanitize_name(row["name"]),
            "level": row["level"],
            "cohesion": row["cohesion"],
            "size": row["size"],
            "dominant_language": row["dominant_language"] or "",
            "description": _sanitize_name(row["description"] or ""),
            "members": member_qns,
        })

    return communities


_TEST_COMMUNITY_RE = re.compile(
    r"(^test[-/]|[-/]test([:/]|$)|it:should|describe:|spec[-/]|[-/]spec$)",
    re.IGNORECASE,
)


def _is_test_community(name: str) -> bool:
    """Return True if a community name indicates it is test-dominated."""
    return bool(_TEST_COMMUNITY_RE.search(name))


def get_architecture_overview(store: GraphStore) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    Builds a node-to-community mapping, counts cross-community edges,
    and generates warnings for high coupling.

    Args:
        store: The GraphStore instance.

    Returns:
        Dict with keys: communities, cross_community_edges, warnings.
    """
    communities = get_communities(store)

    # Build node -> community_id mapping
    node_to_community: dict[str, int] = {}
    for comm in communities:
        comm_id = comm.get("id", 0)
        for qn in comm.get("members", []):
            node_to_community[qn] = comm_id

    # Count cross-community edges
    all_edges = store.get_all_edges()
    cross_edges: list[dict[str, Any]] = []
    cross_counts: Counter[tuple[int, int]] = Counter()

    for e in all_edges:
        # TESTED_BY edges are expected cross-community coupling (test → code),
        # not an architectural smell.
        if e.kind == "TESTED_BY":
            continue
        src_comm = node_to_community.get(e.source_qualified)
        tgt_comm = node_to_community.get(e.target_qualified)
        if (
            src_comm is not None
            and tgt_comm is not None
            and src_comm != tgt_comm
        ):
            pair = (min(src_comm, tgt_comm), max(src_comm, tgt_comm))
            cross_counts[pair] += 1
            cross_edges.append({
                "source_community": src_comm,
                "target_community": tgt_comm,
                "edge_kind": e.kind,
                "source": _sanitize_name(e.source_qualified),
                "target": _sanitize_name(e.target_qualified),
            })

    # Generate warnings for high coupling, skipping test-dominated pairs.
    warnings: list[str] = []
    comm_name_map = {c.get("id", 0): c["name"] for c in communities}
    for (c1, c2), count in cross_counts.most_common():
        if count > 10:
            name1 = comm_name_map.get(c1, f"community-{c1}")
            name2 = comm_name_map.get(c2, f"community-{c2}")
            # Skip pairs where either community is test-dominated — coupling
            # between test and production code is expected, not architectural.
            if _is_test_community(name1) or _is_test_community(name2):
                continue
            warnings.append(
                f"High coupling ({count} edges) between "
                f"'{name1}' and '{name2}'"
            )

    return {
        "communities": communities,
        "cross_community_edges": cross_edges,
        "warnings": warnings,
    }
