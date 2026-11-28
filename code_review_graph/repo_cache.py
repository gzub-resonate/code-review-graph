"""Per-repo results of the cross-repo joins, kept until that repo's graph changes.

The ``stepfunction``, ``awssdk`` and Feign joins read facts about every registered repo (what
it defines, declares and is called) from its graph database and some of its files. A server
lives for days and a graph changes only when the indexer publishes a rebuild, so those reads
are kept in this process and used again while the repo's graph file is the same one.

Parallel reads of several repos run on one bounded thread pool shared by all calls (size
``CRG_CROSS_REPO_WORKERS``, default the CPU count capped at 4), so concurrent calls do not each
start their own threads.

A cached value is shared between calls and threads: callers must treat it as read-only.

Concurrent calls that need the same missing value share one computation (``SingleFlight``):
the first computes, the others wait for its value or exception, and a waiter that has waited
``CRG_SINGLE_FLIGHT_WAIT_SECONDS`` (default 240) computes for itself instead of hanging.

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

from .constants import CROSS_REPO_WORKERS_ENV, CROSS_REPO_WORKERS_MAX, env_float, env_int
from .errors import GraphStoreError

logger = logging.getLogger(__name__)

ENTRIES_ENV = "CRG_REPO_MEMO_ENTRIES"
DEFAULT_ENTRIES = 4096

T = TypeVar("T")
R = TypeVar("R")

#: What ``parallel_map_budget`` returns for an item it did not start because the time ran out.
SKIPPED: Any = object()

Stamp = tuple[tuple[Any, ...], ...]

WAIT_ENV = "CRG_SINGLE_FLIGHT_WAIT_SECONDS"
DEFAULT_WAIT_SECONDS = 240.0
#: How many whole-call results ``ResultCache`` keeps by default.
DEFAULT_RESULT_ENTRIES = 16


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


def single_flight_wait() -> float:
    """Seconds a waiter gives the computing call before it computes for itself."""
    value = env_float(WAIT_ENV, DEFAULT_WAIT_SECONDS)
    return value if value >= 0 else DEFAULT_WAIT_SECONDS


class _Flight:
    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


class SingleFlight:
    """Run one computation per key at a time; concurrent callers share its outcome.

    The first caller for a key computes. Others wait for it and get its value, or raise its
    exception. A waiter that is not answered within *timeout* seconds computes for itself, so a
    stuck leader never blocks it for good. Nothing is kept once the computation ends: pair it
    with a cache to reuse the value later.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[Any, _Flight] = {}

    def do(self, key: Any, compute: Callable[[], T], timeout: float | None = None) -> T:
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
        if leader:
            try:
                flight.value = compute()
                return flight.value  # type: ignore[no-any-return]
            except BaseException as exc:
                flight.error = exc
                raise
            finally:
                with self._lock:
                    if self._flights.get(key) is flight:
                        del self._flights[key]
                flight.done.set()
        if not flight.done.wait(single_flight_wait() if timeout is None else timeout):
            logger.warning("Gave up waiting for a shared computation; computing it here.")
            return compute()
        if flight.error is not None:
            raise flight.error
        return flight.value  # type: ignore[no-any-return]


class ResultCache:
    """A small LRU of whole-call results, built single-flight.

    Unlike :class:`RepoMemo` the key is chosen by the caller (it must name everything the
    result depends on, repo graph stamps included), and a hit returns the stored object, so
    callers copy what they change.
    """

    def __init__(self, entries: int = DEFAULT_RESULT_ENTRIES) -> None:
        self._entries = entries
        self._lock = threading.Lock()
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._flight = SingleFlight()

    def get(
        self,
        key: Any,
        compute: Callable[[], T],
        cacheable: Callable[[T], bool] | None = None,
    ) -> T:
        if self._entries <= 0 or memo_entries() <= 0:
            return compute()
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]  # type: ignore[no-any-return]

        def build() -> T:
            value = compute()
            if cacheable is None or cacheable(value):
                with self._lock:
                    self._data[key] = value
                    self._data.move_to_end(key)
                    while len(self._data) > self._entries:
                        self._data.popitem(last=False)
            return value

        return self._flight.do(key, build)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


#: Size of the header at the start of a SQLite ``-wal`` file; a file this small has no frames.
WAL_HEADER_BYTES = 32
_STAMP_LOCK = threading.Lock()
#: ``db path -> (file signature, stamp)``: the stamp last read for a graph file as it looked then.
_STAMP_MEMO: dict[str, tuple[tuple[Any, ...], Stamp]] = {}
_STAMP_MEMO_MAX = 8192


def _file_signature(db_path: Path) -> tuple[Any, ...] | None:
    """Inode, mtime and size of the database and of its ``-wal``; ``None`` without a database.

    A ``-wal`` of at most :data:`WAL_HEADER_BYTES` holds no frames, only the header SQLite
    writes whenever a connection opens the database, so it counts as absent: its mtime moves on
    every open by any reader and would otherwise never match twice.
    """
    parts: list[Any] = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            info = path.stat()
        except FileNotFoundError:
            if path is db_path:
                return None
            parts.append(None)
            continue
        if path is not db_path and info.st_size <= WAL_HEADER_BYTES:
            parts.append(None)
            continue
        parts.append((info.st_ino, info.st_mtime_ns, info.st_size))
    return tuple(parts)


def _graph_marker(db_path: Path) -> tuple[Any, ...] | None:
    """What the graph holds, read from the file: build time, head sha, id high-water marks, counts.

    Opened ``mode=ro`` and only ever read, so it adds no write to the database. ``None`` when
    the file cannot be read as a graph (the caller then falls back to the file signature).
    """
    has_sidecar = any(
        db_path.with_name(db_path.name + suffix).exists() for suffix in ("-wal", "-shm")
    )
    uri = f"{db_path.resolve().as_uri()}?mode=ro" + ("" if has_sidecar else "&immutable=1")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
    except sqlite3.Error:
        return None
    try:
        meta = dict(conn.execute(
            "SELECT key, value FROM metadata WHERE key IN ('last_updated', 'git_head_sha')"
        ).fetchall())
        counts: list[Any] = []
        for table in ("nodes", "edges"):
            high, rows = conn.execute(f"SELECT MAX(id), COUNT(*) FROM {table}").fetchone()
            counts.extend((high, rows))
        return (meta.get("last_updated"), meta.get("git_head_sha"), *counts)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def graph_stamp(entry: Mapping[str, Any]) -> Stamp | None:
    """Identify the graph of a registry entry as it is now, or ``None`` when unreadable.

    The stamp is the database file's inode plus what the graph holds (build time, head sha, the
    highest node and edge ids and their counts). It does not use the modification time or size
    of the file or of its ``-wal``: an indexer writing, a checkpoint, or any reader opening the
    database moves those without changing the graph, and each such move used to throw away every
    cached cross-repo result. A rebuild that replaces the file changes the inode; an update
    changes the build time or the ids and counts. The marker is only read again when the file
    signature moved, so a call over 100+ repos costs ``stat`` calls. A graph whose marker cannot
    be read falls back to the old file signature.
    """
    from .incremental import get_db_path

    try:
        db_path = get_db_path(Path(entry["path"]), read_only=True)
        signature = _file_signature(db_path)
        if signature is None:
            return None
        key = str(db_path)
        with _STAMP_LOCK:
            known = _STAMP_MEMO.get(key)
        if known is not None and known[0] == signature:
            return known[1]
        marker = _graph_marker(db_path)
        inode = signature[0][0]
        stamp: Stamp = (("graph", inode, *marker),) if marker is not None else (
            ("file", *signature),
        )
        with _STAMP_LOCK:
            if len(_STAMP_MEMO) >= _STAMP_MEMO_MAX:
                _STAMP_MEMO.clear()
            _STAMP_MEMO[key] = (signature, stamp)
        return stamp
    except (OSError, ValueError, KeyError, GraphStoreError, sqlite3.Error):
        return None


class RepoMemo:
    """A bounded, thread-safe cache of one value per (namespace, repo path)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[tuple[str, str], tuple[Stamp, Any]] = OrderedDict()
        self._flight = SingleFlight()

    def get(
        self,
        namespace: str,
        entry: Mapping[str, Any],
        compute: Callable[[], T],
        cacheable: Callable[[T], bool] | None = None,
    ) -> T:
        """The value for *entry*, from the cache while its graph file is unchanged.

        *compute* runs outside the lock, and threads that miss the same value at the same graph
        stamp share one computation (``SingleFlight``): a reindex changes the stamp, so it is
        never shared across graph versions. A value *cacheable* rejects (a read that failed) is
        returned but not kept, and nothing is kept for a repo whose graph file cannot be
        stamped.
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

        def build() -> T:
            value = compute()
            if cacheable is not None and not cacheable(value):
                return value
            with self._lock:
                self._data[key] = (stamp, value)
                self._data.move_to_end(key)
                while len(self._data) > limit:
                    self._data.popitem(last=False)
            return value

        return self._flight.do((key, stamp), build)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


#: The cache the cross-repo joins share.
MEMO = RepoMemo()
#: Whole ``infrastructure`` blocks, keyed by the repos' graph stamps.
INFRASTRUCTURE_RESULTS = ResultCache()
#: The all-repo server index of the Feign/JS join, keyed by every repo's graph stamp. Two entries:
#: one registry state is current, the other covers a call that began before an indexer published.
SERVER_INDEXES = ResultCache(2)
#: The rows of ``origin="consumers"`` before paging, keyed by the query and every graph stamp.
CONSUMER_RESULTS = ResultCache()
#: The merged rows of ``cross_repo_search_tool`` before paging, keyed by the query and the
#: stamps of the searched repos.
SEARCH_RESULTS = ResultCache()


POOL_THREADS_CAP = 4
_POOL_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None


def shared_pool_size() -> int:
    """Threads in the shared pool: ``CRG_CROSS_REPO_WORKERS``, else the CPU count capped at 4.

    Read when the pool is first used, so it holds for the life of the process.
    """
    default = min(os.cpu_count() or 1, POOL_THREADS_CAP)
    return max(1, min(env_int(CROSS_REPO_WORKERS_ENV, default), CROSS_REPO_WORKERS_MAX))


def _shared_pool() -> ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(
                max_workers=shared_pool_size(), thread_name_prefix="crg-xrepo",
            )
        return _POOL


def submit_shared(fn: Callable[..., R], *args: Any) -> Any:
    """Run ``fn(*args)`` on the shared pool and return its ``Future``."""
    return _shared_pool().submit(fn, *args)


def _reset_shared_pool() -> None:
    """Drop the shared pool so the next use makes a new one (for tests)."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=True)


def _run(fn: Callable[[T], R], todo: list[T], workers: int) -> list[R]:
    """``[fn(item) for item in todo]`` on the calling thread plus helpers from the shared pool.

    Calls share one bounded pool, so N concurrent calls use at most the pool's threads (plus
    their own callers) rather than N pools. The caller works through the items itself and
    helpers only take items from the same queue, so a call never waits for a helper that has
    not started (it cancels it): no deadlock when the pool is busy or ``fn`` calls this again.
    """
    count = len(todo)
    if workers <= 1 or count <= 1:
        return [fn(item) for item in todo]
    results: list[Any] = [None] * count
    errors: dict[int, BaseException] = {}
    lock = threading.Lock()
    cursor = iter(range(count))

    def drain() -> None:
        while True:
            with lock:
                if errors:
                    return
                index = next(cursor, None)
            if index is None:
                return
            try:
                results[index] = fn(todo[index])
            except BaseException as exc:
                with lock:
                    errors[index] = exc
                return

    pool = _shared_pool()
    helpers = [pool.submit(drain) for _ in range(min(workers, count) - 1)]
    drain()
    for helper in helpers:
        if not helper.cancel():
            helper.result()
    if errors:
        raise errors[min(errors)]
    return results  # type: ignore[no-any-return]


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """``[fn(item) for item in items]`` on up to *workers* threads, in the order of *items*.

    ``fn`` must not raise: the first exception ends the call. One worker runs on the calling
    thread, as before; the others come from one pool shared by every call (see ``_run``).
    """
    return _run(fn, list(items), workers)


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
    for start in range(0, len(todo), size):
        batch = todo[start:start + size]
        if out_of_budget():
            results.extend(SKIPPED for _ in batch)
        else:
            results.extend(_run(fn, batch, size))
    return results
