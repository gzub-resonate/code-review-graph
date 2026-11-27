"""Flow ids are deterministic across full rebuilds of the same graph."""

import tempfile
from pathlib import Path

from code_review_graph.flows import get_flows, store_flows
from code_review_graph.graph import GraphStore, NodeInfo


def _store() -> GraphStore:
    root = Path(tempfile.mkdtemp()).resolve()
    return GraphStore(str(root / "graph.db"))


def _entry(store: GraphStore, name: str) -> int:
    path = f"/r/{name}.py"
    store.upsert_node(NodeInfo(
        kind="Function", name=name, file_path=path,
        line_start=1, line_end=2, language="python",
    ))
    store.commit()
    return store.get_node(f"{path}::{name}").id


def _flow(entry_id: int, name: str, crit: float) -> dict:
    return {
        "name": name, "entry_point_id": entry_id, "depth": 1, "node_count": 1,
        "file_count": 1, "criticality": crit, "path": [entry_id],
    }


def test_ids_do_not_depend_on_detection_order():
    store = _store()
    ids = {n: _entry(store, n) for n in ("a", "b", "c")}
    flows = [_flow(ids["c"], "c", 0.5), _flow(ids["a"], "a", 0.5), _flow(ids["b"], "b", 0.9)]

    store_flows(store, flows)
    first = {f["name"]: f["id"] for f in get_flows(store)}
    store_flows(store, list(reversed(flows)))
    second = {f["name"]: f["id"] for f in get_flows(store)}

    assert first == second


def test_order_is_criticality_then_key():
    store = _store()
    ids = {n: _entry(store, n) for n in ("a", "b", "c")}
    store_flows(store, [
        _flow(ids["c"], "c", 0.5), _flow(ids["a"], "a", 0.5), _flow(ids["b"], "b", 0.9),
    ])
    by_id = {f["id"]: f["name"] for f in get_flows(store)}
    assert [by_id[i] for i in sorted(by_id)] == ["b", "a", "c"]


def test_rebuild_restarts_the_counter():
    store = _store()
    eid = _entry(store, "a")
    store_flows(store, [_flow(eid, "a", 0.5)])
    store_flows(store, [_flow(eid, "a", 0.5)])
    assert [f["id"] for f in get_flows(store)] == [1]
