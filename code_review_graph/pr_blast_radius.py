"""Blast radius of a pull request, worked out from its changed files alone.

A review bot cannot index every pull request: the graph a server holds is built from the
default branch, so a file the pull request adds is not in it. This module reads only the
files the pull request touches, straight from Git, parses each one in memory with the same
parser a build uses, and compares the result with the graph of the base. Nothing is
written to the graph.

* A symbol that the base graph also has and that a changed line overlaps is **changed**;
  the graph then says what reaches it.
* A symbol only the new file has is **added**: nothing in the base calls it yet.
* A symbol only the base graph has, in a file the pull request changes or deletes, is
  **removed**: its callers in the base graph are the ones that would break.
* Routes come from the parse, so a route the pull request adds is listed even though no
  graph holds it.

Where the answer rests on a guess the result says so (``notes``): a stacked pull request,
a Python route whose router prefix lives in a file the pull request does not touch, a
shallow clone without a merge base.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .changes import _SAFE_GIT_REF, DiffRanges, _parse_unified_diff
from .constants import (
    GIT_TIMEOUT,
    IMPACT_DEFAULT_EDGE_DIRECTION,
    IMPACT_DIRECTION_INCOMING,
    IMPACT_EDGE_DIRECTIONS,
)
from .endpoint_index import extract_endpoints
from .graph import GraphNode, GraphStore

logger = logging.getLogger(__name__)

#: Files parsed for one pull request. Past this the rest are listed unparsed.
MAX_PARSED_FILES = 300
#: A file larger than this is listed, not parsed.
MAX_FILE_BYTES = 1_000_000
#: Bytes of file content read for one pull request; past this the rest are listed unparsed.
MAX_TOTAL_BYTES = 32_000_000
#: Output Git may produce for one command (a diff, a file list) before the run is refused.
MAX_GIT_OUTPUT = 16_000_000
#: Changed files looked at for one pull request; the rest are counted, not read.
MAX_CHANGED_FILES = 5_000
#: Hops the reach walk follows from a changed symbol.
DEFAULT_DEPTH = 3
#: Nodes one reach walk may visit.
MAX_REACHED_NODES = 5_000
#: Nodes all the reach walks of one request may visit together.
MAX_REACH_VISITS = 50_000

#: Node kinds that count as a symbol a pull request can change, add or remove.
_SYMBOL_KINDS = ("Function", "Test", "Class")
#: Of those, the kinds a changed line marks as changed. A class holds its methods, so a
#: line inside a method would otherwise mark the whole class.
_CHANGEABLE_KINDS = ("Function", "Test")

#: A Flyway-style migration: SQL under a ``migration`` directory.
_MIGRATION_RE = re.compile(r"(^|/)(db/)?migrations?/[^/]+\.sql$", re.IGNORECASE)
_SQL_STATEMENT_RE = re.compile(
    r"\b(create(?:\s+unique)?\s+(?:table|index|view|sequence)(?:\s+if\s+not\s+exists)?"
    r"|alter\s+table(?:\s+if\s+exists)?|drop\s+(?:table|index|view|column|constraint)"
    r"(?:\s+if\s+exists)?)\s+([\w.\"]+)",
    re.IGNORECASE,
)


_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]*@")


def _scrub(text: str) -> str:
    """Drop the user and password of any URL in *text*; Git echoes the remote on a failed fetch."""
    return _URL_USERINFO_RE.sub(r"\1", text)


class PullRequestError(ValueError):
    """The pull request's files could not be read; the message says why."""


@dataclass(frozen=True)
class FileChange:
    """One file the pull request touches."""

    status: str  # added, modified, deleted, renamed
    path: str
    old_path: Optional[str] = None


_STATUS_NAMES = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "added",
                 "T": "modified"}


def _git(
    repo_root: str, *args: str, binary: bool = False, stdin: Optional[bytes] = None,
    limit: int = MAX_GIT_OUTPUT, real: bool = False,
) -> Any:
    """Run one read-only Git command in *repo_root*; raise ``PullRequestError`` on failure.

    Output past *limit* bytes stops the command and is refused, so a very large diff or
    blob cannot fill the server's memory.

    While a scratch session holds fetched objects (:func:`scratch_session`) the command runs
    against that throwaway repository, which borrows the checkout's objects; *real* runs it
    against the checkout itself (for its branches and remote settings).
    """
    scratch = None if real else _SCRATCH_DIR.get()
    env = {**os.environ, "GIT_DIR": scratch} if scratch else None
    try:
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(  # nosec B603 B607 - fixed argv, no shell
                ["git", *args],
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=errors, cwd=repo_root, env=env,
            )
            timer = threading.Timer(GIT_TIMEOUT, proc.kill)
            timer.start()
            try:
                if stdin is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(stdin)
                    except BrokenPipeError:
                        pass
                    finally:
                        proc.stdin.close()
                assert proc.stdout is not None
                out = proc.stdout.read(limit + 1)
                if len(out) > limit:
                    proc.kill()
                    proc.wait()
                    raise PullRequestError(f"git {args[0]} produced more than {limit} bytes")
                code = proc.wait()
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
            errors.seek(0)
            detail = errors.read().decode("utf-8", errors="replace").strip().splitlines()
    except OSError as exc:
        raise PullRequestError("git could not be run") from exc
    if code != 0:
        raise PullRequestError(
            f"git {args[0]} failed: {_scrub(detail[-1])[:200] if detail else 'no detail'}"
        )
    return out if binary else out.decode("utf-8", errors="replace")


_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")


def _safe_ref(ref: str, what: str) -> str:
    """A ref that is safe to hand to Git: no leading dash, only ref characters."""
    if not ref or ref.startswith("-") or not _SAFE_GIT_REF.fullmatch(ref):
        raise PullRequestError(f"{what} is not a usable Git ref: {ref!r}")
    return ref


def resolve_commit(repo_root: str, ref: str, what: str) -> str:
    """The commit a ref names, as a full SHA; every later Git call uses only the SHA.

    A full SHA and a ref under ``refs/crg/`` are looked up where fetched objects live (the
    scratch repository, when a session is open); any other name is looked up in the checkout.
    The error names the ref that failed and carries Git's own message.
    """
    _safe_ref(ref, what)
    scratch = bool(_COMMIT_RE.fullmatch(ref)) or ref.startswith("refs/crg/")
    try:
        sha = _git(
            repo_root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
            limit=256, real=not scratch,
        ).strip()
    except PullRequestError as exc:
        raise PullRequestError(f"{what} {ref!r} could not be resolved: {exc}") from exc
    if not _COMMIT_RE.fullmatch(sha):
        raise PullRequestError(f"{what} does not name a commit: {ref!r}")
    return sha


_SCRATCH_DIR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "crg_pr_scratch_dir", default=None,
)
_SESSION: contextvars.ContextVar[Optional["ScratchSession"]] = contextvars.ContextVar(
    "crg_pr_scratch_session", default=None,
)
SCRATCH_PREFIX = "crg-pr-"
#: A scratch directory older than this is left over from a call that never finished.
SCRATCH_MAX_AGE = 3600.0
#: Refs an earlier version wrote into the checkout itself; removed when found.
_LEGACY_REF_PREFIX = "refs/crg/"


class ScratchSession:
    """A throwaway bare repository that fetched objects land in, removed when the call ends.

    It borrows the checkout's object store (Git alternates), so only the objects the checkout
    lacks are written, and they go to the temporary directory, never into the checkout. The
    checkout gains no refs and no objects, so repeated calls cannot fill the server's disk.
    """

    def __init__(self) -> None:
        self.path: Optional[str] = None

    def ensure(self, repo_root: str) -> str:
        if self.path:
            return self.path
        path = tempfile.mkdtemp(prefix=SCRATCH_PREFIX)
        self.path = path
        try:
            common = _git(repo_root, "rev-parse", "--git-common-dir", limit=4096, real=True)
            objects = os.path.join(os.path.abspath(os.path.join(repo_root, common.strip())),
                                   "objects")
            subprocess.run(  # nosec B603 B607 - fixed argv, no shell
                ["git", "init", "--bare", "--quiet", path], check=True, capture_output=True,
                timeout=GIT_TIMEOUT,
            )
            info = os.path.join(path, "objects", "info")
            os.makedirs(info, exist_ok=True)
            with open(os.path.join(info, "alternates"), "w", encoding="utf-8") as handle:
                handle.write(objects + "\n")
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise PullRequestError("could not set up a scratch repository for the fetch") from exc
        _SCRATCH_DIR.set(path)
        return path

    def close(self) -> None:
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None
        _SCRATCH_DIR.set(None)


def _sweep_stale_scratch() -> None:
    """Remove scratch directories a killed call left behind."""
    root = tempfile.gettempdir()
    cutoff = time.time() - SCRATCH_MAX_AGE
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(SCRATCH_PREFIX):
            continue
        full = os.path.join(root, name)
        try:
            if os.path.isdir(full) and os.path.getmtime(full) < cutoff:
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            continue


def _drop_legacy_refs(repo_root: str) -> None:
    """Delete ``refs/crg/*`` refs an earlier version fetched into the checkout, if any."""
    try:
        out = _git(repo_root, "for-each-ref", "--format=%(refname)", _LEGACY_REF_PREFIX,
                   limit=1_000_000, real=True)
    except PullRequestError:
        return
    refs = [line for line in out.splitlines() if line.startswith(_LEGACY_REF_PREFIX)]
    if not refs:
        return
    commands = "".join(f"delete {ref}\n" for ref in refs).encode()
    try:
        _git(repo_root, "update-ref", "--stdin", stdin=commands, limit=1024, real=True)
    except PullRequestError as exc:
        logger.warning("could not remove %d leftover refs/crg refs: %s", len(refs), exc)


@contextlib.contextmanager
def scratch_session(repo_root: str) -> Iterator[ScratchSession]:
    """Everything fetched for one request lives in this session and is removed on exit."""
    _sweep_stale_scratch()
    _drop_legacy_refs(repo_root)
    session = ScratchSession()
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)
        session.close()


def _scratch_fetch(repo_root: str, refspec: str, what: str) -> None:
    session = _SESSION.get()
    if session is None:
        raise PullRequestError("internal error: a fetch needs a scratch session")
    session.ensure(repo_root)
    url = _git(repo_root, "config", "--get", "remote.origin.url", limit=4096, real=True).strip()
    if not url:
        raise PullRequestError(
            f"{what} is not in this checkout and there is no origin to fetch it from"
        )
    _git(repo_root, "fetch", "--no-tags", "--quiet", url, refspec)


def ensure_commit(repo_root: str, ref: str, what: str) -> str:
    """The commit *ref* names, fetching it from ``origin`` when the checkout lacks it.

    A branch name or full SHA the checkout already has is used as it stands. Otherwise a full
    SHA is fetched by value and a branch name from ``refs/heads/``. An abbreviated SHA cannot
    be fetched, and the error says so.
    """
    _safe_ref(ref, what)
    try:
        return resolve_commit(repo_root, ref, what)
    except PullRequestError as first:
        local_error = first
    if _COMMIT_RE.fullmatch(ref):
        dest, spec = f"refs/crg/{what}/{ref}", f"{ref}:refs/crg/{what}/{ref}"
    elif re.fullmatch(r"[0-9a-fA-F]{7,39}", ref):
        raise PullRequestError(
            f"{what} {ref!r} is an abbreviated commit id that this checkout does not have; "
            "give the full 40-character SHA so it can be fetched"
        ) from local_error
    else:
        dest, spec = f"refs/crg/{what}/branch", f"+refs/heads/{ref}:refs/crg/{what}/branch"
    try:
        _scratch_fetch(repo_root, spec, what)
        return resolve_commit(repo_root, dest, what)
    except PullRequestError as exc:
        raise PullRequestError(
            f"{what} {ref!r} is not in this checkout and could not be fetched from origin: {exc}"
        ) from exc


def fetch_pull_request(repo_root: str, pr: int, base: Optional[str]) -> tuple[str, str]:
    """Fetch ``refs/pull/<pr>/head`` (and the base branch) and return ``(base_sha, head_sha)``.

    The objects land in the request's scratch repository, so the checkout's refs, branches,
    objects and working tree are not touched. *base* is a branch name; omitted, the remote's
    default branch is used. Call it inside :func:`scratch_session`.
    """
    if not isinstance(pr, int) or pr <= 0:
        raise PullRequestError("pr must be a positive pull request number")
    pull_ref = f"refs/crg/pull/{pr}"
    _scratch_fetch(repo_root, f"+pull/{pr}/head:{pull_ref}", "head")
    head = resolve_commit(repo_root, pull_ref, "head")
    if base:
        _safe_ref(base, "base")
        base_ref = f"refs/crg/base/{pr}"
        try:
            _scratch_fetch(repo_root, f"+refs/heads/{base}:{base_ref}", "base")
            return resolve_commit(repo_root, base_ref, "base"), head
        except PullRequestError as exc:
            raise PullRequestError(
                f"base {base!r} could not be fetched from origin: {exc}"
            ) from exc
    return resolve_commit(repo_root, default_branch(repo_root), "base"), head


def default_branch(repo_root: str) -> str:
    """The remote's default branch as a ref name (``origin/main`` when Git does not say)."""
    try:
        return _git(
            repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", limit=1024,
            real=True,
        ).strip()
    except PullRequestError:
        return "origin/main"


def changed_files(repo_root: str, base_ref: str, head: str) -> tuple[list[FileChange], bool]:
    """The files that differ between *base_ref* and *head*, and whether a merge base was used.

    Three dots (changes since the branches diverged) when Git has the merge base, else two
    dots with the flag set to ``False``; a shallow clone often lacks the merge base.
    """
    base_ref = resolve_commit(repo_root, base_ref, "base")
    head = resolve_commit(repo_root, head, "head")
    merge_based = True
    try:
        out = _git(repo_root, "diff", "--name-status", "-z", "-M", f"{base_ref}...{head}", "--")
    except PullRequestError:
        merge_based = False
        out = _git(repo_root, "diff", "--name-status", "-z", "-M", base_ref, head, "--")
    parts = out.split("\0")
    changes: list[FileChange] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index][0]
        if status in ("R", "C"):
            old, new = parts[index + 1], parts[index + 2]
            index += 3
            changes.append(FileChange("renamed" if status == "R" else "added", new, old))
        else:
            changes.append(FileChange(_STATUS_NAMES.get(status, "modified"), parts[index + 1]))
            index += 2
    return changes, merge_based


def diff_ranges(repo_root: str, base_ref: str, head: str, merge_based: bool) -> DiffRanges:
    """New-side changed line ranges per file."""
    base_ref = resolve_commit(repo_root, base_ref, "base")
    head = resolve_commit(repo_root, head, "head")
    dots = "..." if merge_based else ".."
    out = _git(repo_root, "diff", "--unified=0", "--no-color", f"{base_ref}{dots}{head}", "--")
    return _parse_unified_diff(out)


def git_file_reader(repo_root: str, head: str) -> Callable[[str], Optional[bytes]]:
    """A reader of a file's bytes at *head*; ``None`` when it cannot be read or is too big."""
    head = resolve_commit(repo_root, head, "head")

    def read(path: str) -> Optional[bytes]:
        try:
            return _git(
                repo_root, "show", f"{head}:{path}", binary=True, limit=MAX_FILE_BYTES + 1,
            )
        except PullRequestError:
            return None

    return read


def blob_sizes(repo_root: str, head: str, paths: list[str]) -> dict[str, Optional[int]]:
    """The size in bytes of each path at *head* (``None`` if absent), in one Git call.

    Sizes are asked for before any file is read, so a huge blob is never pulled into memory.
    """
    head = resolve_commit(repo_root, head, "head")
    if not paths:
        return {}
    request = "".join(f"{head}:{path}\n" for path in paths if "\n" not in path).encode()
    try:
        out = _git(repo_root, "cat-file", "--batch-check", stdin=request, limit=MAX_GIT_OUTPUT)
    except PullRequestError:
        return {}
    sizes: dict[str, Optional[int]] = {}
    valid = [path for path in paths if "\n" not in path]
    for path, line in zip(valid, out.splitlines()):
        fields = line.split()
        sizes[path] = int(fields[2]) if len(fields) == 3 and fields[2].isdigit() else None
    return sizes


def _as_graph_node(info: Any) -> GraphNode:
    """A parser node as the graph-node shape ``extract_endpoints`` reads."""
    return GraphNode(
        id=0, kind=info.kind, name=info.name,
        qualified_name=f"{info.file_path}::{info.name}",
        file_path=info.file_path, line_start=info.line_start or 0, line_end=info.line_end or 0,
        language=info.language or "", parent_name=info.parent_name, params=info.params,
        return_type=info.return_type, is_test=bool(info.is_test), file_hash=None,
        extra=dict(info.extra or {}),
    )


def _key(kind: str, name: str, parent: Optional[str]) -> tuple[str, str, str]:
    return (kind, name, parent or "")


@dataclass
class FileReport:
    """What one changed file does to the graph."""

    path: str
    status: str
    changed: list[str]
    added: list[str]
    removed: list[str]
    seeds: list[str]
    changed_qns: list[str] = None  # type: ignore[assignment]
    parsed: bool = True
    note: Optional[str] = None
    reached: int = 0
    reached_tests: int = 0
    same_repo_files: int = 0
    untested: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.untested is None:
            self.untested = []
        if self.changed_qns is None:
            self.changed_qns = []


def reach_from(
    store: GraphStore, seeds: list[str], depth: int = DEFAULT_DEPTH,
    max_nodes: int = MAX_REACHED_NODES, budget: Optional[list[int]] = None,
) -> dict[str, int]:
    """Qualified names that depend on *seeds*, with the hop count of each (seeds are 0).

    Follows the same edge kinds and directions as the graph's impact radius, from the base
    graph. A call bound to no indexed node cannot be followed, as there.

    *budget* is a one-item list shared by every walk of a request: each node visited takes
    one from it, and the walk stops when it is spent, so many small walks cannot add up to
    more work than one large one.
    """
    hops = {qn: 0 for qn in seeds}
    frontier = list(seeds)
    for hop in range(1, max(0, depth) + 1):
        following: list[str] = []
        for qn in frontier:
            for edge in store.get_edges_by_target(qn):
                direction = IMPACT_EDGE_DIRECTIONS.get(edge.kind, IMPACT_DEFAULT_EDGE_DIRECTION)
                if direction != IMPACT_DIRECTION_INCOMING:
                    continue
                if budget is not None and budget[0] <= 0:
                    return hops
                if edge.source_qualified not in hops and len(hops) < max_nodes:
                    hops[edge.source_qualified] = hop
                    following.append(edge.source_qualified)
                    if budget is not None:
                        budget[0] -= 1
        frontier = following
        if not frontier:
            break
    return hops


def sql_changes(path: str, text: str, limit: int = 25) -> list[dict[str, str]]:
    """The DDL statements a migration file holds, as ``{"file", "change", "target"}``."""
    rows: list[dict[str, str]] = []
    for match in _SQL_STATEMENT_RE.finditer(text):
        change = " ".join(match.group(1).lower().split())
        rows.append({"file": path, "change": change, "target": match.group(2).strip('"')})
        if len(rows) >= limit:
            break
    return rows


def analyse(
    store: GraphStore,
    repo_root: str,
    changes: list[FileChange],
    ranges: DiffRanges,
    read_head: Callable[[str], Optional[bytes]],
    *,
    depth: int = DEFAULT_DEPTH,
    parser: Any = None,
    sizes: Optional[dict[str, Optional[int]]] = None,
) -> dict[str, Any]:
    """Compare the pull request's files with the base *store* and return the raw findings.

    Returns ``files`` (a :class:`FileReport` per file), ``reached`` (qualified name to
    hops), ``endpoints_head`` (server endpoints of the new and changed files),
    ``endpoints_base`` (those the base graph held in the same files), ``endpoints_reached``
    (nodes of kind ``Endpoint`` the changed symbols reach) and ``schema`` rows.

    A file is read only after its size (*sizes*, from :func:`blob_sizes`) is known to be within
    the limit, the parse limit has room, and the per-request byte budget is not spent.
    """
    if parser is None:
        from .parser import CodeParser

        parser = CodeParser()
    root = Path(repo_root)
    reports: list[FileReport] = []
    head_endpoint_nodes: list[GraphNode] = []
    base_endpoint_nodes: list[GraphNode] = []
    schema: list[dict[str, str]] = []
    all_seeds: list[str] = []
    parsed_count = 0
    bytes_read = 0

    def fetch(path: str) -> tuple[Optional[bytes], Optional[str]]:
        """The file's bytes, or why they were not read; checks come before the read."""
        nonlocal bytes_read
        size = sizes.get(path) if sizes else None
        if size is not None and size > MAX_FILE_BYTES:
            return None, "larger than the parse limit"
        if bytes_read >= MAX_TOTAL_BYTES:
            return None, "past the per-request byte limit"
        blob = read_head(path)
        if blob is None:
            return None, "could not read the file at the head"
        if len(blob) > MAX_FILE_BYTES:
            return None, "larger than the parse limit"
        bytes_read += len(blob)
        return blob, None

    for change in changes:
        base_path = change.old_path or change.path
        absolute = str(root / change.path)
        base_nodes = store.get_nodes_by_file(str(root / base_path)) or store.get_nodes_by_file(
            base_path
        )
        base_symbols = [n for n in base_nodes if n.kind in _SYMBOL_KINDS]
        base_endpoint_nodes.extend(n for n in base_nodes if n.kind == "Endpoint")
        report = FileReport(change.path, change.status, [], [], [], [])

        head_symbols: list[Any] = []
        if change.status != "deleted":
            is_migration = bool(_MIGRATION_RE.search(change.path))
            room_to_parse = parsed_count < MAX_PARSED_FILES
            if not room_to_parse:
                report.parsed, report.note = False, "past the per-request parse limit"
            blob: Optional[bytes] = None
            if room_to_parse or is_migration:
                blob, why = fetch(change.path)
                if blob is None and room_to_parse:
                    report.parsed, report.note = False, why
            if blob is not None and is_migration:
                schema.extend(sql_changes(change.path, blob.decode("utf-8", errors="replace")))
            if blob is not None and room_to_parse:
                try:
                    nodes, _edges = parser.parse_bytes(Path(absolute), blob)
                except Exception as exc:  # noqa: BLE001 - a parse failure must not fail the PR
                    logger.warning("Could not parse %s: %s", change.path, exc)
                    report.parsed, report.note = False, "the parser could not read it"
                else:
                    parsed_count += 1
                    head_symbols = [n for n in nodes if n.kind in _SYMBOL_KINDS]
                    head_endpoint_nodes.extend(
                        _as_graph_node(n) for n in nodes if n.kind == "Endpoint"
                    )
        if not report.parsed:
            reports.append(report)
            continue

        by_key: dict[tuple[str, str, str], list[GraphNode]] = {}
        for node in base_symbols:
            by_key.setdefault(_key(node.kind, node.name, node.parent_name), []).append(node)
        head_keys = set()
        file_ranges = ranges.get(change.path, [])
        for sym in head_symbols:
            key = _key(sym.kind, sym.name, sym.parent_name)
            head_keys.add(key)
            matches = by_key.get(key)
            if not matches:
                report.added.append(sym.name)
            elif sym.kind in _CHANGEABLE_KINDS and any(
                sym.line_start <= end and sym.line_end >= start for start, end in file_ranges
            ):
                report.changed.append(sym.name)
                report.seeds.extend(m.qualified_name for m in matches)
                report.changed_qns.extend(m.qualified_name for m in matches)
        for key, nodes_ in by_key.items():
            if key not in head_keys:
                report.removed.append(key[1])
                report.seeds.extend(n.qualified_name for n in nodes_)
        all_seeds.extend(report.seeds)
        reports.append(report)

    budget = [MAX_REACH_VISITS]
    reached = (
        reach_from(store, list(dict.fromkeys(all_seeds)), depth, budget=budget)
        if all_seeds else {}
    )

    reached_endpoints: dict[str, GraphNode] = {}
    for qn in reached:
        for edge in store.get_edges_by_source(qn):
            if edge.kind == "HANDLES" and edge.target_qualified not in reached_endpoints:
                target = store.get_node(edge.target_qualified)
                if target is not None and target.kind == "Endpoint":
                    reached_endpoints[edge.target_qualified] = target

    changed_paths = {c.path for c in changes}
    changed_abs = {str(root / p) for p in changed_paths} | changed_paths
    for report in reports:
        own = reach_from(store, report.seeds, depth, budget=budget) if report.seeds else {}
        seeds = set(report.seeds)
        files: set[str] = set()
        production = tests = 0
        for qn in own:
            reached_node = store.get_node(qn)
            if reached_node is None:
                continue
            if reached_node.file_path not in changed_abs:
                files.add(reached_node.file_path)
            if qn in seeds:
                continue
            if reached_node.is_test or reached_node.kind == "Test":
                tests += 1
            else:
                production += 1
        report.reached, report.reached_tests = production, tests
        report.same_repo_files = len(files)
        for qn in report.seeds:
            seed_node = store.get_node(qn)
            if seed_node is None or seed_node.kind not in _CHANGEABLE_KINDS:
                continue
            if seed_node.kind == "Test":
                continue
            if not any(e.kind == "TESTED_BY" for e in store.get_edges_by_source(qn)):
                report.untested.append(seed_node.name)

    _clients_h, servers_head = extract_endpoints(head_endpoint_nodes, root)
    _clients_b, servers_base = extract_endpoints(base_endpoint_nodes, root)
    _c, servers_reached = extract_endpoints(list(reached_endpoints.values()), root)
    return {
        "files": reports,
        "reached": reached,
        "endpoints_head": servers_head,
        "endpoints_base": servers_base,
        "endpoints_reached": servers_reached,
        "schema": schema,
        "parsed_files": parsed_count,
        "reach_truncated": budget[0] <= 0,
    }
