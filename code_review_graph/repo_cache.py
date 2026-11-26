"""Per-repo results of the cross-repo joins, kept until that repo's graph changes.

The ``stepfunction``, ``awssdk`` and Feign joins read facts about every registered repo (what
it defines, declares and is called) from its graph database and some of its files. A server
lives for days and a graph changes only when the indexer publishes a rebuild, so those reads
are kept in this process and used again while the repo's graph file is the same one.

A cached value is shared between calls and threads: callers must treat it as read-only.

``CRG_REPO_MEMO_ENTRIES`` (default 4096) bounds how many results are kept; ``0`` turns the
cache off. This module is not imported by the build code, so it does not change the index
fingerprint.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from .errors import GraphStoreError

logger = logging.getLogger(__name__)

ENTRIES_ENV = "CRG_REPO_MEMO_ENTRIES"
DEFAULT_ENTRIES = 4096

T = TypeVar("T")
R = TypeVar("R")

#: What ``parallel_map_budget`` returns for an item it did not start because the time ran out.
SKIPPED: Any = object()

Stamp = tuple[tuple[int, int, int], ...]


def memo_entries() -> int:
    """How many results the cache keeps; read on every call, so a change takes effect at once."""
    raw = os.environ.get(ENTRIES_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s.", ENTRIES_ENV, raw, DEFAULT_ENTRIES)
        return DEFAULT_ENTRIES
    if value < 0:
        logger.warning("Ignoring negative %s=%r; using %s.", ENTRIES_ENV, raw, DEFAULT_ENTRIES)
        return DEFAULT_ENTRIES
    return value


def graph_stamp(entry: Mapping[str, Any]) -> Stamp | None:
    """Identify the graph file of a registry entry as it is now, or ``None`` when unreadable.

    The stamp is the inode, modification time and size of the database file and of its
    write-ahead log when there is one, so a rebuild that replaces the file, or writes to it,
    gives a different stamp.
    """
    from .incremental import get_db_path

    try:
        db_path = get_db_path(Path(entry["path"]), read_only=True)
        parts = []
        for path in (db_path, db_path.with_name(db_path.name + "-wal")):
            try:
                info = path.stat()
            except FileNotFoundError:
                if path is db_path:
                    return None
                continue
            parts.append((info.st_ino, info.st_mtime_ns, info.st_size))
        return tuple(parts)
    except (OSError, ValueError, KeyError, GraphStoreError, sqlite3.Error):
        return None


class RepoMemo:
    """A bounded, thread-safe cache of one value per (namespace, repo path)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[tuple[str, str], tuple[Stamp, Any]] = OrderedDict()

    def get(
        self,
        namespace: str,
        entry: Mapping[str, Any],
        compute: Callable[[], T],
        cacheable: Callable[[T], bool] | None = None,
    ) -> T:
        """The value for *entry*, from the cache while its graph file is unchanged.

        *compute* runs outside the lock, so two threads may both compute a missing value; the
        later one wins. A value *cacheable* rejects (a read that failed) is returned but not
        kept, and nothing is kept for a repo whose graph file cannot be stamped.
        """
        limit = memo_entries()
        stamp = graph_stamp(entry) if limit > 0 else None
        if stamp is None:
            return compute()
        key = (namespace, str(entry["path"]))
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] == stamp:
                self._data.move_to_end(key)
                return hit[1]  # type: ignore[no-any-return]
        value = compute()
        if cacheable is not None and not cacheable(value):
            return value
        with self._lock:
            self._data[key] = (stamp, value)
            self._data.move_to_end(key)
            while len(self._data) > limit:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


#: The cache the cross-repo joins share.
MEMO = RepoMemo()


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """``[fn(item) for item in items]`` on up to *workers* threads, in the order of *items*.

    ``fn`` must not raise: the first exception ends the call. One worker runs on the calling
    thread, as before.
    """
    todo = list(items)
    if workers <= 1 or len(todo) <= 1:
        return [fn(item) for item in todo]
    with ThreadPoolExecutor(
        max_workers=min(workers, len(todo)), thread_name_prefix="crg-xrepo",
    ) as pool:
        return list(pool.map(fn, todo))


def parallel_map_budget(
    fn: Callable[[T], R],
    items: Sequence[T],
    workers: int,
    out_of_budget: Callable[[], bool],
) -> list[Any]:
    """Like :func:`parallel_map`, but a batch of *workers* items starts only within the budget.

    The budget is checked before each batch, so with one worker it is checked before each item,
    as a serial loop does; with more, up to ``workers - 1`` items may start after it ran out.
    An item not started is :data:`SKIPPED`.
    """
    todo = list(items)
    size = max(1, workers)
    results: list[Any] = []
    if not todo:
        return results
    with ThreadPoolExecutor(
        max_workers=min(size, len(todo)), thread_name_prefix="crg-xrepo",
    ) as pool:
        for start in range(0, len(todo), size):
            batch = todo[start:start + size]
            if out_of_budget():
                results.extend(SKIPPED for _ in batch)
            elif size == 1 or len(batch) == 1:
                results.extend(fn(item) for item in batch)
            else:
                results.extend(pool.map(fn, batch))
    return results
