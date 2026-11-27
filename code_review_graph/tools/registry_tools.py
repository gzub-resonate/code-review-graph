"""Tools 21, 22, 23: list_repos_func, cross_repo_search_func, cross_repo_requests_func."""

from __future__ import annotations

import copy
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from typing import Any, Optional

from .. import amqp_exchanges as ax
from .. import amqp_join as aj
from .. import aws_sdk_links as al
from .. import external_hosts as eh
from .. import feign_links as fl
from .. import infra_join as ij
from .. import js_route_leads as jl
from .. import service_aliases as sa
from .. import stepfunction_links as sl
from .. import terraform_links as tl
from .. import trigger_links as trl
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
from ..parser import is_test_file
from ..repo_aliases import RepoNameResolver, load_repo_aliases
from ..repo_cache import (
    INFRASTRUCTURE_RESULTS,
    MEMO,
    SKIPPED,
    parallel_map_budget,
    submit_shared,
)
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

#: A searched-repo list at most this long is echoed without being asked for.
_REPOS_LISTED_MAX = 10

_INFRASTRUCTURE_FIRST_PAGE_ONLY = (
    "infrastructure describes the selected repos, not these rows, and is returned on the "
    "first page only; call again without a cursor to read it"
)

#: Queue-only names echoed in the ``queues`` section of an AMQP exchange list.
_MAX_QUEUE_ROWS = 100

_INFRASTRUCTURE_NO_MATCHES = (
    "no rows matched the filter, so there is no infrastructure to describe and the block is "
    "omitted; it describes the repos of the matched rows, never other repos"
)

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


def _searched_fields(names: list[str], include_repo_list: bool) -> dict[str, Any]:
    """``repos_searched_total`` always; ``repos_searched`` (the names) only when short or asked.

    A registry of 100+ repos made the name list ~2.5 KB of every reply. The count says how
    many were searched; ``include_repo_list=True`` returns the names.
    """
    fields: dict[str, Any] = {"repos_searched_total": len(names)}
    if include_repo_list or len(names) <= _REPOS_LISTED_MAX:
        fields["repos_searched"] = names
    return fields


def _listed(names: list[str]) -> list[str]:
    """Sanitised names, never cut: used for ``skipped_repos``, which the caller must see whole."""
    return [_sanitize_name(name) for name in names]


#: Match tiers, best first. ``cross_repo_search_tool`` orders its merged rows by
#: tier before anything else, because the scores of different repos are not
#: comparable and an exact name in one repo must not wait behind another repo's
#: weak rank-1 hit.
_MATCH_TIERS = ("exact", "exact_ci", "qualified", "prefix", "tokens", "fuzzy")
_TIER_ORDER = {name: rank for rank, name in enumerate(_MATCH_TIERS)}
#: Tiers that make weaker rows noise, and the rows hidden then (see ``include_fuzzy``).
#: With no exact hit, at most this many ``fuzzy`` rows are kept (``include_fuzzy`` lifts it).
_FUZZY_CAP_NO_EXACT = 10
_MATCH_MODES = ("fuzzy", "exact")
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


class _RepoRows(list[tuple[int, str, dict[str, Any]]]):
    """A repo's search rows, and whether the per-repo ``limit`` left matches out (``more``)."""

    more: bool = False


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
    # One row more than the limit is asked for, only to learn whether the limit cut anything;
    # the ranking below sees exactly the rows it always did.
    fetched = hybrid_search(store, query, kind=kind, limit=limit + 1)
    more = len(fetched) > limit
    rows = fetched[:limit]
    seen = {row.get("qualified_name") for row in rows}
    for row in _lookup_rows(store, query, kind, limit):
        if row["qualified_name"] not in seen:
            seen.add(row["qualified_name"])
            rows.append(row)
    tiered = [(rank, _match_tier(query, row), row) for rank, row in enumerate(rows)]
    tiered.sort(key=lambda item: (_TIER_ORDER[item[1]], item[0]))
    kept = _RepoRows(tiered[:limit])
    kept.more = more or len(tiered) > limit
    return kept


class _SearchedRows(list[tuple[int, int, int, dict[str, Any]]]):
    """The rows one repo contributed; ``more`` is set when ``limit`` cut matches off."""

    more: bool = False


def _has_embeddings(store: GraphStore) -> bool:
    """Whether the graph's database holds embedding vectors (one indexed probe, no writes)."""
    try:
        conn = store._conn
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
        ).fetchone() is None:
            return False
        return conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return True


def _open_for_search(db_path: Path, *, lookup_only: bool = False) -> GraphStore:
    """Open a repo's graph for a search without the writes a normal open makes.

    A normal open runs the schema and migration statements and, for the embedding search, opens
    a second connection that creates its table: every one of those takes the write lock, so on
    a repo the indexer is writing the open itself waits, 100+ times in a row. A read-only open
    does none of that. The embedding search needs the writable open, so a graph that holds
    vectors (and any graph a read-only open refuses, such as an older schema) is opened the
    normal way: the answers are the ones they always were. ``lookup_only`` never needs vectors.
    """
    try:
        store = GraphStore(db_path, read_only=True)
    except GraphStoreError:
        return GraphStore(str(db_path))
    if lookup_only or not _has_embeddings(store):
        return store
    store.close()
    return GraphStore(str(db_path))


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
        store = _open_for_search(db_path)
        try:
            alias = repo_entry.get("alias", repo_path.name)
            rows: list[tuple[int, int, int, dict[str, Any]]] = _SearchedRows()
            found = _search_repo(store, query, kind, limit)
            rows.more = bool(getattr(found, "more", False))  # type: ignore[attr-defined]
            for local_rank, match, row in found:
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
            store = _open_for_search(db_path, lookup_only=True)
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


#: ``search key -> repo paths the time budget kept out of that search``, newest last. A repeat of
#: the same first-page search starts with them, so retrying a call that ran out of time makes
#: progress on the repos it never reached instead of re-reading the ones it already did.
_UNSEARCHED: dict[tuple[Any, ...], list[str]] = {}
_UNSEARCHED_LOCK = threading.Lock()
_UNSEARCHED_KEYS_MAX = 32


def _search_key(
    selected: list[dict[str, Any]], query: str, kind: str | None, limit: int,
    repos: list[str] | None,
) -> tuple[Any, ...]:
    return (query, kind, limit, tuple(sorted(repos)) if repos else None,
            tuple(str(e["path"]) for e in selected))


def _retry_order(
    selected: list[dict[str, Any]], query: str, kind: str | None, limit: int,
    repos: list[str] | None, cursor: str | None,
) -> list[int]:
    """Indexes into *selected* in the order they are started: registry order, except that a
    repeat of a first-page search that skipped repos tries those first. A call with a cursor
    keeps registry order, so its pages line up with the page that made the cursor."""
    order = list(range(len(selected)))
    if cursor:
        return order
    with _UNSEARCHED_LOCK:
        pending = set(_UNSEARCHED.get(_search_key(selected, query, kind, limit, repos), ()))
    if not pending:
        return order
    first = [i for i in order if str(selected[i]["path"]) in pending]
    return first + [i for i in order if str(selected[i]["path"]) not in pending]


def _remember_skipped(
    selected: list[dict[str, Any]], query: str, kind: str | None, limit: int,
    repos: list[str] | None, cursor: str | None, left: list[tuple[int, dict[str, Any]]],
) -> None:
    if cursor:
        return
    key = _search_key(selected, query, kind, limit, repos)
    with _UNSEARCHED_LOCK:
        _UNSEARCHED.pop(key, None)
        if left:
            _UNSEARCHED[key] = [str(entry["path"]) for _i, entry in left]
            while len(_UNSEARCHED) > _UNSEARCHED_KEYS_MAX:
                del _UNSEARCHED[next(iter(_UNSEARCHED))]


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    max_results: int = 50,
    repos: list[str] | None = None,
    cursor: str | None = None,
    include_fuzzy: bool = False,
    include_repo_list: bool = False,
    match: str = "fuzzy",
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
        include_fuzzy: Keep ``fuzzy`` and ``tokens`` rows when a strong match exists.
        include_repo_list: Return ``repos_searched`` (the names) even when more than 10
            repos were searched. ``repos_searched_total`` is always the count.
        match: ``"fuzzy"`` (default) or ``"exact"``. ``"exact"`` returns only ``exact``,
            ``exact_ci`` and ``qualified`` rows; the rest are counted in ``fuzzy_dropped``.
            With ``"fuzzy"`` and no exact hit, ``fuzzy`` rows that share fewer than half of the
            query's words are dropped and at most 10 are kept (``include_fuzzy=True`` keeps
            all). ``exact`` in the reply is ``"found"`` or ``"none"``.

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
    if match not in _MATCH_MODES:
        raise ValueError(f"match must be one of: {', '.join(_MATCH_MODES)}")

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

        # Up to ``workers`` repos are searched at once, on the pool every cross-repo call shares
        # (so concurrent calls do not each start their own threads). The budget is checked
        # here, on the calling thread, each time a worker is free to start the next repo, so
        # what is skipped does not depend on which search finishes first. A repo left out by a
        # first-page call that ran out of budget is tried first by the next such call.
        order = _retry_order(selected, query, kind, limit, repos, cursor)
        for position, repo_index in enumerate(order):
            repo_entry = selected[repo_index]
            while len(in_flight) >= workers:
                reap(wait(in_flight, return_when=FIRST_COMPLETED).done)
            # The first repo always runs, so a call makes progress; after that no
            # new repo is started once the budget is spent, and each one left is named.
            if position and _out_of_budget(deadline):
                left_over = sorted(order[position:])
                skipped.extend(_repo_label(selected[i]) for i in left_over)
                left = [(i, selected[i]) for i in left_over]
                break
            future = submit_shared(
                _search_one_repo, repo_index, repo_entry, query, kind, limit,
            )
            in_flight[future] = repo_index
        reap(wait(in_flight).done)
        _remember_skipped(selected, query, kind, limit, repos, cursor, left)

        capped: list[str] = []
        # Whatever order the searches finished in, the rows go in registry order.
        for repo_index in sorted(outcomes):
            alias, rows = outcomes[repo_index]
            searched_repos.append(alias)
            ranked_results.extend(rows)
            if getattr(rows, "more", False):
                capped.append(alias)

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
        has_exact = any(r.get("match") in _STRONG_TIERS for r in merged)
        if match == "exact":
            kept = [r for r in merged if r.get("match") in _STRONG_TIERS]
            fuzzy_dropped = len(merged) - len(kept)
            merged = kept
        elif not include_fuzzy and has_exact:
            kept = [r for r in merged if r.get("match") not in _WEAK_TIERS]
            fuzzy_dropped = len(merged) - len(kept)
            merged = kept
        elif not include_fuzzy:
            # No exact hit: the weak list is all there is, and an unmatched name returned
            # hundreds of rows. Keep the ``prefix``/``tokens`` rows and the few ``fuzzy`` rows
            # that share at least half of the query's words.
            wanted = _name_tokens(query)
            need = (len(wanted) + 1) // 2 if len(wanted) > 1 else 0
            kept = []
            fuzzy_kept = 0
            for row in merged:
                if row.get("match") == "fuzzy":
                    shared = len(wanted & _name_tokens(str(row.get("name") or "")))
                    if shared < need or fuzzy_kept >= _FUZZY_CAP_NO_EXACT:
                        continue
                    fuzzy_kept += 1
                kept.append(row)
            fuzzy_dropped = len(merged) - len(kept)
            merged = kept
        skipped_shown, skipped_total, skipped_truncated = _listed(skipped), len(skipped), False
        # The rows come from several graph databases and are searched afresh on every
        # call, so the stamp is a digest of the rows themselves, and of the repos left
        # out: a cursor made on a partial answer is only honoured for the same partial one.
        page = paginate(
            merged, max_results, _MAX_CROSS_REPO_RESULTS, cursor,
            scope=make_scope(
                "cross_repo_search_tool", query=query, kind=kind, limit=limit,
                repos=sorted(set(repos)) if repos else None, include_fuzzy=include_fuzzy,
                match=match,
            ),
            stamp=make_scope("cross_repo_search_rows", rows=merged, skipped=skipped),
        )
        all_results, total = page.items, page.total
        truncated = page.truncated or bool(skipped) or bool(capped)
        capped_shown, capped_total, _capped_truncated = _echoed(capped)

        # A dropped or duplicated name reaches the summary too. The
        # all-unknown branch already said so there, and CLAUDE.md trains
        # agents to read the summary first, so both branches must report it.
        notes = ""
        if unknown_total:
            notes += f"; {unknown_total} name(s) matched no repository"
        if ambiguous_total:
            notes += f"; {ambiguous_total} name(s) matched several"
        if not has_exact:
            notes += "; exact: none"
        if fuzzy_dropped and match == "exact":
            notes += f"; {fuzzy_dropped} non-exact row(s) hidden by match=exact"
        elif fuzzy_dropped and has_exact:
            notes += (
                f"; {fuzzy_dropped} fuzzy/tokens row(s) hidden because exact matches exist "
                "(pass include_fuzzy=True to see them)"
            )
        elif fuzzy_dropped:
            notes += (
                f"; {fuzzy_dropped} fuzzy row(s) dropped (fewer than half of the query's words, "
                f"or beyond {_FUZZY_CAP_NO_EXACT}; pass include_fuzzy=True to see them)"
            )
        if capped_total:
            notes += (
                f"; {capped_total} repo(s) have more matches than limit={limit} "
                "(see repos_capped; total is a lower bound, raise limit to see them)"
            )
        searched_of = len(searched_repos)
        coverage = (
            f"searched {searched_of} of {len(selected)} repos; "
            f"{skipped_total} skipped by the time budget"
        )
        if skipped_total:
            notes += (
                f"; {coverage} (budget {budget:g}s spent, {skipped_total} repo(s) not searched; "
                f"exact names were still looked up in {len(exact_looked_in)} of them; "
                "see skipped_repos; repeat the call to search "
                "the skipped repos first, raise CRG_CROSS_REPO_BUDGET_SECONDS or narrow 'repos')"
            )

        response = {
            "status": "ok",
            "summary": (
                f"Found {total}{'+' if capped_total else ''} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
                + shown_range(page)
                + notes
            ),
            "results": all_results,
            "total": total,
            "fuzzy_dropped": fuzzy_dropped,
            "exact": "found" if has_exact else "none",
            "truncated": truncated,
            **_searched_fields(searched_repos, include_repo_list),
            "skipped_repos": skipped_shown,
            "skipped_repos_total": skipped_total,
            "skipped_repos_truncated": skipped_truncated,
            "exact_lookup_repos_total": len(exact_looked_in),
            "repos_capped": capped_shown,
            "repos_capped_total": capped_total,
            **page.fields(),
        }
        if capped_total:
            response["total_is_lower_bound"] = True
        if skipped_total:
            response["truncated_reason"] = f"{coverage} (see skipped_repos)"
            response["repos_total"] = len(selected)
            response["summary"] = f"INCOMPLETE: {coverage}. " + response["summary"]
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
    path_prefix: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    """The ``origin="stepfunction"`` branch: what state machines call, joined to its owner."""
    from .. import not_found_reasons as nfr

    definitions = sl.Definitions()

    def annotate(rows, all_repos, _edges, _context):
        nfr.annotate_stepfunction(rows, definitions._by_repo, all_repos)

    result = _infra_links(
        "stepfunction", "Step Functions target(s)", sl.STATUSES, sl.read_target_edges,
        definitions.defines, sl.resolve_links, "target_name", "target",
        repos, target_name, target, status, max_results, infrastructure=True,
        edge_filter=_edge_target_filter, prefetch=definitions.prefetch, cursor=cursor,
        path_prefix=path_prefix, machine=machine, annotate=annotate,
    )
    if target_name and result["total"] == 0 and not result["counts"]:
        _machine_hint(result, definitions, target_name)
    for row in result["results"]:
        for key in ("machine", "state", "target_kind", "target_subkind", "target_repo"):
            if isinstance(row.get(key), str):
                row[key] = _sanitize_name(row[key])
        if isinstance(row.get("candidates"), list):
            row["candidates"] = [_sanitize_name(str(name)) for name in row["candidates"]]
    return result


def _machine_hint(result: dict[str, Any], definitions: Any, name: str) -> None:
    """Explain an empty ``service=<state machine>`` answer: this origin lists what states call."""
    from ..registry import Registry

    all_repos = Registry().list_repos()
    definitions.prefetch(all_repos, cross_repo_workers())
    key = ("states", "state-machine", "~" + re.sub(r"[-_]", "", name).lower())
    if any(key in definitions._by_repo.get(str(Path(e["path"])), {}) for e in all_repos):
        result["hint"] = (
            f"{name!r} is an indexed state machine. origin=stepfunction lists what Task "
            "states CALL (a Lambda, ECS task, topic or another machine), not what SDK code "
            "starts. For who starts or calls this machine use origin=awssdk route=states/"
            f"{name} (or the machine's callers)."
        )


def _awssdk_links(
    repos: list[str] | None,
    target_name: str | None,
    target: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
    path_prefix: str | None = None,
    source: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """The ``origin="awssdk"`` branch: AWS resources application code calls, joined to owners."""
    from .. import not_found_reasons as nfr

    definitions = sl.Definitions()

    def annotate(rows, all_repos, edges_by_repo, context):
        nfr.annotate_awssdk(rows, definitions._by_repo, all_repos, edges_by_repo, context)

    result = _infra_links(
        "awssdk", "AWS SDK call(s)", al.STATUSES, al.read_edges,
        definitions.defines, al.resolve_links, "target_name", "target",
        repos, target_name, target, status, max_results, infrastructure=True,
        read_context=al.read_declare_edges,
        edge_filter=_edge_target_filter, prefetch=definitions.prefetch, cursor=cursor,
        path_prefix=path_prefix, source=source, include_tests=include_tests,
        annotate=annotate,
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


def _amqp_repo_map(entry: dict[str, Any]) -> Optional[dict[str, list[dict[str, Any]]]]:
    """Every exchange or queue name of one repo's graph with its edges; ``{}`` without AMQP.

    ``None`` when the graph cannot be read. Kept while the graph file is unchanged, so a
    second AMQP call is answered from memory. The values are raw (not sanitised) and shared:
    callers copy what they change.
    """
    def load() -> Optional[dict[str, list[dict[str, Any]]]]:
        store = tl.open_store(entry)
        if store is None:
            return None
        try:
            if not aj.has_amqp_edges(store):
                return {}
            return {
                name: list(aj.read_channel_edges(store, name))
                for name in ax.list_exchange_names(store)
            }
        except (sqlite3.Error, GraphStoreError) as exc:
            logger.warning("Cannot read AMQP edges of %s: %s", entry["path"], exc)
            return None
        finally:
            store.close()

    return MEMO.get("amqp-edges", entry, load, cacheable=lambda found: found is not None)


_HINT_STOP = frozenset({"api", "e", "topic", "exchange", "service", "services", "the", "and"})


def _amqp_hint_order(entries: list[dict[str, Any]], channel: str | None) -> list[dict[str, Any]]:
    """*entries* with the repos whose name shares words with *channel* first (stable)."""
    if not channel:
        return entries
    words = {w for w in re.split(r"[^a-z0-9]+", channel.lower()) if len(w) > 2} - _HINT_STOP
    if not words:
        return entries

    def score(entry: dict[str, Any]) -> int:
        label = {w for w in re.split(r"[^a-z0-9]+", _repo_label(entry).lower()) if w}
        return -len(words & label)

    return sorted(entries, key=score)


class _AmqpScan:
    """What one read of the AMQP edges of the registry found, and what it could not read."""

    def __init__(self) -> None:
        self.loaded: list[tuple[str, bool, dict[str, list[dict[str, Any]]]]] = []
        self.skipped: list[str] = []
        self.skipped_in_scope: list[str] = []
        self.unreadable: list[str] = []
        self.without_amqp: list[str] = []
        self.total = 0

    @property
    def searched(self) -> int:
        return self.total - len(self.skipped)

    @property
    def incomplete(self) -> bool:
        """Some repo that could have changed a status was not (fully) read."""
        return bool(self.skipped or self.unreadable)

    def coverage(self, selected: int, scoped: bool) -> str:
        """The part of a summary that says which repos the numbers rest on."""
        where = (
            f" in {selected} selected repo(s)" if scoped else ""
        )
        if self.skipped:
            return (
                f"{where}; searched {self.searched} of {self.total} repos, "
                f"{len(self.skipped)} skipped by the time budget (statuses are incomplete)"
            )
        if scoped:
            return f"{where} (status computed over all {self.total} repo(s))"
        return f" across {self.total} repo(s)"


def _amqp_scan(
    all_repos: list[dict[str, Any]], selected: list[dict[str, Any]], scoped: bool,
    channel: str | None = None,
) -> _AmqpScan:
    """Read the AMQP edges of the selected repos, then (when *scoped*) of all the others.

    A status is a statement about every repo, so a ``repos`` scope only chooses what is
    reported. The selected repos are read first, so a spent time budget costs the status
    before it costs the rows asked for. Repos run on ``CRG_CROSS_REPO_WORKERS`` threads.
    """
    chosen = {id(e) for e in selected}
    rest = [e for e in all_repos if id(e) not in chosen] if scoped else []
    ordered = _amqp_hint_order(list(selected), channel) + _amqp_hint_order(rest, channel)
    scan = _AmqpScan()
    scan.total = len(ordered)
    budget = cross_repo_budget_seconds()
    started = _now()

    def out_of_budget() -> bool:
        return bool(budget) and _now() - started > budget

    for entry, found in zip(
        ordered, parallel_map_budget(_amqp_repo_map, ordered, cross_repo_workers(), out_of_budget),
    ):
        label = _sanitize_name(_repo_label(entry))
        in_scope = id(entry) in chosen
        if found is SKIPPED:
            scan.skipped.append(label)
            if in_scope:
                scan.skipped_in_scope.append(label)
        elif found is None:
            scan.unreadable.append(label)
        elif not found:
            if in_scope:
                scan.without_amqp.append(label)
        else:
            scan.loaded.append((label, in_scope, found))
    return scan



def _amqp_exchange_list(
    repos: list[str] | None,
    routing_key: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """``origin="amqp"`` with no exchange: every exchange, with who publishes and who listens.

    One row per exchange (sorted by name): ``publisher_count``, ``listener_count``,
    ``publisher_repos``, ``listener_repos``, the ``routing_keys`` publishers use and the
    ``binding_keys`` listeners bind with, and a ``status``: ``unlistened`` (publishers, no
    listener), ``unpublished`` (listeners, no publisher) or ``matched`` (both; the keys are not
    compared). ``status`` filters on those values; ``routing_key`` (``route``) keeps the
    exchanges with a routing or binding key containing it as a substring.

    ``repos`` chooses which exchanges are listed (those with an edge in the selected repos),
    not what a status rests on: counts, repos and ``status`` always cover every repo, and
    ``scoped_publisher_count`` / ``scoped_listener_count`` give the selected repos' share.
    A name only ever used as a queue is listed apart, in ``queues``. When the time budget leaves
    repos unread the summary says so and a status that could change is flagged
    ``status_confirmed: false``. Pages like the other origins.
    """
    from ..registry import Registry

    if status is not None and status not in ax.LIST_STATUSES:
        raise ValueError(
            f"without service (exchange), status must be one of: {', '.join(ax.LIST_STATUSES)}"
        )
    all_repos = Registry().list_repos()
    selected: list[dict[str, Any]] = all_repos
    unknown_shown: list[str] = []
    if repos:
        selected, unknown, _ambiguous = _select_repos(all_repos, repos)
        unknown_shown, _total, _cut = _echoed(unknown)
    scoped = bool(repos)
    scan = _amqp_scan(all_repos, selected, scoped)
    exchanges: dict[str, ax.Exchange] = {}
    for label, in_scope, amap in scan.loaded:
        for name, raw_edges in amap.items():
            edges = [
                {**e, "routing_key": _sanitize_name(e["routing_key"])
                 if isinstance(e.get("routing_key"), str) else None,
                 "binding_key": _sanitize_name(e["binding_key"])
                 if isinstance(e.get("binding_key"), str) else None,
                 "queue": _sanitize_name(e["queue"]) if isinstance(e.get("queue"), str) else None}
                for e in raw_edges
            ]
            ax.add_edges(
                exchanges.setdefault(_sanitize_name(name), ax.new_exchange()), label, edges,
                in_scope=in_scope,
            )
    ax.link_bound_queues(exchanges)
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    queues: list[dict[str, Any]] = []
    for name in sorted(exchanges):
        entry_data = exchanges[name]
        if not ax.has_role_edges(entry_data):
            continue  # only declared bindings name it
        if scoped and not ax.in_scope_edges(entry_data):
            continue
        if routing_key and not ax.has_key_containing(entry_data, routing_key):
            continue
        row = ax.render(name, entry_data, scoped=scoped)
        if scan.incomplete and row["status"] != ax.MATCHED:
            # A repo that was not read could hold the missing publisher or listener.
            row["status_confirmed"] = False
        if ax.is_queue(entry_data):
            # A name only used as a queue is not an exchange: no exchange status, no count.
            queues.append(row)
            continue
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if status and row["status"] != status:
            continue
        rows.append(row)
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin="amqp", mode="exchange_list",
            repos=sorted(repos) if repos else None, routing_key=routing_key, status=status,
        ),
        stamp=content_stamp([rows, scan.skipped]),
    )
    ordered = ", ".join(f"{counts[s]} {s}" for s in ax.LIST_STATUSES if s in counts)
    result: dict[str, Any] = {
        "status": "ok",
        "summary": (
            f"{sum(counts.values())} AMQP exchange(s)"
            + (f" with a key containing '{_sanitize_name(routing_key)}'" if routing_key else "")
            + scan.coverage(len(selected), scoped)
            + (f": {ordered}" if ordered else "")
            + (f"; {len(queues)} queue name(s) listed apart (see queues)" if queues else "")
            + shown_range(page)
        ),
        "origin": "amqp",
        "results": page.items,
        "counts": counts,
        "total": page.total,
        "truncated": page.truncated or bool(scan.skipped),
        "repos_searched": len(selected) - len(scan.skipped_in_scope),
        "repos_read": scan.searched,
        "unreadable": scan.unreadable,
        "status_basis": "partial" if scan.incomplete else "all_repos",
        **page.fields(),
    }
    if scan.skipped:
        result["truncated_reason"] = (
            f"time budget spent, {len(scan.skipped)} of {scan.total} repo(s) not read "
            "(see skipped_repos); statuses and counts leave them out"
        )
    if scoped:
        result["scoped_to"] = _bounded(
            [_sanitize_name(_repo_label(e)) for e in selected], _MAX_ECHOED_NAMES,
            _MAX_ECHOED_NAMES,
        )[0]
    if queues and not status:
        result["queues"] = queues[:_MAX_QUEUE_ROWS]
        result["queues_total"] = len(queues)
    if scan.without_amqp:
        result["repos_without_amqp"] = _bounded(
            scan.without_amqp, _MAX_ECHOED_NAMES, _MAX_ECHOED_NAMES,
        )[0]
        result["repos_without_amqp_total"] = len(scan.without_amqp)
    if scan.skipped:
        result["skipped_repos"] = scan.skipped
    if unknown_shown:
        result["unknown"] = unknown_shown
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
    is the pattern. Read-only; the graphs are opened read-only inside the call budget (repos
    left over are named in ``skipped_repos`` and the summary says how many). The rows are paged
    like those of the other origins: ``max_results`` is the page size and ``next_cursor`` the
    next page. ``repos`` chooses whose rows are listed; ``exchange_status`` and
    ``exchange_counts`` still cover every repo (``scoped_to`` names the scope), and
    ``exchange_status_confirmed`` is false when unread repos could change it.
    """
    from ..registry import Registry

    if not exchange or not exchange.strip():
        return _amqp_exchange_list(repos, routing_key, status, max_results, cursor)
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
    scoped = bool(repos)
    scan = _amqp_scan(all_repos, selected, scoped, channel)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    overall = {"publisher": 0, "listener": 0}
    paths = {_sanitize_name(_repo_label(e)): Path(e["path"]) for e in all_repos}
    for label, in_scope, amap in scan.loaded:
        found = [e for e in amap.get(channel, []) if e["role"] != aj.ROLE_BINDING]
        for edge in found:
            overall[edge["role"]] += 1
        if not in_scope:
            continue
        root = paths.get(label, Path("."))
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
            if edge.get("kind") == aj.KIND_QUEUE:
                row["kind"] = aj.KIND_QUEUE
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
        stamp=content_stamp([rows, scan.skipped]),
    )
    shown, total = page.items, page.total
    result: dict[str, Any] = {
        "status": "ok",
        "summary": (
            f"{total} AMQP publisher/listener row(s) for exchange "
            f"'{_sanitize_name(channel)}'"
            + (f" linked to key '{_sanitize_name(routing_key)}'" if routing_key else "")
            + scan.coverage(len(selected), scoped)
            + shown_range(page)
        ),
        "origin": "amqp",
        "results": shown,
        "counts": counts,
        "total": total,
        "truncated": page.truncated or bool(scan.skipped),
        "repos_searched": len(selected) - len(scan.skipped_in_scope),
        "repos_read": scan.searched,
        "unreadable": scan.unreadable,
        **page.fields(),
    }
    if overall["publisher"] or overall["listener"]:
        result["exchange_counts"] = overall
        result["exchange_status"] = ax.exchange_status(overall["publisher"], overall["listener"])
        result["exchange_status_confirmed"] = (
            not scan.incomplete or result["exchange_status"] == ax.MATCHED
        )
    elif scan.incomplete:
        result["exchange_status_confirmed"] = False
    if scoped:
        result["scoped_to"] = _bounded(
            [_sanitize_name(_repo_label(e)) for e in selected], _MAX_ECHOED_NAMES,
            _MAX_ECHOED_NAMES,
        )[0]
    if scan.skipped:
        result["truncated_reason"] = (
            f"time budget spent, {len(scan.skipped)} of {scan.total} repo(s) not read "
            "(see skipped_repos); rows and exchange_status leave them out"
        )
        result["skipped_repos"] = scan.skipped
    if scan.without_amqp:
        result["repos_without_amqp"] = _bounded(
            scan.without_amqp, _MAX_ECHOED_NAMES, _MAX_ECHOED_NAMES,
        )[0]
        result["repos_without_amqp_total"] = len(scan.without_amqp)
    if unknown_shown:
        result["unknown"] = unknown_shown
    return result


def _trigger_links(
    repos: list[str] | None,
    target: str | None,
    source: str | None,
    status: str | None,
    max_results: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The ``origin="trigger"`` branch: what invokes a Lambda, queue, state machine or topic.

    ``service`` filters the invoked side (name, ``service/name`` or Terraform label), ``route``
    the firing side (rule, queue, topic, bucket, route key). Either, both or neither may be
    given. ``status`` is ``resolved``, ``source_unresolved`` or ``target_unresolved``.
    """
    from ..registry import Registry

    if status is not None and status not in trl.STATUSES:
        raise ValueError(f"status must be one of: {', '.join(trl.STATUSES)}")
    all_repos = Registry().list_repos()
    selected: list[dict[str, Any]] = all_repos
    unknown_shown: list[str] = []
    if repos:
        selected, unknown, _ambiguous = _select_repos(all_repos, repos)
        unknown_shown, _total, _cut = _echoed(unknown)
    budget = cross_repo_budget_seconds()
    started = _now()

    def read(entry: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
        def load() -> Optional[list[dict[str, Any]]]:
            store = tl.open_store(entry)
            if store is None:
                return None
            try:
                return trl.read_triggers(store)
            except (sqlite3.Error, GraphStoreError) as exc:
                logger.warning("Cannot read triggers of %s: %s", entry["path"], exc)
                return None
            finally:
                store.close()

        return MEMO.get("trigger-rows", entry, load, cacheable=lambda found: found is not None)

    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    unreadable: list[str] = []
    counts: dict[str, int] = {}
    kinds: dict[str, int] = {}
    results = parallel_map_budget(
        read, selected, cross_repo_workers(),
        lambda: bool(budget) and _now() - started > budget,
    )
    for entry, found in zip(selected, results):
        label = _sanitize_name(_repo_label(entry))
        if found is SKIPPED:
            skipped.append(label)
            continue
        if found is None:
            unreadable.append(label)
            continue
        root = str(Path(entry["path"]))
        for raw in found:
            if not trl.matches(raw, target, source):
                continue
            counts[raw["status"]] = counts.get(raw["status"], 0) + 1
            kinds[raw["trigger_kind"]] = kinds.get(raw["trigger_kind"], 0) + 1
            if status and raw["status"] != status:
                continue
            rows.append(trl.finish(raw, label, root))
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin="trigger",
            repos=sorted(repos) if repos else None, target=target, source=source, status=status,
        ),
        stamp=content_stamp([rows, skipped]),
    )
    result: dict[str, Any] = {
        "status": "ok",
        "summary": f"{page.total} inbound trigger(s) across {len(selected)} repo(s)"
        + shown_range(page),
        "origin": "trigger",
        "results": page.items,
        "counts": counts,
        "trigger_kinds": kinds,
        "total": page.total,
        "truncated": page.truncated or bool(skipped),
        "repos_searched": len(selected) - len(skipped),
        "unreadable": unreadable,
        **page.fields(),
    }
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


def _source_matches(row_source: str, wanted: str) -> bool:
    """Whether a row's ``source`` symbol is *wanted*, its last segment, or lies below it."""
    return (
        row_source == wanted
        or row_source.rsplit(".", 1)[-1] == wanted
        or row_source.startswith(wanted + ".")
    )


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
    path_prefix: str | None = None,
    source: str | None = None,
    machine: str | None = None,
    include_tests: bool = True,
    annotate: Any = None,
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

    *path_prefix*, *source*, *machine* and *include_tests* filter the rows before ``counts``,
    ``total`` and paging, so a page and its cursor only ever hold rows that passed them.
    """
    from .. import not_found_reasons as nfr
    from ..registry import Registry

    reason_values = nfr.REASONS_BY_ORIGIN.get(origin, ())
    if status is not None and status not in statuses + reason_values:
        raise ValueError(
            f"status must be one of: {', '.join(statuses + reason_values)}"
        )
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
    if origin == "awssdk":
        for r in rows:
            r["is_test"] = is_test_file(r.get("file") or "")
    if annotate is not None:
        annotate(rows, all_repos, edges_by_repo, context)
    if path_prefix:
        wanted_path = path_prefix.strip("/")
        rows = [
            r for r in rows
            if (r.get("file") or "") == wanted_path
            or (r.get("file") or "").startswith(wanted_path + "/")
        ]
    if source:
        rows = [r for r in rows if _source_matches(str(r.get("source") or ""), source)]
    if machine:
        rows = [r for r in rows if r.get("machine") == machine]
    tests_hidden = 0
    if not include_tests:
        kept_rows = [r for r in rows if not r.get("is_test")]
        tests_hidden = len(rows) - len(kept_rows)
        rows = kept_rows
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    reason_counts = nfr.counts_by_reason(rows)
    if status:
        rows = [r for r in rows if r["status"] == status or r.get("not_found_reason") == status]
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin=origin,
            repos=sorted(repos) if repos else None,
            target_repo=target_repo, target_path=target_path, status=status,
            path_prefix=path_prefix, source=source, machine=machine,
            include_tests=include_tests,
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
        "summary": (
            f"{total} {noun} across {len(selected)} repo(s)" + shown_range(page)
            + (f"; {tests_hidden} test row(s) hidden (include_tests=True shows them)"
               if tests_hidden else "")
            + nfr.summary_clause(counts, reason_counts)
        ),
        "origin": origin,
        "results": shown,
        "counts": counts,
        "total": total,
        "truncated": page.truncated or bool(skipped),
        "repos_searched": len(selected) - len(skipped),
        "unreadable": unreadable,
        **page.fields(),
    }
    if reason_counts:
        result["counts_by_reason"] = reason_counts
    if origin == "awssdk":
        result["tests_hidden"] = tests_hidden
    if skipped:
        result["skipped_repos"] = skipped
    if unknown_shown:
        result["unknown"] = unknown_shown
    reasons: list[str] = []
    if page.truncated:
        reasons.append("rows cut at the page size (see next_cursor)")
    if skipped:
        reasons.append(
            f"time budget spent, {len(skipped)} repo(s) not read (see skipped_repos)"
        )
    phase = _now()
    # A filter narrows the answer to some repos; the block then describes those, not every
    # selected repo (which would present unrelated repos as part of the result).
    scoped = bool(target_repo or target_path or status)
    if infrastructure and scoped:
        owners = {
            r[key] for r in rows for key in ("repo", "target_repo", "module_repo", "client_repo")
            if isinstance(r.get(key), str)
        }
        described = [
            e for e in all_repos if _sanitize_name(_repo_label(e)) in {
                _sanitize_name(o) for o in owners
            }
        ]
    else:
        described = selected
    if infrastructure and scoped and not described:
        result["infrastructure_note"] = _INFRASTRUCTURE_NO_MATCHES
    elif infrastructure and page.offset == 0:
        _attach_infrastructure(result, all_repos, described, budget)
        if result["infrastructure"]["truncated"]:
            reasons.append(
                "the infrastructure block stopped at its repo cap or the time budget "
                "(see infrastructure.truncated, repos_not_checked)"
            )
    elif infrastructure:
        # The block describes the selected repos, not the rows, so a continuation page leaves
        # it out (the first page has it) instead of rebuilding it for every page.
        result["infrastructure_note"] = _INFRASTRUCTURE_FIRST_PAGE_ONLY
    timings["infrastructure"] = _now() - phase
    if result["truncated"] and reasons:
        result["truncated_reason"] = "; ".join(reasons)
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


def _file_signature(path: Optional[Path]) -> Any:
    """What a small input file looks like now, for a cache key (``None`` when it is absent)."""
    if path is None:
        return None
    try:
        info = path.expanduser().stat()
    except OSError:
        return (str(path), None)
    return (str(path), info.st_mtime_ns, info.st_size)


def _infrastructure_key(
    all_repos: list[dict[str, Any]], chosen: list[int],
) -> Optional[tuple[Any, ...]]:
    """Everything the ``infrastructure`` block depends on, or ``None`` if it cannot be known.

    That is the registry (path and alias of every repo), each repo's graph stamp, which repos
    were selected, both alias files and the repo cap. The time budget is left out because only
    a block that was not cut short is kept.
    """
    from ..repo_aliases import repo_aliases_path
    from ..repo_cache import graph_stamp

    stamps = []
    for entry in all_repos:
        stamp = graph_stamp(entry)
        if stamp is None:
            return None
        stamps.append((str(entry["path"]), entry.get("alias"), stamp))
    service_file = os.environ.get(sa.ALIASES_FILE_ENV, "").strip()
    return (
        tuple(stamps), tuple(chosen),
        _file_signature(Path(service_file) if service_file else None),
        _file_signature(repo_aliases_path()), ij.MAX_REPOS,
    )


def _attach_infrastructure(
    result: dict[str, Any], all_repos: list[dict[str, Any]], selected: list[dict[str, Any]],
    budget: float,
) -> None:
    """Add the per-repo ``infrastructure`` summary (see ``infra_join``) to a reply."""
    by_id = {id(e): i for i, e in enumerate(all_repos)}
    chosen = [by_id[id(e)] for e in selected]

    def compute() -> dict[str, Any]:
        rules = sa.load_alias_rules()
        resolver = RepoNameResolver(all_repos)
        return ij.build_infrastructure(
            all_repos, chosen, sa.FactsCache(all_repos), rules,
            resolver.lookup, budget, cross_repo_workers(),
        )

    key = _infrastructure_key(all_repos, chosen)
    if key is None:
        block = compute()
    else:
        # Concurrent calls for the same repos at the same graph versions build it once, and a
        # complete block is reused until a graph or an alias file changes.
        block = copy.deepcopy(INFRASTRUCTURE_RESULTS.get(
            key, compute, cacheable=lambda found: not found["truncated"],
        ))
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
    include_repo_list: bool = False,
    path_prefix: str | None = None,
    source: str | None = None,
    machine: str | None = None,
    include_tests: bool = True,
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
            ``case_mismatch``, ``method_not_served`` (a JS/TS call whose route is served
            only with other HTTP methods; ``served_methods`` lists them),
            ``prefix_mismatch`` (a JS/TS lead: the same last two path segments under another
            prefix, low confidence, not a match), ``no_route_match``, ``no_target_repo`` or
            ``unmatchable``.
        limit: Maximum rows per client repo (default 20).
        max_results: Maximum rows returned overall (default 50, capped at
            100). ``counts`` and ``total`` cover every call that passed the
            filters, not just the rows shown.
        include_repo_list: Feign/JS only: return ``repos_searched`` (the names) even when more
            than 10 repos were read. ``repos_searched_total`` is always the count.
        path_prefix: ``awssdk`` and ``stepfunction`` only: keep rows whose ``file`` is this
            path or lies below it (repo-relative, e.g. ``mcp/``).
        source: ``awssdk`` only: keep rows whose ``source`` symbol equals this, ends with it
            after a ``.``, or lies below it.
        machine: ``stepfunction`` only: keep rows of this state machine (exact name).
        include_tests: ``awssdk`` only. Every row has ``is_test``. Default True keeps test
            rows (counts are unchanged); False hides them and ``tests_hidden`` says how many.
            All four filters apply before ``counts``, ``total`` and paging, and a cursor is
            refused for different values.
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

    ``amqp`` without ``service`` lists every exchange (one row each: publisher and listener
    counts and repos, routing and binding keys, ``status`` ``unlistened``, ``unpublished`` or
    ``matched``; ``status`` filters on those, ``route`` is a key substring). With ``service`` it
    lists the publishers and listeners of one RabbitMQ exchange across the selected
    repos: ``service`` is the exchange, ``route`` an optional key and ``status``
    ``publisher`` or ``listener``. With ``route`` a listener row is kept when its ``binding_key``
    matches that key and a publisher row when its ``routing_key`` matches it, using AMQP topic
    wildcards (``*`` one word, ``#`` zero or more) only in a binding key that holds one;
    ``matched_via`` is ``exact_key``, ``topic_pattern`` or ``key_unknown``. The exchange type is
    never inferred. It can only find links once both repos carry the exchange edges: a repo
    indexed before they existed is named in ``repos_without_amqp`` and shows nothing until
    rebuilt. It pages like the other origins (``max_results`` is the page size) and does not
    use ``limit`` or the infrastructure summary.

    ``trigger`` answers "what invokes this Lambda, queue, state machine, topic or bucket
    handler" from the Terraform already indexed (no rebuild): EventBridge rules and Scheduler
    schedules, Lambda event source mappings, SNS subscriptions, S3 bucket notifications, API
    Gateway routes and integrations, ``aws_lambda_permission`` and EventBridge Pipes. ``service``
    keeps rows whose invoked side (target) is that name, ``service/name`` or Terraform label;
    ``route`` keeps rows whose firing side (source) is that rule, queue, topic, bucket or route
    key, so "what fires X" is ``route=X``. Rows carry ``trigger_kind``, ``source``, ``target``,
    ``schedule`` and ``event_pattern_sources`` when written out, ``file``/``line`` and a
    ``status`` of ``resolved``, ``source_unresolved`` or ``target_unresolved``; ``via`` is
    ``reference`` for the shapes inferred from which resources a block mentions. See
    ``docs/COMMANDS.md`` for the shapes that cannot be resolved.

    A Feign service name that is not a registered repo name resolves through the optional
    alias file ``CRG_SERVICE_ALIASES_FILE`` (``{"alias": "repo", "prefix:p-": "repo"}``) or
    the names the repos declare (``spring.application.name``, ECS service, ECR repository);
    rows carry ``matched_via``, and a name reaching several repos is ``ambiguous`` with
    ``candidate_repos``. The ``terraform``, ``stepfunction`` and ``awssdk`` origins also return
    an ``infrastructure`` object relating each selected repo to the ECR, ECS, stack and declared
    resources that belong to it (see ``docs/SERVICE_ALIASES.md``), on the first page only; a
    page reached with ``cursor`` says so in ``infrastructure_note`` instead.

    ``external`` groups the HTTP calls that reach no registered service by host, to show which
    third-party vendors each repo depends on: one row per (host, repo) with ``vendor`` (a small
    static table; unknown hosts stay as they are), ``calls``, ``files``, ``samples`` and
    ``sources``. Sources are JavaScript/TypeScript absolute URLs, ``baseURL`` and Ember adapter
    hosts, and Feign clients whose service name is registered nowhere (grouped as
    ``service:<name>``; a literal ``url=`` in the annotation gives its host). A placeholder host
    is ``host_unresolved`` with its key. ``service`` filters by host, domain, vendor or Feign
    service name, ``route`` by path prefix, ``status`` is ``known_vendor``, ``unknown_vendor`` or
    ``host_unresolved``. Never shows query strings, credentials or ports. ``consumers`` is a
    reverse lookup: ``service`` is the name of a queue, bucket, SSM parameter, table, topic or
    secret, and the rows are its Terraform declarations and every repo that references it, each
    with ``match`` ``exact``, ``prefix`` (an SSM ``GetParametersByPath`` path above it) or
    ``config_key`` (a property or environment variable of that name). ``route`` filters by AWS
    service or kind, ``status`` by ``declaration``, ``consumer`` or a match. Names under 4
    characters or generic ones (``logs``, ``test``) are refused. Without ``service`` it lists the
    SSM reads that match no declared ``aws_ssm_parameter``. Environment variable values and
    string literals are not in the graphs and are not joined. Both page like the other origins.

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
        The ``terraform``, ``workflow``, ``stepfunction``, ``awssdk``, ``amqp`` and ``trigger``
        origins page the same way: ``max_results`` (at most 100) is the page size, ``limit`` is
        not used, and the cursor is refused when the query or the rows changed.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")
    infra_origins = (
        "terraform", "workflow", "stepfunction", "awssdk", "amqp", "trigger", "external",
        "consumers",
    )
    accepted_statuses = (*jl.ALL_STATUSES, *eh.NO_TARGET_REASONS)
    if status is not None and origin not in infra_origins and status not in accepted_statuses:
        raise ValueError(f"status must be one of: {', '.join(accepted_statuses)}")
    if origin is not None and origin not in ("feign", "js", *infra_origins):
        raise ValueError(
            "origin must be one of: feign, js, terraform, workflow, stepfunction, awssdk, amqp, "
            "trigger, external, consumers"
        )
    for name, given, allowed in (
        ("path_prefix", path_prefix, ("awssdk", "stepfunction")),
        ("source", source, ("awssdk",)),
        ("machine", machine, ("stepfunction",)),
        ("include_tests", None if include_tests else False, ("awssdk",)),
    ):
        if given is not None and origin not in allowed:
            raise ValueError(f"{name} applies only to origin={' or '.join(allowed)}")
    if origin == "trigger":
        return _trigger_links(repos, service, route, status, max_results, cursor)
    if origin in ("external", "consumers"):
        from . import cross_repo_origins as cro

        if origin == "external":
            return cro.external_links(repos, service, route, status, max_results, cursor)
        return cro.consumers_links(repos, service, route, status, max_results, cursor)
    if origin == "amqp":
        return _amqp_links(repos, service, route, status, max_results, cursor)
    if origin == "awssdk":
        return _awssdk_links(
            repos, service, route, status, max_results, cursor,
            path_prefix=path_prefix, source=source, include_tests=include_tests,
        )
    if origin == "stepfunction":
        return _stepfunction_links(
            repos, service, route, status, max_results, cursor,
            path_prefix=path_prefix, machine=machine,
        )
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
        counts_by_reason: dict[str, int] = {}
        origin_counts: dict[str, int] = {}
        multiple_total = 0
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
                if reason is None and call.origin == "js":
                    result = jl.refine_no_route_match(
                        call, result, js_index_for(index), own_index_for(index),
                    )
                no_target_why = (
                    eh.no_target_reason(call.service, result.reason)
                    if result.status == fl.NO_TARGET_REPO else None
                )
                if status is not None and status not in (result.status, no_target_why):
                    continue

                counts[result.status] = counts.get(result.status, 0) + 1
                if no_target_why:
                    counts_by_reason[no_target_why] = counts_by_reason.get(no_target_why, 0) + 1
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
                if result.status in (fl.CASE_MISMATCH, jl.PREFIX_MISMATCH):
                    row["confidence"] = "low"
                if result.status == jl.METHOD_NOT_SERVED:
                    row["served_methods"] = jl.served_methods(result.candidates)
                if result.reason:
                    row["reason"] = result.reason
                if no_target_why:
                    row["no_target_reason"] = no_target_why
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
                    if result.status == fl.MATCHED and len(result.candidates) > 1:
                        # "matched" stays as is; this says the match is not a unique one.
                        row["multiple_candidates"] = True
                        multiple_total += 1
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
        skipped_shown, skipped_total, skipped_truncated = _listed(skipped), len(skipped), False
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
        ordered = ", ".join(f"{counts[s]} {s}" for s in jl.ALL_STATUSES if s in counts)
        response: dict[str, Any] = {
            "status": "ok",
            "summary": (
                (
                    # A filtered request names its origin, also when nothing matched.
                    f"{total} JS/TS call(s) (origin=js) in {len(searched)} repo(s)"
                    if origin == "js"
                    else f"{total} Feign call(s) (origin=feign) in {len(searched)} repo(s)"
                    if origin == "feign"
                    else f"{total} call(s) ({origin_counts.get('feign', 0)} Feign, "
                    f"{origin_counts.get('js', 0)} JS/TS) in {len(searched)} repo(s)"
                    if origin_counts.get("js")
                    else f"{total} Feign call(s) in {len(searched)} repo(s)"
                )
                + (f": {ordered}" if ordered else "")
                + (f"; {multiple_total} matched with several candidates" if multiple_total else "")
                + shown_range(page)
                + notes
            ),
            "origin": origin or "all",
            "results": results,
            "results_shown": len(results),
            "counts": counts,
            "counts_by_reason": counts_by_reason,
            "multiple_candidates_total": multiple_total,
            "total": total,
            "truncated": truncated,
            **page.fields(),
            **_searched_fields(searched, include_repo_list),
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


# ---------------------------------------------------------------------------
# handlers_of on a route, across every registered repository
# ---------------------------------------------------------------------------

#: Handler rows kept per matching repo: a route is normally served by one place.
_ROUTE_ROWS_PER_REPO = 10


def handlers_of_route_across_repos(
    target: str,
    detail_level: str = "standard",
    max_results: int | None = None,
) -> dict[str, Any]:
    """Answer ``handlers_of <route>`` from whichever registered repos serve the route.

    [REGISTRY] A route such as ``POST /v2/respondents/avmap`` names an endpoint, not a
    repository, so a caller should not have to know which repo serves it. Every registered
    repo with a graph is asked; the reply lists the repos that matched (``repos_matched``,
    each with its handler count and matched endpoints) and tags every row with its ``repo``.
    Rows are capped (``max_results``, default 20; at most ten per repo) and
    ``results_omitted`` counts the rest; ``repos_without_graph`` and ``repos_unreadable`` count
    registered repos that could not be asked, and ``repos_skipped`` those left when the call
    budget (``CRG_CROSS_REPO_BUDGET_SECONDS``) ran out.
    """
    from ..registry import Registry
    from .query import query_graph

    cap = max_results if max_results is not None else 20
    deadline = None
    budget = cross_repo_budget_seconds()
    if budget > 0:
        deadline = _now() + budget
    entries = Registry().list_repos()
    matched: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    total_rows = 0
    searched = without_graph = unreadable = skipped = 0
    for entry in entries:
        if _out_of_budget(deadline):
            skipped += 1
            continue
        repo_path = Path(str(entry.get("path") or ""))
        if not repo_path.is_dir() or not get_db_path(repo_path).exists():
            without_graph += 1
            continue
        try:
            reply = query_graph(
                pattern="handlers_of", target=target, repo_root=str(repo_path),
                detail_level=detail_level, max_results=min(cap, _ROUTE_ROWS_PER_REPO),
            )
        except Exception as exc:  # one broken graph must not hide the others
            logger.warning("handlers_of route lookup failed in %s: %s", repo_path, exc)
            unreadable += 1
            continue
        searched += 1
        if reply.get("status") != "ok":
            continue
        label = _repo_label(entry)
        handlers = int(reply.get("result_count", len(reply.get("results", []))))
        matched.append({
            "repo": label,
            "handlers": handlers,
            "matched_endpoints": list(reply.get("matched_endpoints", [])),
        })
        total_rows += handlers
        for row in reply.get("results", []):
            if len(rows) < cap:
                rows.append({**row, "repo": label})
    scope = {
        "repos_searched": searched,
        "repos_without_graph": without_graph,
        "repos_unreadable": unreadable,
        "repos_skipped": skipped,
    }
    note = "".join(
        f", {n} {why}" for n, why in (
            (without_graph, "without a graph"), (unreadable, "unreadable"),
            (skipped, "skipped (call budget spent)"),
        ) if n
    )
    if not matched:
        return {
            "status": "not_found",
            "pattern": "handlers_of",
            "target": _sanitize_name(target),
            "summary": (
                f"No endpoint matches route '{_sanitize_name(target)}' in {searched} "
                f"registered repo(s) searched{note}."
            ),
            **scope,
        }
    names = ", ".join(f"{m['repo']} ({m['handlers']})" for m in matched[:10])
    return {
        "status": "ok",
        "pattern": "handlers_of",
        "target": _sanitize_name(target),
        "summary": (
            f"Route '{_sanitize_name(target)}' is handled in {len(matched)} of {searched} "
            f"registered repo(s) searched{note}: {names}"
            f"{' ...' if len(matched) > 10 else ''}. Pass repo_root for one repo's full answer."
        ),
        "repos_matched": matched[:20],
        "repos_matched_total": len(matched),
        "repos_matched_truncated": len(matched) > 20,
        **scope,
        "result_count": total_rows,
        "results_omitted": max(0, total_rows - len(rows)),
        "results": rows,
    }
