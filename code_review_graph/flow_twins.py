"""Read the flows of each size once per tool call, not once per affected flow.

``get_flow_by_id`` names the flows that share a call tree with the one it returns, and for that
it read and decoded every flow of the same node count. ``get_affected_flows`` calls it for each
flow a change touches, so a change that touches N flows decoded the flows table about N times
(a 200-file change on a 5,500-file synthetic repo spent most of its time there).

:func:`shared_twin_reads` gives the calls inside it one cache. It lives here, not in ``flows``,
because ``flows`` is part of what a graph build runs and editing it would change the index
fingerprint and force a reindex for a query-side speed-up. The cache lasts for one ``with``
block in one context, so a later call reads the flows table again and sees a rebuilt graph.
A test compares the answers with the uncached lookup.

Nothing here changes which flows are twins. It is query-time only and not imported by the build.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from . import flows
from .graph import _sanitize_name

_Sizes = dict[int, list[tuple[int, str, frozenset[int]]]]

_SIZES: ContextVar[tuple[Any, _Sizes] | None] = ContextVar("crg_flow_twin_sizes", default=None)
_original = flows._structural_twins


def _flows_of_size(
    store: Any, node_count: int, sizes: _Sizes,
) -> list[tuple[int, str, frozenset[int]]]:
    rows = sizes.get(node_count)
    if rows is None:
        rows = []
        for row in store._conn.execute(
            "SELECT id, name, path_json FROM flows WHERE node_count = ? ORDER BY id",
            (node_count,),
        ).fetchall():
            try:
                path = json.loads(row["path_json"])
            except (TypeError, ValueError):
                continue
            rows.append((row["id"], row["name"], frozenset(path[1:])))
        sizes[node_count] = rows
    return rows


def _structural_twins(store: Any, flow: dict) -> list[dict]:
    active = _SIZES.get()
    if active is None or active[0] is not store:
        return _original(store, flow)
    key = flows._tree_key(flow)
    if not key:
        return []
    return [
        {"flow_id": fid, "name": _sanitize_name(name)}
        for fid, name, tree in _flows_of_size(store, flow["node_count"], active[1])
        if fid != flow["id"] and tree == key
    ]


flows._structural_twins = _structural_twins


@contextlib.contextmanager
def shared_twin_reads(store: Any) -> Iterator[None]:
    """Share one read of the flows table per node count among the lookups inside the block."""
    token = _SIZES.set((store, {}))
    try:
        yield
    finally:
        _SIZES.reset(token)


def with_shared_twin_reads(store: Any, fn: Any) -> Any:
    """*fn* wrapped so each call runs inside :func:`shared_twin_reads` for *store*."""

    def run(*args: Any, **kwargs: Any) -> Any:
        with shared_twin_reads(store):
            return fn(*args, **kwargs)

    return run
