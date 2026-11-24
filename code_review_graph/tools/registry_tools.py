"""Tools 21, 22, 23: list_repos_func, cross_repo_search_func, cross_repo_requests_func."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Optional

from .. import feign_links as fl
from ..constants import cross_repo_budget_seconds, cross_repo_workers
from ..endpoint_index import attach as attach_endpoints
from ..endpoint_index import extract_endpoints, read_endpoint_index
from ..errors import GraphStoreError
from ..graph import GraphNode, GraphStore, _sanitize_name
from ..incremental import get_db_path
from ..paging import (
    CursorError,
    content_stamp,
    make_scope,
    paginate,
    paginate_groups,
    shown_range,
)
from ..repo_aliases import RepoNameResolver, load_repo_aliases
from ..search import hybrid_search
from ._common import _bounded, _validate_positive_int

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

_NO_RESULTS_FOR_CURSOR = (
    "the registry no longer holds a repository this cursor can continue; "
    "call again without a cursor to start over"
)


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


#: Match tiers, best first. ``cross_repo_search_tool`` orders its merged rows by
#: tier before anything else, because the scores of different repos are not
#: comparable and an exact name in one repo must not wait behind another repo's
#: weak rank-1 hit.
_MATCH_TIERS = ("exact", "exact_ci", "qualified", "prefix", "tokens", "fuzzy")
_TIER_ORDER = {name: rank for rank, name in enumerate(_MATCH_TIERS)}
_QUALIFIED_SEPARATORS = (".", "::")
#: Kind the hybrid search leaves out unless asked for; the exact lookup does too.
_UNSEARCHED_KIND = "Output"
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _name_tokens(text: str) -> set[str]:
    """Lowercase words of ``text``: split on non-alphanumerics and at camelCase."""
    tokens: set[str] = set()
    for run in _NON_ALNUM.split(text):
        tokens.update(part.lower() for part in _CAMEL_BOUNDARY.split(run) if part)
    return tokens


def _match_tier(query: str, row: dict[str, Any]) -> str:
    """The tier a search row falls in for ``query`` (see ``_MATCH_TIERS``)."""
    text = query.strip()
    name = str(row.get("name") or "")
    if not text or not name:
        return "fuzzy"
    if name == text:
        return "exact"
    if name.lower() == text.lower():
        return "exact_ci"
    if any(sep in text for sep in _QUALIFIED_SEPARATORS):
        qualified = str(row.get("qualified_name") or "")
        symbol = qualified.partition("::")[2] or qualified
        if symbol == text or any(symbol.endswith(sep + text) for sep in _QUALIFIED_SEPARATORS):
            return "qualified"
    if name.startswith(text):
        return "prefix"
    wanted = _name_tokens(text)
    if wanted and wanted <= _name_tokens(name):
        return "tokens"
    return "fuzzy"


def _node_row(node: GraphNode) -> dict[str, Any]:
    """A search row for a node the exact lookup found (``score`` 0.0: it was not ranked)."""
    return {
        "name": _sanitize_name(node.name),
        "qualified_name": _sanitize_name(node.qualified_name),
        "kind": node.kind,
        "file_path": node.file_path,
        "line_start": node.line_start,
        "line_end": node.line_end,
        "language": node.language or "",
        "params": node.params,
        "return_type": node.return_type,
        "signature": None,
        "score": 0.0,
    }


def _lookup_rows(
    store: GraphStore, query: str, kind: str | None, limit: int,
) -> list[dict[str, Any]]:
    """The rows of the nodes whose name (or qualified tail) is the query, at most ``limit``.

    A plain indexed lookup: no ranking, no embeddings, so it costs a fraction of
    ``hybrid_search`` and can run on every repo.
    """
    text = query.strip()
    if not text:
        return []
    if any(sep in text for sep in _QUALIFIED_SEPARATORS):
        found = store.search_nodes_by_qualified_tail(text, limit=limit)
    else:
        found = store.get_nodes_by_name(text)[:limit]
    return [
        _node_row(node) for node in found
        if (node.kind == kind if kind else node.kind != _UNSEARCHED_KIND)
    ]


def _search_repo(
    store: GraphStore, query: str, kind: str | None, limit: int,
) -> list[tuple[int, str, dict[str, Any]]]:
    """One repo's rows as ``(local_rank, match, row)``, at most ``limit`` of them.

    The hybrid search is cut at ``limit`` by relevance, which can drop an exact
    name behind fuzzier hits. So the rows an exact lookup finds that the search
    did not return are added, and the cut is made after ordering by match tier,
    so an exact match always survives it. ``local_rank`` is the position the
    row had in the search (lookup-only rows follow the last one).
    """
    rows = hybrid_search(store, query, kind=kind, limit=limit)
    seen = {row.get("qualified_name") for row in rows}
    for row in _lookup_rows(store, query, kind, limit):
        if row["qualified_name"] not in seen:
            seen.add(row["qualified_name"])
            rows.append(row)
    tiered = [(rank, _match_tier(query, row), row) for rank, row in enumerate(rows)]
    tiered.sort(key=lambda item: (_TIER_ORDER[item[1]], item[0]))
    return tiered[:limit]


def _search_one_repo(
    repo_index: int, repo_entry: dict[str, Any], query: str, kind: str | None, limit: int,
) -> tuple[str, list[tuple[int, int, int, dict[str, Any]]]] | None:
    """Search one repo (on a worker thread): its alias and its rows, or ``None``.

    ``None`` means the repo has no graph database or its search failed (logged), so it
    is not counted as searched. The graph is opened, and closed, inside this call: a
    connection is never shared between two threads.
    """
    repo_path = Path(repo_entry["path"])
    db_path = get_db_path(repo_path)
    if not db_path.exists():
        return None
    try:
        store = GraphStore(str(db_path))
        try:
            alias = repo_entry.get("alias", repo_path.name)
            rows: list[tuple[int, int, int, dict[str, Any]]] = []
            for local_rank, match, row in _search_repo(store, query, kind, limit):
                row["repo"] = alias
                row["repo_path"] = str(repo_path)
                row["match"] = match
                rows.append((_TIER_ORDER[match], local_rank, repo_index, row))
            return alias, rows
        finally:
            store.close()
    except Exception as exc:
        logger.warning("Search failed for %s: %s", repo_path, exc)
        return None


#: Seconds the exact-name pass may spend on the repos the budget kept the full search
#: out of. It is a bound on a slow volume, not a share of the call's budget.
_EXACT_PASS_SECONDS = 30.0


def _exact_pass(
    left: list[tuple[int, dict[str, Any]]], query: str, kind: str | None, limit: int,
) -> tuple[list[tuple[int, int, int, dict[str, Any]]], list[str]]:
    """Exact-name rows of the repos the time budget kept out of the full search.

    Without it an exact name in a repo reached after the budget ran out would be
    missing from the answer, though finding it costs one indexed lookup. Returns
    the rows as ``(tier rank, local rank, registry position, row)`` and the
    aliases of the repos looked in. A repo is still named in ``skipped_repos``
    (its fuzzy matches were not searched).
    """
    rows: list[tuple[int, int, int, dict[str, Any]]] = []
    looked_in: list[str] = []
    deadline = _now() + _EXACT_PASS_SECONDS
    for position, entry in left:
        if _out_of_budget(deadline):
            break
        repo_path = Path(entry["path"])
        db_path = get_db_path(repo_path)
        if not db_path.exists():
            continue
        try:
            store = GraphStore(str(db_path))
            try:
                found = _lookup_rows(store, query, kind, limit)
            finally:
                store.close()
        except Exception as exc:
            logger.warning("Exact lookup failed for %s: %s", repo_path, exc)
            continue
        alias = entry.get("alias", repo_path.name)
        looked_in.append(alias)
        for local_rank, row in enumerate(found):
            tier = _match_tier(query, row)
            if tier not in ("exact", "exact_ci", "qualified"):
                continue
            row["repo"] = alias
            row["repo_path"] = str(repo_path)
            row["match"] = tier
            rows.append((_TIER_ORDER[tier], local_rank, position, row))
    return rows, looked_in


def _repo_label(entry: dict[str, Any]) -> str:
    return str(entry.get("alias") or Path(entry["path"]).name)


def _now() -> float:
    """The clock the call budget runs on (a seam for tests)."""
    return time.monotonic()


def _out_of_budget(deadline: float | None) -> bool:
    return deadline is not None and _now() >= deadline


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    max_results: int = 50,
    repos: list[str] | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search across registered repositories.

    [REGISTRY] Runs hybrid_search on each registered repo's graph database
    and merges the results. Scores are not comparable between repos, so rows
    are ordered by match tier first (``match``: ``exact`` name, ``exact_ci``,
    ``qualified`` suffix, ``prefix``, ``tokens``, ``fuzzy``), then by each
    repo's local rank, then registry order. A repo's exact-name nodes are
    always kept, whatever ``limit`` is. At most ``CRG_CROSS_REPO_BUDGET_SECONDS``
    (default 300, 0 = unlimited) are spent per call: once spent no full search
    is started and the repos left are named in ``skipped_repos``. Those repos
    still get a plain exact-name lookup (bounded to 30 s), so an exact match in
    one of them is returned; ``exact_lookup_repos_total`` counts them.
    Up to ``CRG_CROSS_REPO_WORKERS`` repos (default 4, 1 = one at a time) are
    searched at once; the order of the rows does not depend on which finishes first.

    Args:
        query: Search query string.
        kind: Optional node kind filter (e.g. "Function", "Class").
        limit: Maximum results per repo (default: 20). The cut is made after
            ordering by match tier, so an exact match survives it.
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
        cursor: ``next_cursor`` from the previous reply, to read the next
            merged results. Omit it for the first page. A cursor is opaque
            and only valid for the same ``query``, ``kind``, ``limit`` and
            ``repos``, and only while the merged results are unchanged: the
            cursor holds a digest of them, because the repos are searched
            afresh on every call.

    Returns:
        Combined search results from the searched repos, plus ``total``
        and ``truncated``; every row has ``match`` (its tier). When the time
        budget ran out, ``skipped_repos`` (bounded), ``skipped_repos_total`` and
        ``truncated`` say which repos were not searched. When more results remain,
        ``next_cursor`` is the ``cursor`` that returns the next ones.
        When ``repos`` was given, also ``unknown`` and ``ambiguous`` with their
        ``*_total`` counts, both bounded like every other list in a response.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")

    try:
        registry = Registry()
        all_repos = registry.list_repos()
        if not all_repos:
            if cursor:
                raise CursorError(_NO_RESULTS_FOR_CURSOR)
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
                if cursor:
                    raise CursorError(_NO_RESULTS_FOR_CURSOR)
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

        budget = cross_repo_budget_seconds()
        deadline = _now() + budget if budget > 0 else None
        ranked_results: list[tuple[int, int, int, dict[str, Any]]] = []
        searched_repos: list[str] = []
        skipped: list[str] = []
        left: list[tuple[int, dict[str, Any]]] = []
        workers = cross_repo_workers()
        outcomes: dict[int, tuple[str, list[tuple[int, int, int, dict[str, Any]]]]] = {}
        in_flight: dict[Future[Any], int] = {}

        def reap(done: Any) -> None:
            for future in done:
                outcome = future.result()
                index = in_flight.pop(future)
                if outcome is not None:
                    outcomes[index] = outcome

        # Up to ``workers`` repos are searched at once. The budget is checked here, on the
        # calling thread and in registry order, each time a worker is free to start the next
        # repo, so what is skipped does not depend on which search finishes first.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crg-xrepo") as pool:
            for repo_index, repo_entry in enumerate(selected):
                while len(in_flight) >= workers:
                    reap(wait(in_flight, return_when=FIRST_COMPLETED).done)
                # The first repo always runs, so a call makes progress; after that no
                # new repo is started once the budget is spent, and each one left is named.
                if repo_index and _out_of_budget(deadline):
                    skipped.extend(_repo_label(e) for e in selected[repo_index:])
                    left = list(enumerate(selected))[repo_index:]
                    break
                future = pool.submit(
                    _search_one_repo, repo_index, repo_entry, query, kind, limit,
                )
                in_flight[future] = repo_index
            reap(wait(in_flight).done)

        # Whatever order the searches finished in, the rows go in registry order.
        for repo_index in sorted(outcomes):
            alias, rows = outcomes[repo_index]
            searched_repos.append(alias)
            ranked_results.extend(rows)

        # The repos the budget kept out of the full search still get the cheap exact-name
        # lookup, so an exact match in a late repo is not lost to the time limit.
        exact_rows, exact_looked_in = _exact_pass(left, query, kind, limit)
        ranked_results.extend(exact_rows)

        # Scores from different search paths are not comparable across repos.
        # Order by match tier first (an exact name beats any fuzzy hit, whatever
        # its repo), then by each repo's local rank, with registry order as a
        # stable tie-breaker.
        ranked_results.sort(key=lambda item: item[:3])
        merged = [item[3] for item in ranked_results]
        skipped_shown, skipped_total, skipped_truncated = _echoed(skipped)
        # The rows come from several graph databases and are searched afresh on every
        # call, so the stamp is a digest of the rows themselves, and of the repos left
        # out: a cursor made on a partial answer is only honoured for the same partial one.
        page = paginate(
            merged, max_results, _MAX_CROSS_REPO_RESULTS, cursor,
            scope=make_scope(
                "cross_repo_search_tool", query=query, kind=kind, limit=limit,
                repos=sorted(set(repos)) if repos else None,
            ),
            stamp=make_scope("cross_repo_search_rows", rows=merged, skipped=skipped),
        )
        all_results, total = page.items, page.total
        truncated = page.truncated or bool(skipped)

        # A dropped or duplicated name reaches the summary too. The
        # all-unknown branch already said so there, and CLAUDE.md trains
        # agents to read the summary first, so both branches must report it.
        notes = ""
        if unknown_total:
            notes += f"; {unknown_total} name(s) matched no repository"
        if ambiguous_total:
            notes += f"; {ambiguous_total} name(s) matched several"
        if skipped_total:
            notes += (
                f"; time budget of {budget:g}s spent, {skipped_total} repo(s) not searched "
                f"(exact names were still looked up in {len(exact_looked_in)} of them; "
                "see skipped_repos; raise CRG_CROSS_REPO_BUDGET_SECONDS or narrow 'repos')"
            )

        response = {
            "status": "ok",
            "summary": (
                f"Found {total} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
                + shown_range(page)
                + notes
            ),
            "results": all_results,
            "total": total,
            "truncated": truncated,
            "repos_searched": searched_repos,
            "skipped_repos": skipped_shown,
            "skipped_repos_total": skipped_total,
            "skipped_repos_truncated": skipped_truncated,
            "exact_lookup_repos_total": len(exact_looked_in),
            **page.fields(),
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


_STORED = "stored"
_COMPUTED = "computed"


def _load_endpoints(
    entry: dict[str, Any],
) -> tuple[list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str], Optional[str]]:
    """Read one repo's Feign client calls and server endpoints, read-only.

    The third item is ``None`` on success, else ``"no_database"`` or
    ``"unreadable"``; nothing from the failure (a path, an exception text) is
    carried out. The fourth says where the endpoints came from: ``"stored"`` (the
    index the build wrote) or ``"computed"`` (read from the ``Endpoint`` nodes, for a
    graph with no current index).
    """
    repo_path = Path(entry["path"])
    try:
        db_path = get_db_path(repo_path, read_only=True)
        if not db_path.is_file():
            return [], [], "no_database", None
        with GraphStore(db_path, read_only=True) as store:
            endpoints = read_endpoint_index(store)
            source = _STORED
            if endpoints is None:
                source = _COMPUTED
                endpoints = extract_endpoints(store.get_nodes_by_kind(["Endpoint"]), repo_path)
    except (GraphStoreError, sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Cannot read endpoints of %s: %s", repo_path, exc)
        return [], [], "unreadable", None
    clients, servers = attach_endpoints(_repo_label(entry), repo_path, endpoints)
    return clients, servers, None, source


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
    cursor: str | None = None,
) -> dict[str, Any]:
    """Join outbound Spring Feign and JavaScript/TypeScript calls to server endpoints.

    [REGISTRY] Reads each registered repo's graph database read-only, finds
    the Feign client calls in the selected (client) repos, and for each one
    looks for the matching controller endpoint in the repo its service name
    points at. JavaScript and TypeScript calls (axios, fetch, jQuery, Ember
    ajax and Ember Data) have no service name: they are matched by method and
    route against the server endpoints of every other registered repo.
    Each repo's endpoints are read from the index its build stored (see
    ``endpoint_index.py``), or worked out from its graph when it has none that is
    current; the join itself depends on the whole registry and is recomputed on
    every call.

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
            ``case_mismatch``, ``no_route_match``, ``no_target_repo`` or
            ``unmatchable``.
        limit: Maximum rows per client repo (default 20).
        max_results: Maximum rows returned overall (default 50, capped at
            100). ``counts`` and ``total`` cover every call that passed the
            filters, not just the rows shown.
        cursor: ``next_cursor`` from the previous reply, to read the next page of
            the same query. Each page takes up to ``limit`` more rows from every
            client repo, then keeps the first ``max_results``. Omit it for the
            first page.
        origin: Only ``feign`` or only ``js`` calls. Default: both.

    The call has a time budget, ``CRG_CROSS_REPO_BUDGET_SECONDS`` (default 300,
    0 = unlimited). Once spent, no further client repo is started; those left are
    named in ``skipped_repos`` (with ``skipped_repos_total``), ``truncated`` is set,
    and ``counts`` and ``total`` leave them out. A cursor made on such a partial
    answer is refused unless the same repos are skipped again.

    Returns:
        ``results`` rows, ``counts`` by status, ``total``, ``truncated``,
        ``repos_searched``, ``endpoint_index`` (how many of the repos read had their
        endpoints from the stored index and how many from their graph) and
        ``unreadable`` (repos whose graph could not be read), plus
        ``unknown``/``ambiguous`` when ``repos`` was given.
        When rows remain, ``next_cursor`` is the ``cursor`` that returns the next page.
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
            int,
            tuple[
                list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str], Optional[str],
            ],
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
            list[fl.ClientCall], list[fl.ServerEndpoint], Optional[str], Optional[str]
        ]:
            if index not in loaded:
                loaded[index] = _load_endpoints(all_repos[index])
            return loaded[index]

        def servers_of(index: int) -> Optional[list[fl.ServerEndpoint]]:
            _c, srv, why, _src = load(index)
            if why:
                note_unreadable(all_repos[index], "target", why)
                return None
            return srv

        # A JavaScript call names no service, so every other registered repo
        # is a candidate; its index is built once per calling repo.  The
        # caller's own endpoints are indexed apart because they are searched
        # first (a relative URL goes to the origin that served the page).
        js_index: dict[int, fl.ServerIndex] = {}
        own_index: dict[int, fl.ServerIndex] = {}

        def own_index_for(client_index: int) -> fl.ServerIndex:
            if client_index not in own_index:
                own_index[client_index] = fl.ServerIndex(
                    load(client_index)[1],
                )
            return own_index[client_index]

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
        skipped: list[str] = []
        total = 0
        budget = cross_repo_budget_seconds()
        deadline = _now() + budget if budget > 0 else None
        started = 0

        for index, entry in enumerate(all_repos):
            if id(entry) not in selected_ids:
                continue
            # The first client repo always runs, so a call makes progress; after
            # that no new one is started once the budget is spent, and each is named.
            if started and _out_of_budget(deadline):
                skipped.append(_repo_label(entry))
                continue
            started += 1
            clients, _own_servers, why, _src = load(index)
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
                    result = fl.link_js_call(
                        call, js_index_for(index), own_index_for(index),
                    )
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
                        row_notes.append(result.reason or "catch_all_route")
                    if row_notes and result.status in (fl.MATCHED, fl.AMBIGUOUS):
                        row["confidence"] = "low"
                        row["confidence_notes"] = row_notes
                if result.status == fl.CASE_MISMATCH:
                    row["confidence"] = "low"
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
            rows_by_repo.append(repo_rows)

        page = paginate_groups(
            rows_by_repo, limit, max_results, _MAX_REQUEST_ROWS, cursor,
            scope=make_scope(
                "cross_repo_requests_tool", repos=sorted(repos) if repos else None,
                service=service, route=route, status=status, origin=origin,
            ),
            # The client repos left out are part of the stamp: a cursor made on a partial
            # answer is only honoured for the same partial one.
            stamp=content_stamp([rows_by_repo, skipped]),
        )
        results = page.items
        truncated = page.truncated or bool(skipped)
        skipped_shown, skipped_total, skipped_truncated = _echoed(skipped)
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
        if skipped_total:
            notes += (
                f"; time budget of {budget:g}s spent, {skipped_total} client repo(s) not read "
                "(see skipped_repos; counts and total leave them out; "
                "raise CRG_CROSS_REPO_BUDGET_SECONDS or narrow 'repos')"
            )
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
                + shown_range(page)
                + notes
            ),
            "results": results,
            "counts": counts,
            "total": total,
            "truncated": truncated,
            **page.fields(),
            "repos_searched": searched,
            "skipped_repos": skipped_shown,
            "skipped_repos_total": skipped_total,
            "skipped_repos_truncated": skipped_truncated,
            "endpoint_index": {
                kind: sum(1 for item in loaded.values() if item[3] == kind)
                for kind in (_STORED, _COMPUTED)
            },
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
