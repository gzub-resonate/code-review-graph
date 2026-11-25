"""``get_hub_nodes`` ranks by resolved edges: a generic name that only looks hot because many
classes share it drops, and each hub says how much of its degree is traced."""

from __future__ import annotations

import pytest

from code_review_graph.analysis import find_hub_nodes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import get_hub_nodes_func


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata("repo_root", str(tmp_path))
        store.root = tmp_path
        yield store


def _node(store, name, file, parent=None):
    store.upsert_node(NodeInfo("Function", name, file, 1, 5, "java", parent_name=parent))
    return f"{file}::{parent + '.' if parent else ''}{name}"


def _edge(store, source, target, **extra):
    store.upsert_edge(EdgeInfo("CALLS", source, target, source.split("::")[0], 3, extra=extra))


def _synthetic(store):
    """``insert`` is defined on three DAOs and 30 callers reach one by name alone; one of them
    also makes 10 calls to bare names. ``placeOrder`` has 6 callers bound through a typed
    receiver, so fewer edges but all of them resolved."""
    inserts = [
        _node(store, "insert", f"/r/Dao{i}.java", parent=f"Dao{i}") for i in range(3)
    ]
    real = _node(store, "placeOrder", "/r/OrderService.java", parent="OrderService")
    chatty = None
    for i in range(30):
        caller = _node(store, f"c{i}", f"/r/C{i}.java")
        _edge(store, caller, inserts[0], bare_call_target="insert")
        chatty = chatty or caller
    for i in range(6):
        caller = _node(store, f"o{i}", f"/r/O{i}.java")
        _edge(store, caller, real, bare_call_target="placeOrder", receiver_class="OrderService")
    for i in range(10):
        _edge(store, chatty, f"bare{i}")
    store.commit()
    return inserts[0], real, chatty


def test_common_name_hub_drops_when_ranked_by_resolved_edges(repo):
    insert, real, _chatty = _synthetic(repo)

    hubs = find_hub_nodes(repo, top_n=10)

    assert [h["qualified_name"] for h in hubs][0] == real
    by_qn = {h["qualified_name"]: h for h in hubs}
    assert by_qn[real]["resolved_degree"] == 6
    assert by_qn[real]["unresolved_degree"] == 0
    assert by_qn[real]["resolved_share"] == 1.0
    assert by_qn[insert]["total_degree"] == 30
    assert by_qn[insert]["resolved_degree"] == 0
    assert by_qn[insert]["unresolved_degree"] == 30
    assert by_qn[insert]["resolved_share"] == 0.0


def test_rank_by_total_keeps_the_older_order(repo):
    insert, real, _chatty = _synthetic(repo)

    hubs = find_hub_nodes(repo, top_n=10, rank_by="total")

    # 30 name-matched calls to a name 3 classes share weigh 30 * 0.25 = 7.5 > 6 typed calls.
    assert hubs[0]["qualified_name"] == insert
    assert hubs[0]["ranking_degree"] == 7.5
    assert real in [h["qualified_name"] for h in hubs]


def test_bare_name_edges_count_as_unresolved(repo):
    _insert, _real, chatty = _synthetic(repo)

    (row,) = [h for h in find_hub_nodes(repo, top_n=50) if h["qualified_name"] == chatty]

    # One call bound by name to a shared name, plus ten bare-name calls: nothing traced.
    assert row["total_degree"] == 1
    assert row["resolved_degree"] == 0
    assert row["unresolved_degree"] == 11
    assert row["resolved_share"] == 0.0


def test_tool_reports_share_and_rank_by(repo):
    _insert, real, _chatty = _synthetic(repo)

    standard = get_hub_nodes_func(repo_root=str(repo.root), top_n=3)
    assert standard["hub_nodes"][0]["qualified_name"] == real
    assert standard["hub_nodes"][0]["resolved_share"] == 1.0
    assert "resolved_degree" in standard["summary"]

    minimal = get_hub_nodes_func(repo_root=str(repo.root), top_n=3, detail_level="minimal")
    assert {"resolved_degree", "resolved_share"} <= set(minimal["hub_nodes"][0])

    total = get_hub_nodes_func(repo_root=str(repo.root), top_n=3, rank_by="total")
    assert total["hub_nodes"][0]["name"] == "insert"
    assert "ranking_degree" in total["summary"]

    with pytest.raises(ValueError):
        get_hub_nodes_func(repo_root=str(repo.root), rank_by="loudest")
