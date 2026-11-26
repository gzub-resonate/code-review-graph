"""Query-time helpers for ``query_graph``: the ``endpoints`` listing, route targets,
best-match resolution of an ambiguous name, and Java file importers.

Nothing here changes what a graph build writes. Everything is derived from nodes and
edges that already exist, so an older graph answers too.

Direction of an endpoint (``endpoints`` pattern) is not stored. It is derived, and the
reply says which signal decided each class:

* ``outbound``: ``extra.role == "client"`` (a Feign or JavaScript client declaration) or
  the node is the target of a ``REQUESTS`` edge.
* ``simulator``: a server route whose path has a ``__`` or ``sim``-like segment, or whose
  declaring class or file name contains ``simulator``. ``CRG_SIMULATOR_PATHS`` (comma
  separated, case-insensitive substrings of the route) replaces the path test.
* ``inbound``: every other server route.

Call sites written with ``RestClient``, ``WebClient`` or ``@HttpExchange`` are not stored
as ``Endpoint`` nodes, so they do not appear.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..constants import SPRING_FEIGN_EDGE_KIND
from ..graph import GraphEdge, GraphNode, GraphStore, _sanitize_name
from ..paging import graph_stamp, make_scope, paginate
from ..parser import normalize_file_path

ENDPOINT_DIRECTIONS = ("inbound", "outbound", "simulator")
#: Hard ceiling on rows in one ``endpoints`` reply; ``cursor`` reaches the rest.
MAX_ENDPOINT_ROWS = 500
#: Environment variable that replaces the default simulator path test.
SIMULATOR_PATHS_ENV = "CRG_SIMULATOR_PATHS"

DIRECTION_SIGNAL = (
    "derived at query time: out = role 'client' (Feign/JS) or REQUESTS edge; sim = route "
    "segment '__*'/'sim*' or 'simulator' in class/path (CRG_SIMULATOR_PATHS overrides the "
    "segment test); in = other server routes. RestClient/WebClient calls are not endpoints."
)

_SIM_SEGMENT = re.compile(r"^(?:__|sim(?:$|[-_.])|simulat)", re.IGNORECASE)
_METHOD_ANY = frozenset({"", "ANY", "*", "REQUEST"})
_HTTP_METHODS = "GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE|REQUEST|ANY"
_ROUTE_TARGET = re.compile(rf"^(?:({_HTTP_METHODS})\s+)?(/[^\s]*)$", re.IGNORECASE)
_PLACEHOLDER_SEGMENT = re.compile(r"^(?:\{[^}]*\}|:[A-Za-z_]\w*|<[^>]*>)$")

#: Candidate patterns that pick a best match instead of answering ``ambiguous``.
AUTO_RESOLVE_PATTERNS = frozenset({"handlers_of", "references_to"})
_MAX_ALTERNATIVES = 10


# ------------------------------------------------------------------ endpoints listing


def _field(value: Any, spaces: bool = False) -> str:
    """One repo-derived string for a reply: the codebase's ``_sanitize_name`` (control
    characters stripped, length cut), then whitespace folded to single spaces so a value can
    never start a new row or column. Every repo-derived field in this module goes through it."""
    folded = " ".join(_sanitize_name(str(value)).split())
    return folded if spaces else folded.replace(" ", "_")


def _simulator_route(route: str) -> bool:
    configured = os.environ.get(SIMULATOR_PATHS_ENV, "").strip()
    if configured:
        needles = [p.strip().lower() for p in configured.split(",") if p.strip()]
        return any(needle in route.lower() for needle in needles)
    return any(_SIM_SEGMENT.match(seg) for seg in route.split("/") if seg)


def classify_direction(node: GraphNode, requested: set[str], root: Path | None = None) -> str:
    """``inbound``, ``outbound`` or ``simulator`` for one ``Endpoint`` node."""
    extra = node.extra if isinstance(node.extra, dict) else {}
    if extra.get("role") == "client" or node.qualified_name in requested:
        return "outbound"
    route = str(extra.get("route") or "")
    where = _relative(node.file_path, root) if root else Path(node.file_path).name
    owner = f"{node.parent_name or ''} {where}".lower()
    if "simulator" in owner or _simulator_route(route):
        return "simulator"
    return "inbound"


def _requested_endpoints(store: GraphStore) -> set[str]:
    rows = store._conn.execute(
        "SELECT DISTINCT target_qualified FROM edges WHERE kind = ?",
        (SPRING_FEIGN_EDGE_KIND,),
    ).fetchall()
    return {r[0] for r in rows}


def _owner(node: GraphNode) -> str:
    extra = node.extra if isinstance(node.extra, dict) else {}
    handler = extra.get("handler") or extra.get("client_method")
    if not isinstance(handler, str):
        handler = node.name.split("@", 1)[0]
    return _field(f"{node.parent_name}.{handler}" if node.parent_name else handler)


def _relative(path: str, root: Path) -> str:
    prefix = f"{normalize_file_path(root)}/"
    path = normalize_file_path(path)
    return path[len(prefix):] if path.startswith(prefix) else path


def _common_dir(paths: list[str]) -> str:
    """The directory every path shares, so a page does not repeat it on each row."""
    if not paths:
        return ""
    parts = [p.split("/")[:-1] for p in paths]
    common = parts[0]
    for other in parts[1:]:
        size = 0
        for a, b in zip(common, other):
            if a != b:
                break
            size += 1
        common = common[:size]
        if not common:
            break
    return "/".join(common)


def query_endpoints(
    store: GraphStore,
    root: Path,
    target: str,
    direction: str | None,
    minimal: bool,
    limit: int,
    cursor: str | None,
    description: str,
) -> dict[str, Any]:
    """Compact, paged table of ``Endpoint`` nodes.

    *target* is an optional case-insensitive substring of ``"<METHOD> <route>"`` (empty or
    ``*`` lists all). *direction* keeps one of ``ENDPOINT_DIRECTIONS``. Each row is one line
    ``METHOD route Class.handler file:line in|out|sim`` (space separated); the directory
    every row of the page shares is hoisted into ``files_under``. ``direction_counts`` covers
    every endpoint that matches *target*, whatever its direction.
    """
    needle = (target or "").strip()
    needle = "" if needle == "*" else needle.lower()
    requested = _requested_endpoints(store)
    counts = {d: 0 for d in ENDPOINT_DIRECTIONS}
    rows: list[tuple[tuple[str, str, str, int], GraphNode, str]] = []
    for node in store.get_nodes_by_kind(["Endpoint"]):
        extra = node.extra if isinstance(node.extra, dict) else {}
        route, method = extra.get("route"), extra.get("http_method")
        if not isinstance(route, str) or not isinstance(method, str):
            continue
        if needle and needle not in f"{method} {route}".lower():
            continue
        kind = classify_direction(node, requested, root)
        counts[kind] += 1
        if direction and direction != "all" and kind != direction:
            continue
        rows.append(((route, method, node.file_path, node.line_start), node, kind))
    rows.sort(key=lambda r: r[0])
    scope = make_scope("query_graph_tool", pattern="endpoints", target=needle,
                       direction=direction or "all")
    page = paginate(rows, limit, MAX_ENDPOINT_ROWS, cursor, scope=scope,
                    stamp=graph_stamp(store))
    rels = [_field(_relative(n.file_path, root)) for _k, n, _d in page.items]
    base = _common_dir(rels)
    short = {"inbound": "in", "outbound": "out", "simulator": "sim"}
    lines: list[str] = []
    for (_key, node, kind), rel in zip(page.items, rels):
        extra = node.extra
        where = rel[len(base) + 1:] if base else rel
        line = f"{_field(extra['http_method'])} {_field(extra['route'])} {_owner(node)}"
        if not minimal:
            line += f" {where}:{int(node.line_start)}"
        lines.append(f"{line} {short[kind]}")
    total = page.total
    summary = (
        f"{total} endpoint(s)" + (f" matching '{target}'" if needle else "")
        + (f" with direction={direction}" if direction and direction != "all" else "")
        + f"; by direction: inbound {counts['inbound']}, outbound {counts['outbound']}, "
        f"simulator {counts['simulator']}"
    )
    if page.offset or page.next_cursor:
        summary += f"; showing {page.offset + 1}-{page.offset + len(lines)}"
    response: dict[str, Any] = {
        "status": "ok", "pattern": "endpoints", "target": target,
        "description": description, "summary": summary,
        "columns": "METHOD route owner file:line dir (space separated)" if not minimal
        else "METHOD route owner dir (space separated)",
        "result_count": total, "results_omitted": max(0, total - page.offset - len(lines)),
        "direction_counts": counts, "results": lines,
        **page.fields(),
    }
    if base and not minimal:
        response["files_under"] = _field(base)
    response["direction_signal"] = DIRECTION_SIGNAL
    return response


# ------------------------------------------------------------------ route targets


def normalize_route(path: str) -> str:
    """Lower-case route with ``{id}``, ``:id`` and ``<id>`` made one placeholder."""
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    segments = [s for s in path.split("/") if s]
    out = ["{}" if _PLACEHOLDER_SEGMENT.match(s) else s.lower() for s in segments]
    return "/" + "/".join(out)


def parse_route_target(target: str) -> tuple[str | None, str] | None:
    """``(METHOD or None, path)`` when *target* is written as a route, else ``None``."""
    if "::" in target:
        return None
    found = _ROUTE_TARGET.match(target.strip())
    if not found:
        return None
    method = found.group(1)
    return (method.upper() if method else None), found.group(2)


def match_route_endpoints(
    store: GraphStore, target: str, *, any_role: bool = False,
) -> list[GraphNode] | None:
    """Server ``Endpoint`` nodes a route string such as ``POST /v1/items`` names.

    ``None`` when *target* is not a route string; ``[]`` when it is one that matches
    nothing. A route written in full wins; otherwise a route that ends with the given path
    on a segment boundary (``/items/{id}`` for ``/v1/items/{id}``) is taken. Outbound
    (``role == "client"``) endpoints are left out unless *any_role* is set.
    """
    parsed = parse_route_target(target)
    if parsed is None:
        return None
    method, path = parsed
    wanted = normalize_route(path)
    exact: list[GraphNode] = []
    suffix: list[GraphNode] = []
    for node in store.get_nodes_by_kind(["Endpoint"]):
        extra = node.extra if isinstance(node.extra, dict) else {}
        route = extra.get("route")
        if not isinstance(route, str) or (extra.get("role") == "client" and not any_role):
            continue
        verb = str(extra.get("http_method") or "").upper()
        if method and verb not in _METHOD_ANY and verb != method:
            continue
        have = normalize_route(route)
        if have == wanted:
            exact.append(node)
        elif wanted != "/" and have.endswith(wanted):
            suffix.append(node)
    found = exact or suffix
    found.sort(key=lambda n: (str(n.extra.get("route")), n.file_path, n.line_start))
    return found


def endpoint_label(node: GraphNode) -> str:
    extra = node.extra if isinstance(node.extra, dict) else {}
    return f"{_field(extra.get('http_method', ''))} {_field(extra.get('route', ''))}".strip()


# ------------------------------------------------------------------ best-match resolution


def _in_degree(store: GraphStore, qualified_name: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE target_qualified = ?", (qualified_name,),
    ).fetchone()
    return int(row[0])


def _has_handles_edge(store: GraphStore, qualified_name: str) -> bool:
    row = store._conn.execute(
        "SELECT 1 FROM edges WHERE kind = 'HANDLES' AND source_qualified = ? LIMIT 1",
        (qualified_name,),
    ).fetchone()
    return row is not None


def _is_exact(node: GraphNode, target: str) -> bool:
    return (
        node.qualified_name == target
        or node.name == target
        or node.qualified_name.endswith(f"::{target}")
    )


def rank_candidates(
    store: GraphStore,
    candidates: list[GraphNode],
    target: str,
    pattern: str,
    root: Path,
    file_hint: str | None = None,
) -> list[tuple[GraphNode, dict[str, Any]]]:
    """Candidates best first, each with the facts the ranking used.

    Order: an exact qualified-name or name match, then proximity (the ``file_hint`` file,
    else a production file over a test), then kind preference (``Class`` over ``Function``
    for ``references_to``; a node that is or serves an endpoint for ``handlers_of``),
    then the higher in-degree, then the qualified name.
    """
    hint = file_hint.replace("\\", "/").lower() if file_hint else None
    scored = []
    for node in candidates:
        in_degree = _in_degree(store, node.qualified_name)
        if pattern == "references_to":
            kind_rank = 0 if node.kind in ("Class", "Interface", "Enum", "Type", "Struct") else 1
        elif pattern == "handlers_of":
            serves = node.kind == "Endpoint" or _has_handles_edge(store, node.qualified_name)
            kind_rank = 0 if serves else 1
        else:
            kind_rank = 0
        facts = {
            "exact": _is_exact(node, target),
            "near": bool(hint) and hint in normalize_file_path(node.file_path).lower(),
            "test": bool(node.is_test),
            "kind_rank": kind_rank,
            "in_degree": in_degree,
        }
        key = (
            0 if facts["exact"] else 1,
            0 if facts["near"] else 1,
            1 if facts["test"] else 0,
            kind_rank,
            -in_degree,
            node.qualified_name,
        )
        scored.append((key, node, facts))
    scored.sort(key=lambda item: item[0])
    return [(node, facts) for _key, node, facts in scored]


def _reason(best: dict[str, Any], other: dict[str, Any], best_node: GraphNode,
            node: GraphNode, pattern: str) -> str:
    if best["exact"] and not other["exact"]:
        return "not an exact name match"
    if best["near"] and not other["near"]:
        return "outside the file_hint file"
    if other["test"] and not best["test"]:
        return "test code"
    if other["kind_rank"] > best["kind_rank"]:
        if pattern == "references_to":
            return f"kind {_field(node.kind)} ranks below {_field(best_node.kind)}"
        return "does not serve an endpoint"
    if other["in_degree"] < best["in_degree"]:
        return f"lower in-degree ({other['in_degree']} vs {best['in_degree']})"
    return "same rank; ordered by qualified name"


def resolve_best(
    store: GraphStore,
    candidates: list[GraphNode],
    target: str,
    pattern: str,
    root: Path,
    candidate_count: int,
    file_hint: str | None = None,
) -> tuple[GraphNode, dict[str, Any]] | None:
    """Pick the best of several candidates, or ``None`` when none is an exact match.

    A fuzzy full-text hit is never promoted: only a candidate that is the exact name or
    qualified name is a safe pick. The second value is the reply fields to merge:
    ``ambiguous``, ``resolved_to``, ``alternatives`` (one-line reason each), and counts.
    """
    ranked = rank_candidates(store, candidates, target, pattern, root, file_hint)
    best_node, best_facts = ranked[0]
    if not best_facts["exact"]:
        return None
    alternatives = []
    for node, facts in ranked[1:1 + _MAX_ALTERNATIVES]:
        alternatives.append({
            "qualified_name": _field(_relative(node.qualified_name, root), spaces=True),
            "kind": _field(node.kind),
            "file": f"{_field(_relative(node.file_path, root))}:{int(node.line_start)}",
            "reason": _reason(best_facts, facts, best_node, node, pattern),
        })
    return best_node, {
        "ambiguous": True,
        "resolved_to": _field(_relative(best_node.qualified_name, root), spaces=True),
        "alternatives": alternatives,
        "alternatives_total": candidate_count - 1,
    }


# ------------------------------------------------------------------ Java file importers


def java_class_fqns(store: GraphStore, file_path: str) -> tuple[list[str], str | None]:
    """Fully qualified names of the types a Java file declares, and its package."""
    fqns: list[str] = []
    package: str | None = None
    for node in store.get_nodes_by_file(file_path):
        if node.kind not in ("Class", "Interface", "Enum", "Type", "Struct", "Annotation"):
            continue
        pkg = node.extra.get("java_package") if isinstance(node.extra, dict) else None
        if not isinstance(pkg, str) or not pkg:
            continue
        package = package or pkg
        scope = f"{node.parent_name}." if node.parent_name else ""
        fqns.append(f"{pkg}.{scope}{node.name}")
    return sorted(set(fqns)), package


def java_class_importers(
    store: GraphStore, file_path: str,
) -> tuple[list[tuple[GraphEdge, str, str]], list[str]]:
    """Import edges that name a class of this Java file rather than its path.

    Java imports are by class. The build binds an import to the file when it can; one it
    cannot bind (another source root, a static or nested-member import) or a ``pkg.*``
    wildcard keeps the dotted name as its target. Each tuple is ``(edge, level, imported)``
    where *level* is ``class`` (the class or one of its members) or ``package_wildcard``.
    """
    fqns, package = java_class_fqns(store, file_path)
    found: list[tuple[GraphEdge, str, str]] = []
    for fqn in fqns:
        like = fqn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"
        for row in store._conn.execute(
            "SELECT * FROM edges WHERE kind = 'IMPORTS_FROM' "
            "AND (target_qualified = ? OR target_qualified LIKE ? ESCAPE '\\')",
            (fqn, like),
        ).fetchall():
            edge = store._row_to_edge(row)
            found.append((edge, "class", edge.target_qualified))
    if package:
        for edge in store.iter_edges_by_target(f"{package}.*"):
            if edge.kind == "IMPORTS_FROM":
                found.append((edge, "package_wildcard", edge.target_qualified))
    return found, fqns
