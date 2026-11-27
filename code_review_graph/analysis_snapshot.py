"""Whole-graph analyses on an in-memory copy of the graph, and a cache of their results.

``get_knowledge_gaps_tool`` reads every edge, every node and, for each hotspot candidate, a
test lookup. Run against the graph file on the ECS task that is hundreds of thousands of
random page reads, measured at 0.7 to 1 ms each (a 254 MB graph: 4.4 s to count its edges
from the file, 3 ms from memory, 2.3 s to copy into memory). The CPU and the file's
sequential throughput are not the limit, so the cheapest fix that cannot change an answer is
to read the file once, in order, into an in-memory SQLite database and run the same analysis
on that.

Two parts, both query side only (nothing here is imported by a graph build, so the index
fingerprint does not move):

* :func:`in_memory_snapshot` swaps the store's connection for a memory copy for the length of
  a ``with`` block. It skips graphs over ``CRG_SNAPSHOT_MAX_MB`` (default 1024), and holds at
  most ``CRG_SNAPSHOT_SLOTS`` copies at once (default 2) so concurrent calls cannot add up to
  more memory than the task has; a call that cannot get a slot runs on the file as before.
* :class:`ResultCache` keeps the finished, untruncated result of an analysis per graph state,
  so each cursor page of the same query reads it instead of computing it again.
"""

from __future__ import annotations

import copy
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .constants import env_int

logger = logging.getLogger(__name__)

__all__ = ["RESULTS", "ResultCache", "file_state", "in_memory_snapshot"]

_MAX_MB = env_int("CRG_SNAPSHOT_MAX_MB", 1024)
_SLOTS = max(1, env_int("CRG_SNAPSHOT_SLOTS", 2))
_SLOT_WAIT_SECONDS = 0.0
_slots = threading.BoundedSemaphore(_SLOTS)


def file_state(db_path: Path) -> tuple[int, int, int, int] | None:
    """Cheap fingerprint of a graph file: size and mtime of the database and its WAL.

    A write to a WAL-mode database lands in the ``-wal`` file first, so the main file's
    mtime alone can stay put after an edit. ``None`` when the file cannot be read, which a
    caller treats as "do not cache".
    """
    try:
        main = os.stat(db_path)
    except OSError:
        return None
    try:
        wal = os.stat(db_path.with_name(db_path.name + "-wal"))
        wal_state = (wal.st_mtime_ns, wal.st_size)
    except OSError:
        wal_state = (0, 0)
    return (main.st_mtime_ns, main.st_size, *wal_state)


@contextmanager
def in_memory_snapshot(store: Any) -> Iterator[bool]:
    """Point *store* at an in-memory copy of its graph inside the block; yield whether it did.

    The store keeps its own connection to the file and gets it back on exit, so closing the
    store afterwards behaves as always. Anything that goes wrong while copying is logged and
    leaves the store on the file.
    """
    db_path = Path(store.db_path)
    try:
        size_mb = db_path.stat().st_size / 1048576
    except OSError:
        yield False
        return
    if _MAX_MB <= 0 or size_mb > _MAX_MB:
        yield False
        return
    if not _slots.acquire(timeout=_SLOT_WAIT_SECONDS):
        yield False
        return
    file_conn = store._conn
    mem: sqlite3.Connection | None = None
    swapped = False
    try:
        started = time.monotonic()
        try:
            mem = sqlite3.connect(
                ":memory:", check_same_thread=False, isolation_level=None,
            )
            file_conn.backup(mem)
            mem.row_factory = sqlite3.Row
            store._conn = mem
            swapped = True
            logger.info(
                "analysis snapshot: copied %.0f MB in %.2f s", size_mb,
                time.monotonic() - started,
            )
        except sqlite3.Error as exc:
            logger.warning("analysis snapshot failed, using the file: %s", exc)
        yield swapped
    finally:
        if swapped:
            store._conn = file_conn
        if mem is not None:
            try:
                mem.close()
            except sqlite3.Error:  # pragma: no cover - dead handle
                logger.debug("Could not close the snapshot connection")
        _slots.release()


class ResultCache:
    """A small LRU of finished results, keyed by what was asked and the graph file's state.

    ``get_or_compute`` runs *compute* once per key. Two callers asking for the same key at
    the same time share one computation: the second waits and reads the first's result. A
    result is deep-copied on the way in and out, so a caller that edits what it got cannot
    change what the next caller reads.
    """

    def __init__(self, max_entries: int = 8) -> None:
        self._max = max(1, max_entries)
        self._lock = threading.Lock()
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._building: dict[Any, threading.Event] = {}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> tuple[Any, bool]:
        """``(result, was_cached)``. A key containing ``None`` is never cached."""
        if key is None or (isinstance(key, tuple) and None in key):
            return compute(), False
        while True:
            with self._lock:
                if key in self._data:
                    self._data.move_to_end(key)
                    return copy.deepcopy(self._data[key]), True
                waiting = self._building.get(key)
                if waiting is None:
                    self._building[key] = threading.Event()
                    break
            waiting.wait()
        try:
            value = compute()
            with self._lock:
                self._data[key] = copy.deepcopy(value)
                self._data.move_to_end(key)
                while len(self._data) > self._max:
                    self._data.popitem(last=False)
            return value, False
        finally:
            with self._lock:
                self._building.pop(key).set()


RESULTS = ResultCache(max_entries=env_int("CRG_ANALYSIS_CACHE_ENTRIES", 8))
