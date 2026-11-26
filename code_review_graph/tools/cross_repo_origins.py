"""The ``external`` and ``consumers`` origins of ``cross_repo_requests``.

``registry_tools.cross_repo_requests_func`` dispatches here. The pure rules live in
:mod:`code_review_graph.external_hosts` and :mod:`code_review_graph.consumer_join`; this
module selects the repos, reads each graph read-only inside the call budget, pages the rows
and builds the reply, as the ``amqp`` origin does.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .. import consumer_join as cj
from .. import external_hosts as eh
from .. import service_aliases as sa
from ..constants import cross_repo_budget_seconds, cross_repo_workers
from ..errors import GraphStoreError
from ..graph import _sanitize_name
from ..paging import content_stamp, make_scope, paginate, shown_range
from ..repo_aliases import RepoNameResolver, load_repo_aliases
from ..repo_cache import MEMO
from ..terraform_links import open_store

logger = logging.getLogger(__name__)

_MAX_ECHOED = 20
_MAX_FILES = 5
_MAX_SAMPLES = 3
_MAX_HOSTS_SEEN = 3


def _label(entry: dict[str, Any]) -> str:
    return str(entry.get("alias") or Path(entry["path"]).name)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(_sanitize_name(value).split())
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(str(k)): _clean(v) for k, v in value.items()}
    return value


def _select(
    all_repos: list[dict[str, Any]], repos: Optional[list[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    from .registry_tools import _echoed, _select_repos

    if not repos:
        return all_repos, []
    selected, unknown, _ambiguous = _select_repos(all_repos, repos)
    return selected, _echoed(unknown)[0]


def _read(entry: dict[str, Any], namespace: str, reader: Any) -> Optional[Any]:
    """``reader(store, repo_path)`` on one repo's graph, kept while the graph file is unchanged.

    ``None`` when the graph cannot be read (never kept).
    """
    def compute() -> Optional[Any]:
        store = open_store(entry)
        if store is None:
            return None
        try:
            return reader(store, Path(entry["path"]))
        except (sqlite3.Error, GraphStoreError, OSError) as exc:
            logger.warning("Cannot read %s of %s: %s", namespace, entry["path"], exc)
            return None
        finally:
            store.close()

    return MEMO.get(namespace, entry, compute, cacheable=lambda value: value is not None)


def _reply(
    origin: str, summary: str, page: Any, counts: dict[str, int], selected: int,
    skipped: list[str], unreadable: list[str], unknown: list[str], **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok", "summary": summary + shown_range(page), "origin": origin,
        "results": page.items, "counts": counts, "total": page.total,
        "truncated": page.truncated or bool(skipped),
        "repos_searched": selected - len(skipped), "unreadable": unreadable,
        **page.fields(), **extra,
    }
    if skipped:
        result["skipped_repos"] = skipped
    if unknown:
        result["unknown"] = unknown
    return result


# ---------------------------------------------------------------------------
# origin = external
# ---------------------------------------------------------------------------


class _FeignRegistry:
    """Does a Feign service name resolve to a registered repo? Facts are read lazily."""

    def __init__(self, all_repos: list[dict[str, Any]]) -> None:
        self.all_repos = all_repos
        self.resolver = RepoNameResolver(all_repos)
        self.rules = sa.load_alias_rules()
        self.facts = sa.FactsCache(all_repos)
        self._identity: list[sa.IdentityIndex] = []
        self.folded: dict[str, set[str]] = defaultdict(set)
        names = [n for e in all_repos for n in (e.get("alias"), Path(e["path"]).name) if n]
        names += list(load_repo_aliases())
        from .. import feign_links as fl

        for name in names:
            self.folded[fl.normalize_service_name(name)].add(name)
        self._cache: dict[str, bool] = {}

    def _index(self) -> sa.IdentityIndex:
        if not self._identity:
            self.facts.prefetch(range(len(self.all_repos)), cross_repo_workers())
            self._identity.append(sa.IdentityIndex(self.all_repos, self.facts))
        return self._identity[0]

    def registered(self, service: str) -> bool:
        if service not in self._cache:
            found = sa.resolve_service(
                service, self.resolver.lookup, self.folded, self.rules, self._index,
            )
            self._cache[service] = bool(found.indexes or found.candidates)
        return self._cache[service]


def _host_wanted(group: eh.Group, wanted: str) -> bool:
    text = wanted.strip().lower()
    if not text:
        return True
    candidates = {group.key.lower()}
    if group.host:
        candidates.add(group.host)
    if any(c == text or c.endswith("." + text) or c == f"*.{text}" for c in candidates):
        return True
    return bool(group.vendor and group.vendor.lower() == text) or (
        group.key.lower() == f"service:{text}"
    )


def _path_in_scope(path: str, wanted: str) -> bool:
    base = wanted.strip("/")
    have = path.strip("/")
    return not base or have == base or have.startswith(base + "/")


def external_links(
    repos: Optional[list[str]], host: Optional[str], route: Optional[str],
    status: Optional[str], max_results: int, cursor: Optional[str] = None,
) -> dict[str, Any]:
    """The ``origin="external"`` branch: HTTP calls to hosts that are no registered service.

    One row per (host group, repo), the busiest host first, each with the call count, up to five
    files, up to three method/path samples and the sources the host came from.
    ``host`` (``service``) keeps one host, a domain and its subdomains, a vendor or a Feign
    service name; ``route`` keeps the calls whose path is at or under it; ``status`` is one of
    ``known_vendor``, ``unknown_vendor`` or ``host_unresolved``. Query strings, credentials and
    ports never appear. Calls in a registered repo's own service are not listed.
    """
    from ..registry import Registry

    if status is not None and status not in eh.STATUSES:
        raise ValueError(f"status must be one of: {', '.join(eh.STATUSES)}")
    all_repos = Registry().list_repos()
    selected, unknown = _select(all_repos, repos)
    feign = _FeignRegistry(all_repos)
    budget = cross_repo_budget_seconds()
    started = time.monotonic()
    skipped: list[str] = []
    unreadable: list[str] = []
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    internal = 0
    for entry in sorted(selected, key=_label):
        label = _clean(_label(entry))
        if budget and time.monotonic() - started > budget:
            skipped.append(label)
            continue
        calls = _read(entry, "external-calls", eh.read_client_calls)
        if calls is None:
            unreadable.append(label)
            continue
        for call in calls:
            if call["kind"] == "js":
                group = eh.classify_js(call)
            elif call["kind"] == "py":
                group = eh.classify_py(call)
            else:
                service = call.get("service")
                registered = bool(
                    service and not call.get("service_unresolved") and feign.registered(service)
                )
                group = eh.classify_feign(call, registered)
                if group is None:
                    internal += 1
            if group is None:
                continue
            if host and not _host_wanted(group, host):
                continue
            if route and not _path_in_scope(group.path, route):
                continue
            status_counts[group.status] = status_counts.get(group.status, 0) + 1
            if status and group.status != status:
                continue
            cell = cells.setdefault((group.key, label), {
                "host": group.key, "vendor": group.vendor, "vendor_basis": group.vendor_basis,
                "status": group.status, "repo": label, "calls": 0, "sources": {},
                "files": [], "samples": [], "hosts_seen": [],
            })
            cell["calls"] += 1
            cell["sources"][group.source] = cell["sources"].get(group.source, 0) + 1
            if call["file"] not in cell["files"] and len(cell["files"]) < _MAX_FILES:
                cell["files"].append(call["file"])
            if len(cell["samples"]) < _MAX_SAMPLES:
                cell["samples"].append({
                    "method": call["method"], "path": group.path,
                    "file": call["file"], "line": call["line"],
                })
            if group.unresolved_key:
                cell["unresolved_key"] = group.unresolved_key
            seen = cell["hosts_seen"]
            if (group.host and group.host != group.key and group.host not in seen
                    and len(seen) < _MAX_HOSTS_SEEN):
                seen.append(group.host)
    per_host: dict[str, int] = defaultdict(int)
    for (key, _repo), cell in cells.items():
        per_host[key] += cell["calls"]
    rows = sorted(
        cells.values(), key=lambda c: (-per_host[c["host"]], c["host"], -c["calls"], c["repo"]),
    )
    for row in rows:
        row["host_calls"] = per_host[row["host"]]
        if not row["hosts_seen"]:
            del row["hosts_seen"]
        if not row["vendor"]:
            del row["vendor"], row["vendor_basis"]
    rows = _clean(rows)
    page = paginate(
        rows, max_results, 100, cursor,
        scope=make_scope(
            "cross_repo_requests_tool", origin="external",
            repos=sorted(repos) if repos else None, host=host, route=route, status=status,
        ),
        stamp=content_stamp([rows, skipped]),
    )
    summary = (
        f"{sum(c['calls'] for c in cells.values())} external call(s) to {len(per_host)} host "
        f"group(s) in {len(rows)} (host, repo) row(s) across {len(selected)} repo(s)"
    )
    return _reply(
        "external", summary, page, status_counts, len(selected), skipped, unreadable, unknown,
        hosts_total=len(per_host), feign_calls_to_registered_repos=internal,
    )


# ---------------------------------------------------------------------------
# origin = consumers
# ---------------------------------------------------------------------------


def _declaration_row(repo: str, decl: dict[str, Any]) -> dict[str, Any]:
    row = {
        "role": cj.ROLE_DECLARATION, "match": cj.MATCH_EXACT, "repo": repo,
        "file": decl["file"], "line": decl["line"], "service": decl["service"],
        "kind": decl["kind"], "name": decl["name"], "terraform_type": decl["terraform_type"],
    }
    if decl["template"]:
        row["declared_as"] = "template"
    return row


def consumers_links(
    repos: Optional[list[str]], name: Optional[str], kind: Optional[str],
    status: Optional[str], max_results: int, cursor: Optional[str] = None,
) -> dict[str, Any]:
    """The ``origin="consumers"`` branch: who declares and who uses one named resource.

    With a ``name`` (``service``): the Terraform declarations of it (every registered repo is
    searched) and the consumers in the selected repos, each with ``match`` ``exact``, ``prefix``
    or ``config_key``. Without one: the SSM reads that fit no declared parameter. ``kind``
    (``route``) keeps one AWS service or kind (``sqs``, ``queue``, ``ssm`` ...); ``status`` one
    role (``declaration``, ``consumer``) or match. See :mod:`code_review_graph.consumer_join`
    for what is joined, what is not, and the short-name rule.
    """
    from ..registry import Registry

    if status is not None and status not in (*cj.ROLES, *cj.MATCHES):
        raise ValueError(f"status must be one of: {', '.join((*cj.ROLES, *cj.MATCHES))}")
    wanted = name.strip() if name else ""
    all_repos = Registry().list_repos()
    selected, unknown = _select(all_repos, repos)
    scope = make_scope(
        "cross_repo_requests_tool", origin="consumers", repos=sorted(repos) if repos else None,
        name=wanted, kind=kind, status=status,
    )
    if wanted:
        why = cj.refusal(wanted)
        if why:
            empty = paginate([], max_results, 100, cursor, scope=scope, stamp=content_stamp([]))
            return _reply(
                "consumers",
                f"name '{_clean(wanted)}' not searched: "
                + ("shorter than " + str(cj.MIN_NAME_LENGTH) + " characters"
                   if why == "name_too_short" else "too generic to join on")
                + "; use a qualified name",
                empty, {}, len(selected), [], [], unknown,
                refused=why, min_name_length=cj.MIN_NAME_LENGTH,
            )
    budget = cross_repo_budget_seconds()
    started = time.monotonic()
    skipped: list[str] = []
    unreadable: list[str] = []
    selected_ids = {id(e) for e in selected}
    declared_all: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    unresolved_reads = 0
    unnamed_declarations = 0
    for entry in all_repos:
        label = _clean(_label(entry))
        mine = id(entry) in selected_ids
        if budget and time.monotonic() - started > budget:
            if mine:
                skipped.append(label)
            continue
        decls = _read(entry, "consumer-declarations", cj.read_declarations)
        uses = _read(entry, "consumer-uses", cj.read_uses) if mine else None
        if decls is None or (uses is None and mine):
            if mine:
                unreadable.append(label)
            if decls is None:
                continue
        items = [{**d, "repo": label} for d in decls["items"]]
        declared_all.extend(items)
        unnamed_declarations += decls["unnamed"]
        if wanted:
            for decl in items:
                if cj.declaration_matches(decl, wanted):
                    rows.append(_declaration_row(label, decl))
            for use in uses or []:
                found = cj.use_matches(use, wanted)
                if found is None:
                    continue
                match, detail = found
                row = {
                    "role": cj.ROLE_CONSUMER, "match": match, "repo": label,
                    "file": use["file"], "line": use["line"], "service": use["service"],
                    "kind": use["kind"], "name": use["name"], "operation": use["operation"],
                    "access": use["access"], "sdk": use["sdk"], **detail,
                }
                if use["source"]:
                    row["source"] = use["source"]
                rows.append(row)
        elif mine and uses:
            missing, unnamed = cj.undeclared_reads(uses, [])
            unresolved_reads += unnamed
            for use in missing:
                use["repo"] = label
                rows.append(use)
    if not wanted:
        rows = _undeclared(rows, declared_all)
    if kind:
        folded = kind.strip().lower()
        rows = [r for r in rows if folded in (str(r["service"]).lower(), str(r["kind"]).lower())]
    counts: dict[str, int] = {}
    for r in rows:
        key = r["role"] if r["role"] != cj.ROLE_CONSUMER else r["role"] + ":" + r["match"]
        counts[key] = counts.get(key, 0) + 1
    if status:
        rows = [r for r in rows if status in (r["role"], r["match"])]
    rows = [r for r in _clean(rows)]
    rows.sort(key=lambda r: (
        r["role"] != cj.ROLE_DECLARATION, r["repo"], r["file"], r["line"] or 0, r["name"],
    ))
    page = paginate(rows, max_results, 100, cursor, scope=scope,
                    stamp=content_stamp([rows, skipped]))
    extra: dict[str, Any] = {
        "min_name_length": cj.MIN_NAME_LENGTH,
        "not_joined": ["environment variable values", "task definition environment values",
                       "string literals other than AWS call targets"],
    }
    if wanted:
        summary = (
            f"{page.total} declaration/consumer row(s) for '{_clean(wanted)}' "
            f"across {len(selected)} repo(s)"
        )
    else:
        summary = (
            f"{page.total} SSM read(s) matching no declared aws_ssm_parameter in "
            f"{len(all_repos)} repo(s)"
        )
        extra["unresolved_ssm_reads"] = unresolved_reads
        extra["undeclared_means"] = (
            "no registered Terraform declares the parameter by a literal or templated name; "
            "it may exist from the console, CloudFormation, or a computed name"
        )
    extra["declarations_with_computed_name"] = unnamed_declarations
    return _reply(
        "consumers", summary, page, counts, len(selected), skipped, unreadable, unknown, **extra,
    )


def _undeclared(
    rows: list[dict[str, Any]], declared: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The no-name mode: the reads (all of them, by repo) that fit no declared parameter."""
    ssm = [d for d in declared if d["service"] == "ssm"]
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for use in rows:
        is_path = use.get("is_path")
        name = use["name"]
        hit = (
            any(cj.covers(name, d["name"]) for d in ssm) if is_path
            else any(cj.declaration_matches(d, name) for d in ssm)
        )
        key = (use["repo"], use["file"], use["line"], name)
        if hit or key in seen:
            continue
        seen.add(key)
        row = {
            "role": cj.ROLE_UNDECLARED, "match": cj.MATCH_PREFIX if is_path else cj.MATCH_EXACT,
            "repo": use["repo"], "file": use["file"], "line": use["line"],
            "service": "ssm", "kind": "parameter", "name": name,
            "operation": use["operation"], "access": use["access"], "sdk": use["sdk"],
        }
        for field in ("via", "profile"):
            if use.get(field):
                row[field] = use[field]
        if use.get("source"):
            row["source"] = use["source"]
        out.append(row)
    return out
