"""Batched twin lookup for ``get_affected_flows`` matches the per-flow lookup.

``get_affected_flows`` used to re-read and decode every flow of the same size once per affected
flow. It now shares one size cache; the answer must stay the same.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.flow_twins import shared_twin_reads
from code_review_graph.flows import get_flow_by_id
from code_review_graph.graph import GraphStore

# (id, name, path): 1, 2 and 3 share a tree below the entry; 4 has the same size but another
# tree; 5 has the same tree as 1 but a different size, so it is not a twin.
FLOWS = [
    (1, "a", [10, 20, 30]),
    (2, "b", [11, 20, 30]),
    (3, "c", [12, 30, 20]),
    (4, "d", [13, 20, 31]),
    (5, "e", [14, 20, 30, 40]),
]


def _store(tmp_path: Path) -> GraphStore:
    store = GraphStore(str(tmp_path / "graph.db"))
    for fid, name, path in FLOWS:
        store._conn.execute(
            "INSERT INTO flows (id, name, entry_point_id, depth, node_count, file_count, "
            "criticality, path_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, 1, 0.5, ?, 't', 't')",
            (fid, name, path[0], len(path), json.dumps(path)),
        )
    store._conn.commit()
    return store


def test_shared_cache_gives_the_same_twins_as_a_fresh_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        fresh = {fid: get_flow_by_id(store, fid) for fid, _n, _p in FLOWS}
        with shared_twin_reads(store):
            shared = {fid: get_flow_by_id(store, fid) for fid, _n, _p in FLOWS}
        assert shared == fresh
    finally:
        store.close()


def test_twins_exclude_self_and_other_sizes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        flow = get_flow_by_id(store, 1)
        assert flow is not None
        assert [t["flow_id"] for t in flow["same_call_tree_as"]] == [2, 3]
        flow4 = get_flow_by_id(store, 4)
        assert flow4 is not None
        assert "same_call_tree_as" not in flow4
        flow5 = get_flow_by_id(store, 5)
        assert flow5 is not None
        assert "same_call_tree_as" not in flow5
    finally:
        store.close()


def test_shared_reads_match_unshared_through_get_affected_flows(tmp_path: Path) -> None:
    from code_review_graph.flows import get_affected_flows

    store = _store(tmp_path)
    try:
        store._conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, "
            "language, is_test, extra, updated_at, file_hash) "
            "VALUES ('Function', 'f', 'f.py::f', 'f.py', 1, 2, 'python', 0, '{}', 0, 'h')"
        )
        node_id = store._conn.execute("SELECT id FROM nodes WHERE name = 'f'").fetchone()[0]
        for fid, _name, path in FLOWS:
            store._conn.execute(
                "UPDATE flows SET path_json = ? WHERE id = ?",
                (json.dumps([*path, node_id] if fid != 5 else path), fid),
            )
        store._conn.commit()
        plain = get_affected_flows(store, ["f.py"])
        with shared_twin_reads(store):
            shared = get_affected_flows(store, ["f.py"])
        assert shared == plain
    finally:
        store.close()
