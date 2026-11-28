"""A cache in front of ``graph_provenance``, so a tool call does not reopen SQLite and run Git.

``graph_provenance`` opens the graph read-only for three metadata rows and runs
``git rev-parse --verify HEAD``. ``with_provenance`` does that on every tool call, which costs
tens of milliseconds idle and seconds under CPU contention. The answer only changes when the
graph file or the checked-out commit changes, so it is kept per
``(db_path, graph file state, HEAD state)``:

* the graph file state is ``analysis_snapshot.file_state`` (size and mtime of the database and
  of its WAL, a header-only WAL ignored);
* the HEAD state is the stat of ``HEAD``, of the ref it points to, and of ``packed-refs`` (a
  linked worktree's ``.git`` file is followed to its own HEAD and the shared refs). A new commit
  rewrites the branch ref, a checkout rewrites ``HEAD``, so either moves the key.

Anything that cannot be read cheaply (no state, a failed or partial provenance read) is not
cached: the call falls through to ``graph_provenance`` as before. ``age_seconds`` depends on the
clock, so it is recomputed on every hit from the cached ``updated_at``.

Query-only: nothing the build runs imports this module. Thread safety: one ``threading.Lock``
guards the dict; the (slow) read itself runs outside the lock, so two threads that miss together
both read and the later write wins with an equal value.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 64

_lock = threading.Lock()
_cache: dict[tuple[Any, ...], dict[str, Any]] = {}


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _git_dirs(root: Path) -> tuple[Path, Path] | None:
    """The per-worktree Git directory and the one that holds the shared refs."""
    dot_git = root / ".git"
    try:
        if dot_git.is_dir():
            return dot_git, dot_git
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(text[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir, git_dir
    common_dir = Path(common)
    return git_dir, (common_dir if common_dir.is_absolute() else git_dir / common_dir)


def head_state(root: Path) -> tuple[Any, ...] | None:
    """Fingerprint of what ``HEAD`` resolves to, or ``None`` when it cannot be read."""
    dirs = _git_dirs(root)
    if dirs is None:
        return None
    git_dir, common_dir = dirs
    head_file = git_dir / "HEAD"
    head_stat = _stat(head_file)
    if head_stat is None:
        return None
    try:
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    ref_stat: tuple[int, int] | None = None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        ref_stat = _stat(git_dir / ref) or _stat(common_dir / ref)
    return (head_stat, head, ref_stat, _stat(common_dir / "packed-refs"))


def _with_fresh_age(entry: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(entry)
    updated_at = provenance.get("updated_at")
    if isinstance(updated_at, str):
        try:
            built_at = datetime.fromisoformat(updated_at)
            now = datetime.now(tz=built_at.tzinfo)
            provenance["age_seconds"] = max(0, int((now - built_at).total_seconds()))
        except (OverflowError, TypeError, ValueError):
            pass
    return provenance


def cached_graph_provenance(repo_root: str | None = None) -> dict[str, Any] | None:
    """``graph_provenance`` that reuses its answer while the graph and HEAD are unchanged."""
    from .analysis_snapshot import file_state
    from .tools._common import _resolve_root, get_db_path, graph_provenance

    key: tuple[Any, ...] | None = None
    root: Path | None = None
    try:
        root = _resolve_root(repo_root)
        db_path = get_db_path(root, read_only=True)
        graph_state = file_state(db_path)
        git_state = head_state(root) if (root / ".git").exists() else ()
        if graph_state is not None and git_state is not None:
            key = (str(db_path), graph_state, git_state)
    except Exception:
        logger.debug("Could not fingerprint the graph for provenance", exc_info=True)
    if key is None:
        return graph_provenance(repo_root)
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return _with_fresh_age(hit)
    provenance = graph_provenance(repo_root)
    # ``graph_provenance`` swallows its errors and returns None (or leaves ``head_sha`` out when
    # Git failed), so only a complete answer is kept: a failure is retried on the next call.
    complete = provenance is not None and (key[2] == () or "head_sha" in provenance)
    if provenance is not None and complete:
        with _lock:
            if len(_cache) >= _MAX_ENTRIES:
                _cache.clear()
            _cache[key] = dict(provenance)
    return provenance


def with_provenance(result: Any, repo_root: str | None = None) -> Any:
    """Attach a ``_graph`` envelope without changing existing fields (cached read)."""
    if not isinstance(result, dict) or "_graph" in result:
        return result
    provenance = cached_graph_provenance(repo_root)
    if provenance:
        result["_graph"] = provenance
    return result


def clear() -> None:
    """Drop every cached answer (for tests)."""
    with _lock:
        _cache.clear()
