"""Tool 32: pr_blast_radius_func."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .. import feign_links as fl
from .. import pr_blast_radius as pbr
from ..endpoint_index import attach as attach_endpoints
from ..graph import _sanitize_name
from ._common import _get_store, _validate_positive_int

logger = logging.getLogger(__name__)

_MAX_FILES_SHOWN = 50
_MAX_ENDPOINTS_SHOWN = 50
_MAX_CALLERS_SHOWN = 50
_MAX_NAMES_PER_FILE = 12
_MAX_DEPTH = 6


def _names(values: list[str]) -> list[str]:
    shown = [_sanitize_name(v) for v in dict.fromkeys(values)]
    return shown[:_MAX_NAMES_PER_FILE]


def _route_key(server: fl.ServerEndpoint) -> tuple[str, str]:
    return (server.http_method.upper(), fl.normalize_route(server.route))


def _endpoint_row(server: fl.ServerEndpoint, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "http_method": _sanitize_name(server.http_method),
        "route": _sanitize_name(server.route),
        "file": _sanitize_name(server.file) if server.file else None,
        "line": server.line,
    }
    row.update(extra)
    return row


def _own_entry(all_repos: list[dict[str, Any]], root: Path) -> Optional[int]:
    for index, entry in enumerate(all_repos):
        try:
            if Path(entry["path"]).resolve() == root.resolve():
                return index
        except OSError:
            continue
    return None


def _external_callers(
    root: Path, affected: list[fl.ServerEndpoint],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Client calls in the other registered repos that reach one of *affected*.

    Reads every other repo's stored endpoint index (``endpoint_index.py``), the same data
    ``cross_repo_requests_tool`` joins. Returns ``(rows, info)``; ``info`` says how many repos
    were read and how many could not be.
    """
    from ..registry import Registry
    from .registry_tools import _load_endpoints, _repo_label

    info: dict[str, Any] = {"repos_read": 0, "unreadable": 0}
    if not affected:
        return [], info
    all_repos = Registry().list_repos()
    own = _own_entry(all_repos, root)
    index = fl.ServerIndex(affected)
    own_names = {fl.normalize_service_name(root.name)}
    if own is not None:
        own_names.add(fl.normalize_service_name(_repo_label(all_repos[own])))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for position, entry in enumerate(all_repos):
        if position == own:
            continue
        clients, _servers, why, _source = _load_endpoints(entry)
        if why:
            info["unreadable"] += 1
            continue
        info["repos_read"] += 1
        for call in clients:
            if fl.unmatchable_reason(call) is not None:
                continue
            if call.origin == "js":
                result = fl.link_js_call(call, index)
            elif call.service and fl.normalize_service_name(call.service) in own_names:
                result = fl.link_call(call, affected)
            else:
                continue
            if result.status not in (fl.MATCHED, fl.AMBIGUOUS):
                continue
            key = (call.repo, call.file, call.line, call.http_method, call.route)
            if key in seen:
                continue
            seen.add(key)
            target = result.candidates[0] if result.candidates else None
            rows.append({
                "client_repo": _sanitize_name(call.repo),
                "file": _sanitize_name(call.file) if call.file else None,
                "line": call.line,
                "origin": call.origin,
                "http_method": _sanitize_name(call.http_method),
                "route": _sanitize_name(call.route),
                "status": result.status,
                "confidence": result.confidence,
                "reaches": _sanitize_name(f"{target.http_method} {target.route}")
                if target else None,
            })
    rows.sort(key=lambda r: (r["client_repo"], r["file"] or "", r["line"] or 0))
    return rows, info


def pr_blast_radius_func(
    pr: int | None = None,
    head: str | None = None,
    base: str | None = None,
    depth: int = pbr.DEFAULT_DEPTH,
    include_external: bool = True,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Blast radius of a pull request from its changed files, reached through the base graph.

    [REVIEW] The graph holds the default branch, so a pull request's new files are not in it.
    This reads only the files the pull request touches, straight from Git, parses them in memory
    and compares them with the base graph: which symbols changed, were added or were removed,
    what reaches the changed ones, the routes added or removed, the endpoints affected, the
    client calls in other registered repos that reach those endpoints, migrations the pull
    request adds, and changed members no test names. Nothing is written to the graph.

    Args:
        pr: A pull request number. The server fetches ``refs/pull/<pr>/head`` from the
            checkout's ``origin`` into ``refs/crg/`` (no branch or working tree is touched).
        head: A ref or commit to read the new files from, when no ``pr`` is given.
        base: The branch the pull request merges into. Default: the remote's default
            branch. A stacked pull request should name its parent branch; the result
            then also says how many files differ from the default branch.
        depth: Hops the reach walk follows from a changed symbol (default 3, at most 6).
        include_external: Join the affected endpoints to the client calls of the other
            registered repos (default True).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    _validate_positive_int(depth, "depth")
    depth = min(depth, _MAX_DEPTH)
    if pr is None and not head:
        raise ValueError("give a pull request number (pr) or a head ref (head)")
    store, root = _get_store(repo_root)
    try:
        repo = str(root)
        notes: list[str] = []
        if pr is not None:
            base_ref, head_sha = pbr.fetch_pull_request(repo, pr, base)
        else:
            head_sha = pbr.resolve_commit(repo, head or "", "head")
            base_ref = pbr.resolve_commit(repo, base or pbr.default_branch(repo), "base")
        changes, merge_based = pbr.changed_files(repo, base_ref, head_sha)
        if len(changes) > pbr.MAX_CHANGED_FILES:
            notes.append(
                f"{len(changes)} files changed; only the first {pbr.MAX_CHANGED_FILES} were "
                "read."
            )
            changes = changes[:pbr.MAX_CHANGED_FILES]
        if not merge_based:
            notes.append(
                "Git has no merge base for the base and head (a shallow clone): the files are "
                "compared directly, so changes made on the base since they diverged can show up."
            )
        if not changes:
            return {
                "status": "ok",
                "summary": "No files differ between the base and the head.",
                "files": [], "total_files": 0, "notes": notes,
            }
        ranges = pbr.diff_ranges(repo, base_ref, head_sha, merge_based)
        sizes = pbr.blob_sizes(
            repo, head_sha, [c.path for c in changes if c.status != "deleted"],
        )
        found = pbr.analyse(
            store, repo, changes, ranges, pbr.git_file_reader(repo, head_sha), depth=depth,
            sizes=sizes,
        )
        stacked = _stacked_note(repo, base_ref, head_sha, base)
        if stacked:
            notes.append(stacked)
        return _build_result(root, found, changes, notes, include_external, depth)
    except pbr.PullRequestError as exc:
        return {"status": "error", "error": _sanitize_name(str(exc))}
    finally:
        store.close()


def _stacked_note(repo: str, base_ref: str, head: str, base_given: Optional[str]) -> Optional[str]:
    if not base_given:
        return None
    try:
        default = pbr.resolve_commit(repo, pbr.default_branch(repo), "base")
    except pbr.PullRequestError:
        return None
    if base_ref == default:
        return None
    try:
        out = pbr._git(repo, "diff", "--name-only", f"{default}...{head}", "--")
    except pbr.PullRequestError:
        return None
    count = len([line for line in out.splitlines() if line])
    return (
        f"Compared with the base branch you named. Against the default branch "
        f"{count} files differ, because this pull request sits on top of other changes."
    )


def _build_result(
    root: Path, found: dict[str, Any], changes: list[pbr.FileChange], notes: list[str],
    include_external: bool, depth: int,
) -> dict[str, Any]:
    reports = found["files"]
    head_servers = attach_endpoints(root.name, root, ([], found["endpoints_head"]))[1]
    base_servers = attach_endpoints(root.name, root, ([], found["endpoints_base"]))[1]
    reached_servers = attach_endpoints(root.name, root, ([], found["endpoints_reached"]))[1]

    head_keys = {_route_key(s): s for s in head_servers}
    base_keys = {_route_key(s): s for s in base_servers}
    contract = (
        [_endpoint_row(head_keys[k], change="route added") for k in head_keys if k not in base_keys]
        + [_endpoint_row(base_keys[k], change="route removed")
           for k in base_keys if k not in head_keys]
    )
    affected: dict[tuple[str, str], fl.ServerEndpoint] = {}
    for server in [*head_servers, *base_servers, *reached_servers]:
        affected.setdefault(_route_key(server), server)

    external: list[dict[str, Any]] = []
    external_info: dict[str, Any] = {}
    if include_external:
        try:
            external, external_info = _external_callers(root, list(affected.values()))
        except Exception as exc:  # noqa: BLE001 - the rest of the report is still useful
            logger.warning("External callers could not be read: %s", exc)
            notes.append("The other repos' endpoints could not be read, so external callers "
                         "are missing.")
    callers_by_route: dict[str, int] = {}
    for caller in external:
        reaches = caller["reaches"] or ""
        callers_by_route[reaches] = callers_by_route.get(reaches, 0) + 1

    endpoints = [
        _endpoint_row(s, external_callers=callers_by_route.get(
            _sanitize_name(f"{s.http_method} {s.route}"), 0))
        for s in affected.values()
    ]
    files = []
    for report in sorted(reports, key=lambda r: (-r.reached, r.path)):
        row: dict[str, Any] = {
            "file": _sanitize_name(report.path), "status": report.status,
            "changed_symbols": _names(report.changed), "added_symbols": len(report.added),
            "removed_symbols": _names(report.removed), "reached": report.reached,
            "reached_tests": report.reached_tests,
            "same_repo_files": report.same_repo_files,
        }
        if report.untested:
            row["untested"] = _names(report.untested)
        if report.note:
            row["note"] = report.note
        files.append(row)

    unparsed = sum(1 for r in reports if not r.parsed)
    if unparsed:
        notes.append(f"{unparsed} file(s) could not be parsed (see each file's note); they "
                     "are listed but contribute no symbols.")
    if any(s.file and s.file.endswith(".py") for s in head_servers):
        notes.append("Python routes are read from the changed files only; a router prefix set "
                     "in a file this pull request does not touch is not applied.")
    totals = {
        "files": len(changes),
        "parsed": found["parsed_files"],
        "symbols_changed": sum(len(r.changed) for r in reports),
        "symbols_added": sum(len(r.added) for r in reports),
        "symbols_removed": sum(len(r.removed) for r in reports),
        "reached": sum(r.reached for r in reports),
        "reached_tests": sum(r.reached_tests for r in reports),
        "endpoints": len(endpoints),
        "external_callers": len(external),
    }
    return {
        "status": "ok",
        "summary": (
            f"{totals['files']} files: {totals['symbols_changed']} symbols changed, "
            f"{totals['symbols_added']} added, {totals['symbols_removed']} removed; "
            f"{totals['endpoints']} endpoints affected, {totals['external_callers']} "
            "external callers."
        ),
        "depth": depth,
        "totals": totals,
        "files": files[:_MAX_FILES_SHOWN],
        "files_truncated": len(files) > _MAX_FILES_SHOWN,
        "contract_changes": contract[:_MAX_ENDPOINTS_SHOWN],
        "endpoints": endpoints[:_MAX_ENDPOINTS_SHOWN],
        "external_callers": external[:_MAX_CALLERS_SHOWN],
        "external_callers_total": len(external),
        "external_info": external_info,
        "schema_changes": found["schema"][:_MAX_ENDPOINTS_SHOWN],
        "notes": notes,
    }
