"""Row counts of a graph file, kept per file state, and the cursor stamp built from them.

``paging.graph_stamp`` counts every node and every edge on each call, and about twenty tool
call sites use it. On a large graph that is a full scan from the file: random page reads at
0.7 to 1 ms each, measured at 4.4 s for the edges of a 254 MB graph and about 3 ms from
memory. The answer cannot change while the file does not, so this module remembers it per
:func:`~code_review_graph.analysis_snapshot.file_state` and hands the same numbers back to
the next call on an unchanged graph.

Query side only: nothing a graph build imports reaches this module, so the index fingerprint
does not move. :func:`graph_stamp` is a drop-in for :func:`code_review_graph.paging.graph_stamp`
and returns the same digest for the same graph, so a cursor made by either is accepted by
the other.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any

from .analysis_snapshot import file_state
from .paging import _digest

__all__ = ["count_files", "count_nodes", "count_edges", "graph_stamp"]

_SQL = {
    "nodes": "SELECT COUNT(*) FROM nodes",
    "edges": "SELECT COUNT(*) FROM edges",
    "files": "SELECT COUNT(*) FROM nodes WHERE kind = 'File'",
}
#: One entry per (graph file, file state, count). A server holds ~120 graphs, each of which
#: changes state now and then, so the bound is generous and the entries are three integers.
_MAX_ENTRIES = 1024
_lock = threading.Lock()
_counts: OrderedDict[tuple[str, tuple[int, ...], str], int] = OrderedDict()


def _count(store: Any, name: str) -> int:
    """Rows counted by ``_SQL[name]`` on *store*, from the cache while the file is unchanged.

    Nothing is cached, and nothing is read from the cache, when the file cannot be stat'ed
    (an in-memory database) or the connection holds an open transaction (its own uncommitted
    rows are not in the file's state). The state is read before and after counting, and the
    count is kept only when both agree, so a write that lands in between is never filed
    under the state it did not belong to.
    """
    conn = store._conn
    state = None if conn.in_transaction else file_state(store.db_path)
    if state is None:
        return int(conn.execute(_SQL[name]).fetchone()[0])
    key = (str(store.db_path), state, name)
    with _lock:
        if key in _counts:
            _counts.move_to_end(key)
            return _counts[key]
    value = int(conn.execute(_SQL[name]).fetchone()[0])
    # Counted from a snapshot of the file or from the file itself: the same rows either way.
    if file_state(store.db_path) == state:
        with _lock:
            _counts[key] = value
            _counts.move_to_end(key)
            while len(_counts) > _MAX_ENTRIES:
                _counts.popitem(last=False)
    return value


def count_nodes(store: Any) -> int:
    return _count(store, "nodes")


def count_edges(store: Any) -> int:
    return _count(store, "edges")


def count_files(store: Any) -> int:
    """Number of ``File`` nodes."""
    return _count(store, "files")


def graph_stamp(store: Any, *extra: Any) -> str:
    """Same digest as :func:`code_review_graph.paging.graph_stamp`, without the rescans.

    The last build time is read every call (one row), so a rebuild that rewrites the file is
    seen even where its counts happen to match; the two counts come from the per-state cache.
    """
    updated = store.get_metadata("last_updated")
    return _digest(json.dumps(
        [updated, count_nodes(store), count_edges(store), *extra],
        default=str, separators=(",", ":"),
    ))


def clear() -> None:
    """Forget every cached count (tests)."""
    with _lock:
        _counts.clear()
