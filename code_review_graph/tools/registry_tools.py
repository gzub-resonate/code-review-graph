"""Tools 21, 22, 23: list_repos_func, cross_repo_search_func, cross_repo_requests_func."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Optional

from .. import amqp_join as aj
from .. import aws_sdk_links as al
from .. import feign_links as fl
from .. import infra_join as ij
from .. import service_aliases as sa
from .. import stepfunction_links as sl
from .. import terraform_links as tl
from .. import workflow_links as wl
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
from ..repo_cache import MEMO, SKIPPED, parallel_map_budget
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
#: Tiers that make weaker rows noise, and the rows hidden then (see ``include_fuzzy``).
_STRONG_TIERS = frozenset({"exact", "exact_ci", "qualified"})
_WEAK_TIERS = frozenset({"tokens", "fuzzy"})
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
    include_fuzzy: bool = False,
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
        fuzzy_dropped = 0
        if not include_fuzzy and any(r.get("match") in _STRONG_TIERS for r in merged):
            kept = [r for r in merged if r.get("match") not in _WEAK_TIERS]
            fuzzy_dropped = len(merged) - len(kept)
            merged = kept
        skipped_shown, skipped_total, skipped_truncated = _echoed(skipped)
        # The rows come from several graph databases and are searched afresh on every
        # call, so the stamp is a digest of the rows themselves, and of the repos left
        # out: a cursor made on a partial answer is only honoured for the same partial one.
        page = paginate(
            merged, max_results, _MAX_CROSS_REPO_RESULTS, cursor,
            scope=make_scope(
                "cross_repo_search_tool", query=query, kind=kind, limit=limit,
                repos=sorted(set(repos)) if repos else None, include_fuzzy=include_fuzzy,
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
        if fuzzy_dropped:
            notes += (
                f"; {fuzzy_dropped} fuzzy/tokens row(s) hidden because exact matches exist "
                "(pass include_fuzzy=True to see them)"
            )
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
            "fuzzy_dropped": fuzzy_dropped,
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


#: Notes that say the match rests on a guess about the route or the origin.
_LOW_CONFIDENCE_NOTES = frozenset({"ember_data_convention", "base_url_not_literal"})


def _js_confidence(call: fl.ClientCall, result: fl.LinkResult, notes: list[str]) -> str:
    """``low``, ``medium`` or ``high`` for a matched or ambiguous JavaScript call.

    A wildcard or variable match, a route derived from a model name and a base URL that is
    not a literal are guesses: ``low``. ``receiver_inferred`` alone (the client instance was
    recognised by its name) is ``medium``, and ``high`` when one server location answers
    the call by exact method and a literal, untemplated route. Several handlers of one
    path (``several_handlers_in_repo``) are never better than ``medium``.
    """
    if result.confidence == "low" or _LOW_CONFIDENCE_NOTES.intersection(notes):
        return "low"
    if result.status != fl.MATCHED or len(result.candidates) != 1:
        return "medium"
    server = result.candidates[0]
    exact_method = (
        server.http_method.upper() == call.http_method.upper()
        and not call.method_dynamic
    )
    literal_route = not call.route_dynamic and "{" not in call.route
    if set(notes) <= {"receiver_inferred"} and exact_method and literal_route:
        return "high"
    return "medium"


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


def _terraform_links(
    repos: list[str] | None,
    module_repo: str | None,
    module_path: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="terraform"`` branch: remote module sources joined to registered repos."""
    return _infra_links(
        "terraform", "remote Terraform module source(s)", tl.STATUSES, tl.read_module_edges,
        tl.module_folder_exists, tl.resolve_links, "module_repo", "module_path",
        repos, module_repo, module_path, status, max_results, infrastructure=True,
        cursor=cursor,
    )


def _workflow_links(
    repos: list[str] | None,
    target_repo: str | None,
    target_path: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="workflow"`` branch: GitHub Actions ``uses`` joined to registered repos."""
    return _infra_links(
        "workflow", "GitHub Actions reference(s)", wl.STATUSES, wl.read_uses_edges,
        wl.path_exists, wl.resolve_links, "target_repo", "target_path",
        repos, target_repo, target_path, status, max_results, cursor=cursor,
    )


def _stepfunction_links(
    repos: list[str] | None,
    target_name: str | None,
    target: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="stepfunction"`` branch: what state machines call, joined to its owner."""
    definitions = sl.Definitions()
    result = _infra_links(
        "stepfunction", "Step Functions target(s)", sl.STATUSES, sl.read_target_edges,
        definitions.defines, sl.resolve_links, "target_name", "target",
        repos, target_name, target, status, max_results, infrastructure=True,
        edge_filter=_edge_target_filter, prefetch=definitions.prefetch, cursor=cursor,
    )
    for row in result["results"]:
        for key in ("machine", "state", "target_kind", "target_subkind", "target_repo"):
            if isinstance(row.get(key), str):
                row[key] = _sanitize_name(row[key])
        if isinstance(row.get("candidates"), list):
            row["candidates"] = [_sanitize_name(str(name)) for name in row["candidates"]]
    return result


def _awssdk_links(
    repos: list[str] | None,
    target_name: str | None,
    target: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="awssdk"`` branch: AWS resources application code calls, joined to owners."""
    definitions = sl.Definitions()
    result = _infra_links(
        "awssdk", "AWS SDK call(s)", al.STATUSES, al.read_edges,
        definitions.defines, al.resolve_links, "target_name", "target",
        repos, target_name, target, status, max_results, infrastructure=True,
        read_context=al.read_declare_edges,
        edge_filter=_edge_target_filter, prefetch=definitions.prefetch, cursor=cursor,
    )
    for row in result["results"]:
        # Every string here was read from a repository's files: none is passed on as written.
        for key, value in list(row.items()):
            if isinstance(value, str):
                row[key] = _sanitize_name(value)
            elif isinstance(value, list):
                row[key] = [_sanitize_name(str(item)) for item in value]
            elif isinstance(value, dict):
                row[key] = {
                    _sanitize_name(str(k)): v if isinstance(v, int) else _sanitize_name(str(v))
                    for k, v in value.items()
                }
    return result


def _amqp_links(
    repos: list[str] | None,
    exchange: str | None,
    routing_key: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="amqp"`` branch: publishers and listeners of one exchange, across repos.

    ``exchange`` is required (``service``); ``routing_key`` (``route``) keeps the rows linked
    to that key by the rules of :func:`~code_review_graph.amqp_join.link_kind`: a listener's
    ``binding_key`` is the pattern for a publisher's key, and for a publisher row the key given
    is the pattern. Read-only; the graphs are opened read-only, one at a time, inside the
    call budget (repos left over are named in ``skipped_repos``). The rows are paged like those
    of the other origins: ``max_results`` is the page size and ``next_cursor`` the next page.
    """
    from ..registry import Registry

    if not exchange or not exchange.strip():
        raise ValueError("origin=amqp needs service: the exchange name")
    roles = ("publisher", "listener")
    if status is not None and status not in roles:
        raise ValueError(f"status must be one of: {', '.join(roles)}")
    channel = exchange.strip()
    if channel.startswith(aj.EXCHANGE_PREFIX):
        channel = channel[len(aj.EXCHANGE_PREFIX):]
    all_repos = Registry().list_repos()
    selected: list[dict[str, Any]] = all_repos
    unknown_shown: list[str] = []
    if repos:
        selected, unknown, _ambiguous = _select_repos(all_repos, repos)
        unknown_shown, _total, _cut = _echoed(unknown)
    budget = cross_repo_budget_seconds()
    started = _now()
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    unreadable: list[str] = []
    without_amqp: list[str] = []
    counts: dict[str, int] = {}
    for entry in selected:
        label = _sanitize_name(_repo_label(entry))
        if budget and _now() - started > budget:
            skipped.append(label)
            continue
        store = tl.open_store(entry)
        if store is None:
            unreadable.append(label)
            continue
        try:
            if not aj.has_amqp_edges(store):
                without_amqp.append(label)
                continue
            found = list(aj.read_channel_edges(store, channel))
        except (sqlite3.Error, GraphStoreError) as exc:
            logger.warning("Cannot read AMQP edges of %s: %s", entry["path"], exc)
            unreadable.append(label)
            continue
        finally:
            store.close()
        root = Path(entry["path"])
        for edge in found:
            if routing_key:
                if edge["role"] == "listener":
                    how = aj.link_kind(edge["binding_key"], routing_key)
                else:
                    how = aj.link_kind(routing_key, edge["routing_key"])
                if how is None:
                    continue
            else:
                how = None
            counts[edge["role"]] = counts.get(edge["role"], 0) + 1
            if status and edge["role"] != status:
                continue
            row: dict[str, Any] = {
                "role": edge["role"], "repo": label, "exchange": _sanitize_name(channel),
                "source": _sanitize_name(str(edge["source"]).split("::", 1)[-1]),
                "file": _sanitize_name(_relative_to(edge["file_path"], root)),
                "line": edge["line"],
            }
            for key in ("routing_key", "binding_key", "queue"):
                if isinstance(edge.get(key), str) and edge[key]:
                    row[key] = _sanitize_name(edge[key])
            if how:
                row["matched_via"] = how
            rows.append(row)
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin="amqp",
            repos=sorted(repos) if repos else None,
            exchange=exchange, routing_key=routing_key, status=status,
        ),
        stamp=content_stamp([rows, skipped]),
    )
    shown, total = page.items, page.total
    result: dict[str, Any] = {
        "status": "ok",
        "summary": (
            f"{total} AMQP publisher/listener row(s) for exchange "
            f"'{_sanitize_name(channel)}'"
            + (f" linked to key '{_sanitize_name(routing_key)}'" if routing_key else "")
            + f" across {len(selected)} repo(s)"
            + shown_range(page)
        ),
        "origin": "amqp",
        "results": shown,
        "counts": counts,
        "total": total,
        "truncated": page.truncated or bool(skipped),
        "repos_searched": len(selected) - len(skipped),
        "unreadable": unreadable,
        **page.fields(),
    }
    if without_amqp:
        result["repos_without_amqp"] = _bounded(
            without_amqp, _MAX_ECHOED_NAMES, _MAX_ECHOED_NAMES,
        )[0]
        result["repos_without_amqp_total"] = len(without_amqp)
    if skipped:
        result["skipped_repos"] = skipped
    if unknown_shown:
        result["unknown"] = unknown_shown
    return result


def _relative_to(file_path: str, root: Path) -> str:
    try:
        return str(Path(file_path).relative_to(root))
    except ValueError:
        return file_path


SLOW_CALL_ENV = "CRG_CROSS_REPO_SLOW_SECONDS"
DEFAULT_SLOW_CALL_SECONDS = 10.0


def _slow_call_seconds() -> float:
    """Seconds from which a cross-repo call is logged as a warning (default 10).

    ``0`` logs every call as a warning; an unreadable or negative value uses the default.
    """
    raw = os.environ.get(SLOW_CALL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SLOW_CALL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SLOW_CALL_SECONDS
    return value if value >= 0 else DEFAULT_SLOW_CALL_SECONDS


def _edge_target_filter(
    target_name: str | None, target: str | None,
) -> Callable[[dict[str, Any]], bool]:
    """``edge -> bool`` that keeps what the ``target_name`` and ``target`` filters keep.

    The ``stepfunction`` and ``awssdk`` rows carry ``target_name`` and ``target`` straight from
    the edge (``service/name``), so rejecting an edge here leaves exactly the rows the filters
    leave on the finished rows. Checking early saves the owner lookup of the rest.
    """
    wanted = target.strip("/") if target else ""

    def keep(edge: dict[str, Any]) -> bool:
        if target_name and edge["name"] != target_name:
            return False
        if wanted:
            full = f"{edge['service']}/{edge['name']}"
            return full == wanted or full.startswith(wanted + "/")
        return True

    return keep


def _infra_links(
    origin: str,
    noun: str,
    statuses: tuple[str, ...],
    read_edges: Any,
    exists_in_repo: Any,
    resolve: Any,
    repo_key: str,
    path_key: str,
    repos: list[str] | None,
    target_repo: str | None,
    target_path: str | None,
    status: str | None,
    max_results: int,
    infrastructure: bool = False,
    read_context: Any = None,
    edge_filter: Any = None,
    prefetch: Any = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Shared body of the ``terraform``, ``workflow``, ``stepfunction`` and ``awssdk`` origins.

    One read-only join each. *exists_in_repo* is ``(store, repo path, what) -> bool``.
    *read_context*, when given, reads from the registered repos that were not selected what
    only helps to find an owner (``resolve`` then gets it as its fourth argument).

    Repos are read on up to ``CRG_CROSS_REPO_WORKERS`` threads and what is read about a repo is
    kept while its graph file is unchanged (``repo_cache``). *edge_filter*, when given, is
    ``(target_name, target) -> (edge -> bool)``; it lets ``resolve`` skip the owner lookup of an
    edge the two filters would drop. *prefetch*, when given, is ``(all repos, workers) -> None``
    and reads ahead what ``exists_in_repo`` needs from every repo.

    The rows are paged: *max_results* (at most 100) is the page size, and a cut list carries
    ``next_cursor``, which returns the next page when passed back as *cursor*. A cursor is
    tied to the query and to the rows it was made on, so it is refused when either changed.
    """
    from ..registry import Registry

    if status is not None and status not in statuses:
        raise ValueError(f"status must be one of: {', '.join(statuses)}")
    all_repos = Registry().list_repos()
    selected: list[dict[str, Any]] = all_repos
    unknown_shown: list[str] = []
    if repos:
        selected, unknown, _ambiguous = _select_repos(all_repos, repos)
        unknown_shown, _total, _cut = _echoed(unknown)
    by_id = {id(e): i for i, e in enumerate(all_repos)}
    budget = cross_repo_budget_seconds()
    started = _now()
    workers = cross_repo_workers()
    timings: dict[str, float] = {}

    def out_of_budget() -> bool:
        return bool(budget) and _now() - started > budget

    def read_one(entry: dict[str, Any], reader: Any, what: str) -> Optional[list[dict[str, Any]]]:
        """What *reader* finds in one repo's graph, or ``None`` when it cannot be read."""
        store = tl.open_store(entry)
        if store is None:
            return None
        try:
            return reader(store)  # type: ignore[no-any-return]
        except (sqlite3.Error, GraphStoreError) as exc:
            logger.warning("Cannot read %s %s of %s: %s", origin, what, entry["path"], exc)
            return None
        finally:
            store.close()

    def edges_of(entry: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
        return MEMO.get(
            f"{origin}-edges", entry, lambda: read_one(entry, read_edges, "references"),
            cacheable=lambda found: found is not None,
        )

    edges_by_repo: dict[int, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    unreadable: list[str] = []
    phase = _now()
    for entry, found in zip(
        selected, parallel_map_budget(edges_of, selected, workers, out_of_budget),
    ):
        if found is SKIPPED:
            skipped.append(_sanitize_name(_repo_label(entry)))
        elif found is None:
            unreadable.append(_sanitize_name(_repo_label(entry)))
        elif found:
            edges_by_repo[by_id[id(entry)]] = found
    timings["read"] = _now() - phase

    stores: dict[int, Optional[GraphStore]] = {}
    # ``Definitions`` can answer from what ``prefetch`` read, without opening the repo's graph.
    knows = getattr(getattr(exists_in_repo, "__self__", None), "knows", None)

    def folder_exists(index: int, subdir: Optional[str]) -> bool:
        repo_path = Path(all_repos[index]["path"])
        if knows is not None and knows(repo_path):
            return bool(exists_in_repo(None, repo_path, subdir))
        if index not in stores:
            stores[index] = tl.open_store(all_repos[index])
        store = stores[index]
        if store is None:
            return False
        try:
            return bool(exists_in_repo(store, repo_path, subdir))
        except sqlite3.Error:
            return False

    context: dict[int, list[dict[str, Any]]] = {}
    phase = _now()
    if read_context is not None:
        chosen = {id(e) for e in selected}
        others = [e for e in all_repos if id(e) not in chosen]

        def context_of(entry: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
            return MEMO.get(
                f"{origin}-context", entry, lambda: read_one(entry, read_context, "context"),
                cacheable=lambda found: found is not None,
            )

        for entry, found in zip(
            others, parallel_map_budget(context_of, others, workers, out_of_budget),
        ):
            if found is not SKIPPED and found:
                context[by_id[id(entry)]] = found
    timings["context"] = _now() - phase

    keep = (
        edge_filter(target_repo, target_path)
        if edge_filter is not None and (target_repo or target_path) else None
    )
    phase = _now()
    if prefetch is not None and any(
        keep is None or keep(edge) for found in edges_by_repo.values() for edge in found
    ):
        prefetch(all_repos, workers)
    timings["prefetch"] = _now() - phase
    phase = _now()
    resolve_args: tuple[Any, ...] = (edges_by_repo, all_repos, folder_exists)
    if read_context is not None:
        resolve_args += (context,)
    try:
        rows = resolve(*resolve_args, keep=keep) if keep is not None else resolve(*resolve_args)
    finally:
        for opened in stores.values():
            if opened is not None:
                opened.close()
    timings["resolve"] = _now() - phase
    if target_repo:
        rows = [r for r in rows if r[repo_key] == target_repo]
    if target_path:
        wanted = target_path.strip("/")
        rows = [
            r for r in rows
            if (r[path_key] or "") == wanted
            or (r[path_key] or "").startswith(wanted + "/")
        ]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if status:
        rows = [r for r in rows if r["status"] == status]
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin=origin,
            repos=sorted(repos) if repos else None,
            target_repo=target_repo, target_path=target_path, status=status,
        ),
        # The repos left out of a partial answer are part of the stamp, as for Feign.
        stamp=content_stamp([rows, skipped]),
    )
    shown, total = page.items, page.total
    for r in shown:
        for key in ("repo", "file", repo_key, path_key, "ref"):
            if isinstance(r.get(key), str):
                r[key] = _sanitize_name(r[key])
    result: dict[str, Any] = {
        "status": "ok",
        "summary": f"{total} {noun} across {len(selected)} repo(s)" + shown_range(page),
        "origin": origin,
        "results": shown,
        "counts": counts,
        "total": total,
        "truncated": page.truncated or bool(skipped),
        "repos_searched": len(selected) - len(skipped),
        "unreadable": unreadable,
        **page.fields(),
    }
    if skipped:
        result["skipped_repos"] = skipped
    if unknown_shown:
        result["unknown"] = unknown_shown
    phase = _now()
    if infrastructure:
        _attach_infrastructure(result, all_repos, selected, budget)
    timings["infrastructure"] = _now() - phase
    elapsed = _now() - started
    # The server does not configure INFO logging, so a slow call is logged as a warning.
    logger.log(
        logging.WARNING if elapsed >= _slow_call_seconds() else logging.INFO,
        "cross-repo %s: %d selected repo(s), %d worker(s), %s, total %.1fs",
        origin, len(selected), workers,
        ", ".join(f"{name} {seconds:.1f}s" for name, seconds in timings.items()),
        elapsed,
    )
    return result


def _attach_infrastructure(
    result: dict[str, Any], all_repos: list[dict[str, Any]], selected: list[dict[str, Any]],
    budget: float,
) -> None:
    """Add the per-repo ``infrastructure`` summary (see ``infra_join``) to a reply."""
    rules = sa.load_alias_rules()
    resolver = RepoNameResolver(all_repos)
    by_id = {id(e): i for i, e in enumerate(all_repos)}
    block = ij.build_infrastructure(
        all_repos, [by_id[id(e)] for e in selected], sa.FactsCache(all_repos), rules,
        resolver.lookup, budget, cross_repo_workers(),
    )
    result["infrastructure"] = block
    if block["repos_total"]:
        result["summary"] += (
            f"; infrastructure matched for {block['repos_total']} repo(s) "
            "(see infrastructure)"
        )
    if block["truncated"]:
        result["truncated"] = True


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
        origin: Only ``feign`` or only ``js`` calls. Default: both. ``terraform``
            lists the remote Terraform module sources the repos name instead (see
            below); ``service`` then filters by module repo name, ``route`` by
            module folder prefix, and ``status`` is one of ``matched``,
            ``module_not_found``, ``ambiguous``, ``repo_not_registered`` or ``registry_module``
            (a Terraform registry address, never matched to a repo). ``workflow``
            lists the GitHub Actions ``uses`` references (reusable workflows and actions)
            that point at another repo; ``service`` then filters by the target repo name,
            ``route`` by path prefix inside it, and ``status`` is one of ``matched``,
            ``path_not_found``, ``ambiguous``, ``repo_not_registered`` (same org, not
            indexed) or ``external_action`` (third-party owner). Local
            (``./``) and ``docker://`` references are not listed. ``stepfunction``
            lists what the Step Functions Task states call (a Lambda, an ECS task
            definition, an SNS topic, another state machine, ...) and the registered
            repo that defines it; ``service`` then filters by target name, ``route`` by
            kind (``lambda``) or ``kind/name``, and ``status`` is one of ``matched``,
            ``not_found`` or ``ambiguous``.

    ``amqp`` lists the publishers and listeners of one RabbitMQ exchange across the selected
    repos: ``service`` is the exchange (required), ``route`` an optional key and ``status``
    ``publisher`` or ``listener``. With ``route`` a listener row is kept when its ``binding_key``
    matches that key and a publisher row when its ``routing_key`` matches it, using AMQP topic
    wildcards (``*`` one word, ``#`` zero or more) only in a binding key that holds one;
    ``matched_via`` is ``exact_key``, ``topic_pattern`` or ``key_unknown``. The exchange type is
    never inferred. It can only find links once both repos carry the exchange edges: a repo
    indexed before they existed is named in ``repos_without_amqp`` and shows nothing until
    rebuilt. It pages like the other origins (``max_results`` is the page size) and does not
    use ``limit`` or the infrastructure summary.

    A Feign service name that is not a registered repo name resolves through the optional
    alias file ``CRG_SERVICE_ALIASES_FILE`` (``{"alias": "repo", "prefix:p-": "repo"}``) or
    the names the repos declare (``spring.application.name``, ECS service, ECR repository);
    rows carry ``matched_via``, and a name reaching several repos is ``ambiguous`` with
    ``candidate_repos``. The ``terraform``, ``stepfunction`` and ``awssdk`` origins also return
    an ``infrastructure`` object relating each selected repo to the ECR, ECS, stack and declared
    resources that belong to it (see ``docs/SERVICE_ALIASES.md``).

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
        The ``terraform``, ``workflow``, ``stepfunction``, ``awssdk`` and ``amqp`` origins page the
        same way: ``max_results`` (at most 100) is the page size, ``limit`` is not used, and
        the cursor is refused when the query or the rows changed.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")
    infra_origins = ("terraform", "workflow", "stepfunction", "awssdk", "amqp")
    if status is not None and origin not in infra_origins and status not in fl.STATUSES:
        raise ValueError(f"status must be one of: {', '.join(fl.STATUSES)}")
    if origin is not None and origin not in ("feign", "js", *infra_origins):
        raise ValueError(
            "origin must be one of: feign, js, terraform, workflow, stepfunction, awssdk, amqp"
        )
    if origin == "amqp":
        return _amqp_links(repos, service, route, status, max_results, cursor)
    if origin == "awssdk":
        return _awssdk_links(repos, service, route, status, max_results, cursor)
    if origin == "stepfunction":
        return _stepfunction_links(repos, service, route, status, max_results, cursor)
    if origin == "terraform":
        return _terraform_links(repos, service, route, status, max_results, cursor)
    if origin == "workflow":
        return _workflow_links(repos, service, route, status, max_results, cursor)

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

        alias_rules = sa.load_alias_rules()
        facts = sa.FactsCache(all_repos)
        identity_holder: list[sa.IdentityIndex] = []

        def identity() -> sa.IdentityIndex:
            # Read lazily: only a service name nothing else resolved needs every repo's facts.
            if not identity_holder:
                facts.prefetch(range(len(all_repos)), cross_repo_workers())
                identity_holder.append(sa.IdentityIndex(all_repos, facts))
            return identity_holder[0]

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
                    resolution = sa.resolve_service(
                        call.service, resolver.lookup, folded, alias_rules, identity,
                    )
                    if resolution.candidates:
                        result = fl.LinkResult(fl.AMBIGUOUS, resolution.reason)
                    elif not resolution.indexes:
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
                    elif result.reason == fl.SAME_REPO_REASON:
                        row_notes.append(result.reason)
                    if row_notes and result.status in (fl.MATCHED, fl.AMBIGUOUS):
                        row["confidence"] = _js_confidence(call, result, row_notes)
                        row["confidence_notes"] = row_notes
                if result.status == fl.CASE_MISMATCH:
                    row["confidence"] = "low"
                if result.reason:
                    row["reason"] = result.reason
                if resolution is not None and resolution.how:
                    row["target_resolution"] = resolution.how
                if resolution is not None and resolution.matched_via:
                    row["matched_via"] = resolution.matched_via
                if resolution is not None and resolution.candidates:
                    row["candidate_repos"] = [
                        _sanitize_name(_repo_label(all_repos[i]))
                        for i in resolution.candidates[:_MAX_CANDIDATES_PER_ROW]
                    ]
                    row["candidate_repos_total"] = len(resolution.candidates)
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
