"""The betweenness scores behind the bridge tool are computed once per graph state.

A tool call opens a fresh store, so the scores live in the ``metadata`` table, keyed by
what the graph holds. A hit skips the computation; any change to the nodes or edges
makes the entry stale.
"""

from __future__ import annotations

import json

import pytest

from code_review_graph import analysis
from code_review_graph.analysis import (
    find_bridge_nodes,
    find_hub_nodes,
    find_knowledge_gaps,
    find_surprising_connections,
    generate_suggested_questions,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo


@pytest.fixture
def store(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        yield s


def _chain(store, names):
    """a -> b -> c ...: every middle node sits on the paths between the others."""
    qns = []
    for name in names:
        store.upsert_node(NodeInfo("Function", name, "/r/m.py", 1, 5, "python"))
        qns.append(f"/r/m.py::{name}")
    for src, dst in zip(qns, qns[1:]):
        store.upsert_edge(EdgeInfo("CALLS", src, dst, "/r/m.py", 3))
    store.commit()
    return qns


@pytest.fixture
def counter(monkeypatch):
    calls = {"n": 0}
    real = analysis._compute_betweenness

    def counting(store):
        calls["n"] += 1
        return real(store)

    monkeypatch.setattr(analysis, "_compute_betweenness", counting)
    return calls


def test_second_call_reads_the_cache(store, counter):
    _chain(store, ["a", "b", "c", "d"])
    first = find_bridge_nodes(store, top_n=10)
    second = find_bridge_nodes(store, top_n=10)
    assert counter["n"] == 1
    assert first == second
    assert {r["name"] for r in first} == {"b", "c"}
    assert json.loads(store.get_metadata("bridge_scores"))["scores"]


def test_filters_do_not_split_the_cache(store, counter):
    _chain(store, ["a", "b", "c", "d"])
    find_bridge_nodes(store, include_tests=True)
    find_bridge_nodes(store, include_tests=False, include_config=False)
    generate_suggested_questions(store)
    assert counter["n"] == 1


def test_new_edge_invalidates(store, counter):
    qns = _chain(store, ["a", "b", "c", "d"])
    before = find_bridge_nodes(store)
    store.upsert_edge(EdgeInfo("CALLS", qns[0], qns[3], "/r/m.py", 9))
    store.commit()
    after = find_bridge_nodes(store)
    assert counter["n"] == 2
    assert before != after


def test_new_node_and_removed_file_invalidate(store, counter):
    _chain(store, ["a", "b", "c", "d"])
    find_bridge_nodes(store)
    store.upsert_node(NodeInfo("Function", "e", "/r/other.py", 1, 5, "python"))
    store.commit()
    find_bridge_nodes(store)
    assert counter["n"] == 2
    store.remove_file_data("/r/m.py")
    store.commit()
    assert find_bridge_nodes(store) == []
    assert counter["n"] == 3


def test_cache_survives_a_new_store(store, tmp_path, counter):
    _chain(store, ["a", "b", "c", "d"])
    first = find_bridge_nodes(store)
    with GraphStore(get_db_path(tmp_path)) as other:
        assert find_bridge_nodes(other) == first
    assert counter["n"] == 1


def test_corrupt_cache_is_recomputed(store, counter):
    _chain(store, ["a", "b", "c", "d"])
    expected = find_bridge_nodes(store)
    store.set_metadata("bridge_scores", "{not json")
    assert find_bridge_nodes(store) == expected
    assert counter["n"] == 2


def test_engine_switch_does_not_reuse_scores(store, counter, monkeypatch):
    _chain(store, ["a", "b", "c", "d"])
    find_bridge_nodes(store)
    monkeypatch.setenv("CRG_BETWEENNESS_ENGINE", "networkx")
    find_bridge_nodes(store)
    assert counter["n"] == 2


def test_sampled_igraph_scores_match_networkx(store):
    """Above the exact limit the pivots are the ones networkx draws; scores agree to 1e-9."""
    pytest.importorskip("igraph")
    import networkx as nx

    n = 60
    graph = nx.gnm_random_graph(n, 200, seed=3, directed=True)
    named = nx.relabel_nodes(graph, {i: f"n{i}" for i in range(n)})
    k = 20
    ours = analysis._sampled_betweenness_igraph(named, k)
    assert ours is not None
    ref = nx.betweenness_centrality(named, k=k, normalized=True, seed=0)
    assert max(abs(ours[v] - ref[v]) for v in named) < 1e-9


def test_shared_loads_give_the_same_answers(store):
    qns = _chain(store, ["a", "b", "c", "d", "e", "f", "g"])
    store.upsert_edge(EdgeInfo("CALLS", qns[0], qns[6], "/r/m.py", 9))
    store.commit()
    nodes = store.get_all_nodes(exclude_files=True)
    edges = store.get_all_edges()
    assert find_hub_nodes(store) == find_hub_nodes(store, nodes=nodes, edges=edges)
    assert find_knowledge_gaps(store) == find_knowledge_gaps(store, nodes=nodes, edges=edges)
    assert find_surprising_connections(store) == find_surprising_connections(
        store, nodes=nodes, edges=edges,
    )
