"""Tools 21, 22, 23: list_repos_func, cross_repo_search_func, cross_repo_requests_func."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .. import feign_links as fl
from .. import js_api_extract
from ..errors import GraphStoreError
from ..graph import GraphNode, GraphStore, _sanitize_name
from ..incremental import get_db_path
from ..repo_aliases import RepoNameResolver, load_repo_aliases
from ..search import hybrid_search
from ._common import _bounded, _shown_of, _validate_positive_int

logger = logging.getLogger(__name__)

# Hard ceiling on the merged result set. ``limit`` is per repo, so a
# registry with 40 repos returned 40x the caller's expectation.
_MAX_CROSS_REPO_RESULTS = 100

# Hard ceilings on the name-echo lists. ``repos`` is caller-supplied and
# unbounded, so echoing it back verbatim let one call carry far more tokens
# than the tool's own budget allows while reporting nothing was truncated.
# Names are also sanitised on the way out, like every other name in a
# response, which caps each entry at 256 characters.
_MAX_ECHOED_NAMES = 20


# ---------------------------------------------------------------------------
# Tool 21: list_repos  [REGISTRY]
# ---------------------------------------------------------------------------


def list_repos_func() -> dict[str, Any]:
    """List all registered repositories.

    [REGISTRY] Returns the list of repositories registered in the global
    multi-repo registry at ``~/.code-review-graph/registry.json``.

    Returns:
        List of registered repos with paths and aliases.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        return {
            "status": "ok",
            "summary": f"{len(repos)} registered repository(ies)",
            "repos": repos,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool 22: cross_repo_search  [REGISTRY]
# ---------------------------------------------------------------------------


def _select_repos(
    repos: list[dict[str, Any]],
    names: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Pick the registry entries named by ``names``, preserving registry order.

    A name matches an entry's alias first, and only if no alias matches does
    it match the final component of an entry's path, so an entry registered
    as ``{"path": ".../billing-api", "alias": "billing"}`` is selected by
    either ``["billing"]`` or ``["billing-api"]``. Aliases are resolved
    before folder names across the WHOLE registry, so an explicit alias
    always beats another entry's incidental folder name.

    A name that matches several entries selects ALL of them. Selecting only
    the first would search fewer repositories than the caller asked for
    while reporting nothing missing, which is the failure ``unknown`` exists
    to prevent; sibling checkouts sharing a folder name are the normal case
    this parameter was added for.

    A name that matches neither is looked up in the optional
    ``repo-aliases.toml`` (see ``repo_aliases.py``), whose target is resolved
    by the same two rules; that file never overrides a registered name.

    Registry order is preserved so the caller cannot change the merge
    tie-breaker by reordering ``names``.

    Returns the selected entries, the names that matched nothing, and the
    names that matched more than one entry.
    """
    resolver = RepoNameResolver(repos)
    wanted: set[int] = set()
    unknown: list[str] = []
    ambiguous: list[str] = []
    for name in names:
        matches = resolver.lookup(name)
        if not matches:
            unknown.append(name)
            continue
        wanted.update(matches)
        if len(matches) > 1:
            ambiguous.append(name)

    selected = [entry for i, entry in enumerate(repos) if i in wanted]
    return selected, unknown, ambiguous


def _echoed(names: list[str]) -> tuple[list[str], int, bool]:
    """Bound and sanitise a caller-supplied name list before echoing it back.

    ``repos`` is the only unbounded input this tool echoes. Returning it
    verbatim let a single call carry many times the tool's own token budget
    while reporting ``truncated: False``. Sanitising matches what every
    other name in a response gets and caps each entry at 256 characters.
    """
    cleaned = [_sanitize_name(name) for name in names]
    return _bounded(cleaned, _MAX_ECHOED_NAMES, _MAX_ECHOED_NAMES)


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    max_results: int = 50,
    repos: list[str] | None = None,
) -> dict[str, Any]:
    """Search across registered repositories.

    [REGISTRY] Runs hybrid_search on each registered repo's graph database
    and merges the results.

    Args:
        query: Search query string.
        kind: Optional node kind filter (e.g. "Function", "Class").
        limit: Maximum results per repo (default: 20).
        max_results: Maximum merged results to return across all repos
            (default 50, capped at 100). ``total`` reports the untruncated
            merged count; without it the response grew with the number of
            registered repos rather than with the caller's ``limit``.
        repos: Optional aliases or folder names to search. Omitted or empty
            searches every registered repo. A registry that spans unrelated
            products otherwise contributes each repo's best local rank to the
            top of the merged list, so the caller pays for repositories that
            cannot answer the question. Matching is exact and case-sensitive
            and paths are not accepted, unlike ``Registry.resolve_repo``:
            this parameter only ever narrows an already-trusted registry, and
            a name that does not match is reported rather than guessed at.
            Names matching no entry are returned in ``unknown`` rather than
            dropped, since a silently skipped repo is indistinguishable from
            one that simply had no matches; names matching several entries
            select all of them and are listed in ``ambiguous``.

    Returns:
        Combined search results from the searched repos, plus ``total``
        and ``truncated``. When ``repos`` was given, also ``unknown`` and
        ``ambiguous`` with their ``*_total`` counts, both bounded like every
        other list in a response.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")

    try:
        registry = Registry()
        all_repos = registry.list_repos()
        if not all_repos:
            return {
                "status": "ok",
                "summary": (
                    "No repositories registered. "
                    "Use 'register' to add repos."
                ),
                "results": [],
            }

        unknown: list[str] = []
        ambiguous: list[str] = []
        unknown_shown: list[str] = []
        unknown_total = 0
        unknown_truncated = False
        ambiguous_shown: list[str] = []
        ambiguous_total = 0
        ambiguous_truncated = False
        if repos:
            selected, unknown, ambiguous = _select_repos(all_repos, repos)
            unknown_shown, unknown_total, unknown_truncated = _echoed(unknown)
            ambiguous_shown, ambiguous_total, ambiguous_truncated = _echoed(
                ambiguous
            )
            if not selected:
                # The count, never the names: the summary is the field agents
                # read first, and interpolating a caller-supplied list here
                # was the second copy of an unbounded echo.
                return {
                    "status": "ok",
                    "summary": (
                        f"No registered repository matches the "
                        f"{unknown_total} name(s) given. "
                        "Use 'repos' to list the registry."
                    ),
                    "results": [],
                    "total": 0,
                    "truncated": False,
                    "repos_searched": [],
                    "unknown": unknown_shown,
                    "unknown_total": unknown_total,
                    "unknown_truncated": unknown_truncated,
                    "ambiguous": ambiguous_shown,
                    "ambiguous_total": ambiguous_total,
                    "ambiguous_truncated": ambiguous_truncated,
                }
        else:
            selected = all_repos

        ranked_results: list[tuple[int, int, dict[str, Any]]] = []
        searched_repos: list[str] = []

        for repo_index, repo_entry in enumerate(selected):
            repo_path = Path(repo_entry["path"])
            db_path = get_db_path(repo_path)
            if not db_path.exists():
                continue

            try:
                store = GraphStore(str(db_path))
                try:
                    results = hybrid_search(
                        store, query, kind=kind, limit=limit
                    )
                    alias = repo_entry.get("alias", repo_path.name)
                    for local_rank, r in enumerate(results):
                        r["repo"] = alias
                        r["repo_path"] = str(repo_path)
                        ranked_results.append((local_rank, repo_index, r))
                    searched_repos.append(alias)
                finally:
                    store.close()
            except Exception as exc:
                logger.warning(
                    "Search failed for %s: %s", repo_path, exc
                )

        # Scores from different search paths are not comparable across repos.
        # Merge by each repo's local rank and use registry order as a stable tie-breaker.
        ranked_results.sort(key=lambda item: (item[0], item[1]))
        all_results, total, truncated = _bounded(
            [result for _, _, result in ranked_results],
            max_results,
            _MAX_CROSS_REPO_RESULTS,
        )

        # A dropped or duplicated name reaches the summary too. The
        # all-unknown branch already said so there, and CLAUDE.md trains
        # agents to read the summary first, so both branches must report it.
        notes = ""
        if unknown_total:
            notes += f"; {unknown_total} name(s) matched no repository"
        if ambiguous_total:
            notes += f"; {ambiguous_total} name(s) matched several"

        response = {
            "status": "ok",
            "summary": (
                f"Found {total} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
                + _shown_of(len(all_results), total)
                + notes
            ),
            "results": all_results,
            "total": total,
            "truncated": truncated,
            "repos_searched": searched_repos,
        }
        if repos:
            response["unknown"] = unknown_shown
            response["unknown_total"] = unknown_total
            response["unknown_truncated"] = unknown_truncated
            response["ambiguous"] = ambiguous_shown
            response["ambiguous_total"] = ambiguous_total
            response["ambiguous_truncated"] = ambiguous_truncated
        return response
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool 23: cross_repo_requests  [REGISTRY]
# ---------------------------------------------------------------------------

_MAX_REQUEST_ROWS = 100
_MAX_CANDIDATES_PER_ROW = 5
_MAX_UNREADABLE = 20


def _repo_label(entry: dict[str, Any]) -> str:
    return str(entry.get("alias") or Path(entry["path"]).name)


def _relative_file(repo_path: Path, file_path: Optional[str]) -> Optional[str]:
    """Return ``file_path`` relative to the repo, or ``None`` when it is not inside.

    A path outside the repository is dropped rather than echoed: the response
    only ever names files the registry entry owns.
    """
    if not file_path:
        return None
    try:
        return Path(file_path).relative_to(repo_path).as_posix()
    except ValueError:
        pass
    try:
        return Path(file_path).resolve().relative_to(repo_path.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _load_endpoints(
    entry: dict[str, Any],
) -> tuple[list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str]]:
    """Read one repo's Feign client calls and server endpoints, read-only.

    The third item is ``None`` on success, else ``"no_database"`` or
    ``"unreadable"``; nothing from the failure (a path, an exception text) is
    carried out.
    """
    repo_path = Path(entry["path"])
    try:
        db_path = get_db_path(repo_path, read_only=True)
        if not db_path.is_file():
            return [], [], "no_database"
        with GraphStore(db_path, read_only=True) as store:
            nodes: list[GraphNode] = store.get_nodes_by_kind(["Endpoint"])
    except (GraphStoreError, sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Cannot read endpoints of %s: %s", repo_path, exc)
        return [], [], "unreadable"

    clients: list[fl.ClientCall] = []
    servers: list[fl.ServerEndpoint] = []
    for node in nodes:
        extra = node.extra if isinstance(node.extra, dict) else {}
        route = extra.get("route")
        method = extra.get("http_method")
        if not isinstance(route, str) or not isinstance(method, str):
            continue
        file = _relative_file(repo_path, node.file_path)
        if extra.get("role") == "client":
            client_tag = extra.get("client")
            if client_tag == js_api_extract.CLIENT_TAG:
                notes: list[str] = []
                if extra.get("inferred"):
                    notes.append("ember_data_convention")
                if extra.get("receiver_inferred"):
                    notes.append("receiver_inferred")
                if extra.get("base_url_dynamic"):
                    notes.append("base_url_not_literal")
                kind = extra.get("client_kind")
                clients.append(fl.ClientCall(
                    repo=_repo_label(entry), file=file, line=node.line_start,
                    http_method=method, route=route, origin="js",
                    client_kind=kind if isinstance(kind, str) else None,
                    route_dynamic=bool(extra.get("route_dynamic")),
                    method_dynamic=bool(extra.get("method_dynamic")),
                    absolute_url=bool(extra.get("absolute_url")),
                    notes=tuple(notes),
                ))
                continue
            if client_tag != "feign":
                continue
            service = extra.get("service")
            clients.append(fl.ClientCall(
                repo=_repo_label(entry), file=file, line=node.line_start,
                http_method=method, route=route,
                service=service if isinstance(service, str) else None,
                service_unresolved=bool(extra.get("service_unresolved")),
                route_dynamic=bool(extra.get("route_dynamic")),
                url_literal=bool(extra.get("url_literal")),
                url_from_argument=bool(extra.get("url_from_argument")),
            ))
        else:
            servers.append(fl.ServerEndpoint(
                repo=_repo_label(entry), file=file, line=node.line_start,
                http_method=method, route=route, origin=str(repo_path),
                catch_all=bool(extra.get("catch_all")),
                optional_catch_all=bool(extra.get("optional_catch_all")),
            ))
    return clients, servers, None


def _route_in_scope(call_route: str, wanted: str) -> bool:
    """Whether ``call_route`` equals ``wanted`` or sits below it, segment-wise."""
    have = fl.normalize_route(call_route)
    return wanted == "/" or have == wanted or have.startswith(wanted + "/")


def _candidate_row(server: fl.ServerEndpoint) -> dict[str, Any]:
    row: dict[str, Any] = {
        "repo": _sanitize_name(server.repo),
        "file": _sanitize_name(server.file) if server.file else None,
        "line": server.line,
        "http_method": _sanitize_name(server.http_method),
        "route": _sanitize_name(server.route),
    }
    if server.catch_all:
        row["catch_all"] = True
    return row


def cross_repo_requests_func(
    repos: list[str] | None = None,
    service: str | None = None,
    route: str | None = None,
    status: str | None = None,
    limit: int = 20,
    max_results: int = 50,
    origin: str | None = None,
) -> dict[str, Any]:
    """Join outbound Spring Feign and JavaScript/TypeScript calls to server endpoints.

    [REGISTRY] Reads each registered repo's graph database read-only, finds
    the Feign client calls in the selected (client) repos, and for each one
    looks for the matching controller endpoint in the repo its service name
    points at. JavaScript and TypeScript calls (axios, fetch, jQuery, Ember
    ajax and Ember Data) have no service name: they are matched by method and
    route against the server endpoints of every other registered repo.
    Nothing is stored; the join is recomputed on every call.

    Args:
        repos: Optional aliases or folder names of the CLIENT repos whose
            calls are listed (same matching, ``unknown`` and ``ambiguous``
            reporting as ``cross_repo_search_func``). Omitted or empty lists
            calls from every registered repo. Target repos are always looked
            up across the whole registry.
        service: Only calls whose Feign service name equals this (exact); this
            leaves out JavaScript/TypeScript calls, which have none.
        route: Only calls whose route equals this or lies below it, compared
            after normalisation.
        status: Only rows with this status: ``matched``, ``ambiguous``,
            ``no_route_match``, ``no_target_repo`` or ``unmatchable``.
        limit: Maximum rows per client repo (default 20).
        max_results: Maximum rows returned overall (default 50, capped at
            100). ``counts`` and ``total`` cover every call that passed the
            filters, not just the rows shown.
        origin: Only ``feign`` or only ``js`` calls. Default: both.

    Returns:
        ``results`` rows, ``counts`` by status, ``total``, ``truncated``,
        ``repos_searched`` and ``unreadable`` (repos whose graph could not be
        read), plus ``unknown``/``ambiguous`` when ``repos`` was given.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")
    if status is not None and status not in fl.STATUSES:
        raise ValueError(f"status must be one of: {', '.join(fl.STATUSES)}")
    if origin is not None and origin not in ("feign", "js"):
        raise ValueError("origin must be one of: feign, js")

    try:
        all_repos = Registry().list_repos()
        if not all_repos:
            return {
                "status": "ok",
                "summary": "No repositories registered. Use 'register' to add repos.",
                "results": [],
                "counts": {},
                "total": 0,
                "truncated": False,
            }

        unknown_shown: list[str] = []
        unknown_total = 0
        unknown_truncated = False
        ambiguous_shown: list[str] = []
        ambiguous_total = 0
        ambiguous_truncated = False
        if repos:
            selected, unknown, ambiguous = _select_repos(all_repos, repos)
            unknown_shown, unknown_total, unknown_truncated = _echoed(unknown)
            ambiguous_shown, ambiguous_total, ambiguous_truncated = _echoed(ambiguous)
            if not selected:
                return {
                    "status": "ok",
                    "summary": (
                        f"No registered repository matches the "
                        f"{unknown_total} name(s) given. Use 'repos' to list the registry."
                    ),
                    "results": [],
                    "counts": {},
                    "total": 0,
                    "truncated": False,
                    "repos_searched": [],
                    "unreadable": [],
                    "unknown": unknown_shown,
                    "unknown_total": unknown_total,
                    "unknown_truncated": unknown_truncated,
                    "ambiguous": ambiguous_shown,
                    "ambiguous_total": ambiguous_total,
                    "ambiguous_truncated": ambiguous_truncated,
                }
        else:
            selected = all_repos

        resolver = RepoNameResolver(all_repos)
        folded: dict[str, set[str]] = defaultdict(set)
        names = [n for e in all_repos for n in (e.get("alias"), Path(e["path"]).name) if n]
        names += list(load_repo_aliases())
        for name in names:
            folded[fl.normalize_service_name(name)].add(name)

        selected_ids = {id(e) for e in selected}
        # Each repo's graph is read at most once, whether it turns up as a
        # client repo, a target, or both.
        loaded: dict[
            int, tuple[list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str]]
        ] = {}
        unreadable: list[dict[str, str]] = []
        unreadable_seen: set[tuple[str, str]] = set()

        def note_unreadable(entry: dict[str, Any], role: str, reason: str) -> None:
            key = (_repo_label(entry), role)
            if key not in unreadable_seen:
                unreadable_seen.add(key)
                unreadable.append({
                    "repo": _sanitize_name(key[0]), "role": role, "reason": reason,
                })

        def load(index: int) -> tuple[
            list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str]
        ]:
            if index not in loaded:
                loaded[index] = _load_endpoints(all_repos[index])
            return loaded[index]

        def servers_of(index: int) -> Optional[list[fl.ServerEndpoint]]:
            _c, srv, why = load(index)
            if why:
                note_unreadable(all_repos[index], "target", why)
                return None
            return srv

        # A JavaScript call names no service, so every other registered repo
        # is a candidate; its index is built once per calling repo.
        js_index: dict[int, fl.ServerIndex] = {}

        def js_index_for(client_index: int) -> fl.ServerIndex:
            if client_index not in js_index:
                pool: list[fl.ServerEndpoint] = []
                for other in range(len(all_repos)):
                    if other == client_index:
                        continue
                    found = servers_of(other)
                    if found:
                        pool.extend(found)
                js_index[client_index] = fl.ServerIndex(pool)
            return js_index[client_index]

        wanted_route = fl.normalize_route(route) if route else None
        counts: dict[str, int] = {}
        origin_counts: dict[str, int] = {}
        rows_by_repo: list[list[dict[str, Any]]] = []
        searched: list[str] = []
        total = 0

        for index, entry in enumerate(all_repos):
            if id(entry) not in selected_ids:
                continue
            clients, _own_servers, why = load(index)
            if why:
                note_unreadable(entry, "client", why)
                continue
            searched.append(_sanitize_name(_repo_label(entry)))
            seen: set[tuple[Any, ...]] = set()
            repo_rows: list[dict[str, Any]] = []
            for call in sorted(
                clients,
                key=lambda c: (c.file or "", c.line or 0, c.route, c.http_method),
            ):
                key = (
                    call.file, call.line, call.http_method, call.route, call.service,
                    call.origin,
                )
                if key in seen:
                    continue
                seen.add(key)
                if origin is not None and call.origin != origin:
                    continue
                if service is not None and call.service != service:
                    continue
                if wanted_route is not None and not _route_in_scope(call.route, wanted_route):
                    continue

                reason = fl.unmatchable_reason(call)
                resolution: Optional[fl.TargetResolution] = None
                result = fl.LinkResult(fl.UNMATCHABLE, reason)
                if reason is None and call.origin == "js":
                    result = fl.link_js_call(call, js_index_for(index))
                elif reason is None:
                    assert call.service is not None
                    resolution = fl.resolve_service(call.service, resolver.lookup, folded)
                    if not resolution.indexes:
                        result = fl.LinkResult(fl.NO_TARGET_REPO, resolution.reason)
                    else:
                        pool: list[fl.ServerEndpoint] = []
                        missing = False
                        for target in resolution.indexes:
                            found = servers_of(target)
                            if found is None:
                                missing = True
                            else:
                                pool.extend(found)
                        result = fl.link_call(call, pool)
                        if missing and result.status == fl.NO_ROUTE_MATCH:
                            result = fl.LinkResult(fl.NO_TARGET_REPO, "target_unreadable")
                if status is not None and result.status != status:
                    continue

                counts[result.status] = counts.get(result.status, 0) + 1
                origin_counts[call.origin] = origin_counts.get(call.origin, 0) + 1
                total += 1
                row: dict[str, Any] = {
                    "client_repo": _sanitize_name(call.repo),
                    "client_file": _sanitize_name(call.file) if call.file else None,
                    "client_line": call.line,
                    "http_method": _sanitize_name(call.http_method),
                    "route": _sanitize_name(call.route),
                    "service": _sanitize_name(call.service) if call.service else None,
                    "status": result.status,
                    "origin": call.origin,
                }
                if call.origin == "js":
                    if call.client_kind:
                        row["client_kind"] = _sanitize_name(call.client_kind)
                    row_notes = list(call.notes)
                    if result.confidence == "low":
                        row_notes.append("catch_all_route")
                    if row_notes and result.status in (fl.MATCHED, fl.AMBIGUOUS):
                        row["confidence"] = "low"
                        row["confidence_notes"] = row_notes
                if result.reason:
                    row["reason"] = result.reason
                if resolution is not None and resolution.how:
                    row["target_resolution"] = resolution.how
                if result.candidates:
                    row["candidates"] = [
                        _candidate_row(c) for c in result.candidates[:_MAX_CANDIDATES_PER_ROW]
                    ]
                    row["candidates_total"] = len(result.candidates)
                repo_rows.append(row)
            rows_by_repo.append(repo_rows[:limit])

        merged = [row for rows in rows_by_repo for row in rows]
        results, _shown_total, truncated = _bounded(merged, max_results, _MAX_REQUEST_ROWS)
        truncated = truncated or len(merged) < total
        shown_unreadable, unreadable_total, unreadable_truncated = _bounded(
            unreadable, _MAX_UNREADABLE, _MAX_UNREADABLE,
        )

        notes = ""
        if unknown_total:
            notes += f"; {unknown_total} name(s) matched no repository"
        if ambiguous_total:
            notes += f"; {ambiguous_total} name(s) matched several"
        if unreadable_total:
            notes += f"; {unreadable_total} repo(s) could not be read"
        ordered = ", ".join(f"{counts[s]} {s}" for s in fl.STATUSES if s in counts)
        response: dict[str, Any] = {
            "status": "ok",
            "summary": (
                (
                    f"{total} call(s) ({origin_counts.get('feign', 0)} Feign, "
                    f"{origin_counts.get('js', 0)} JS/TS) in {len(searched)} repo(s)"
                    if origin_counts.get("js")
                    else f"{total} Feign call(s) in {len(searched)} repo(s)"
                )
                + (f": {ordered}" if ordered else "")
                + _shown_of(len(results), total)
                + notes
            ),
            "results": results,
            "counts": counts,
            "total": total,
            "truncated": truncated,
            "repos_searched": searched,
            "unreadable": shown_unreadable,
            "unreadable_total": unreadable_total,
            "unreadable_truncated": unreadable_truncated,
        }
        if repos:
            response["unknown"] = unknown_shown
            response["unknown_total"] = unknown_total
            response["unknown_truncated"] = unknown_truncated
            response["ambiguous"] = ambiguous_shown
            response["ambiguous_total"] = ambiguous_total
            response["ambiguous_truncated"] = ambiguous_truncated
        return response
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "error": str(exc)}
