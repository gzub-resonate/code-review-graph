"""``find_knowledge_gaps`` looks hotspot coverage up a chunk at a time (loop 1, perf).

The batched lookup must give the same answer as asking ``get_transitive_tests`` per node.
"""

import pytest

from code_review_graph import analysis
from code_review_graph.analysis import _covered_names, find_knowledge_gaps
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


@pytest.fixture
def graph(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        store.set_metadata("repo_root", "/repo")
        yield store


def node(store, name, file="/repo/a.py", kind="Function", **kwargs):
    store.upsert_node(NodeInfo(kind, name, file, 1, 10, "python", **kwargs))
    return f"{file}::{name}"


def edge(store, kind, source, target):
    store.upsert_edge(EdgeInfo(kind, source, target, source.split("::")[0], 2))


def build(store):
    direct = node(store, "direct")
    helped = node(store, "helped")
    bare = node(store, "untested")
    box = node(store, "Box", kind="Class")
    member = node(store, "Box.open", kind="Function")
    edge(store, "CONTAINS", box, member)
    t_direct = node(store, "test_direct", "/repo/test_a.py", kind="Test", is_test=True)
    edge(store, "TESTED_BY", direct, t_direct)
    helper = node(store, "fixture", "/repo/test_a.py", is_test=True)
    t_help = node(store, "test_help", "/repo/test_a.py", kind="Test", is_test=True)
    edge(store, "CALLS", helper, helped)
    edge(store, "CALLS", t_help, helper)
    t_box = node(store, "test_box", "/repo/test_a.py", kind="Test", is_test=True)
    edge(store, "CALLS", t_box, member)
    edge(store, "TESTED_BY", member, t_box)
    # Give every production node enough edges to be a hotspot.
    others = [node(store, f"user{i}", "/repo/u.py") for i in range(6)]
    for target in (direct, helped, bare, box, member):
        for other in others:
            edge(store, "CALLS", other, target)
    store.commit()
    return [direct, helped, bare, box, member]


def test_batch_gives_the_answer_of_asking_each_node(graph):
    names = build(graph)
    each = {n for n in names if graph.get_transitive_tests(n, include_callee_routes=False)}
    assert each == {names[0], names[1], names[3], names[4]}
    assert _covered_names(graph, names) == each
    assert _covered_names(graph, []) == set()


@pytest.mark.parametrize("chunk", [1, 2, 256])
@pytest.mark.parametrize("cap", [None, 1, 5])
def test_gaps_do_not_depend_on_the_chunk_size(graph, monkeypatch, chunk, cap):
    build(graph)
    monkeypatch.setattr(analysis, "_COVERAGE_CHUNK", 1)
    base = find_knowledge_gaps(graph, max_untested=cap, include_tests=False)
    monkeypatch.setattr(analysis, "_COVERAGE_CHUNK", chunk)
    other = find_knowledge_gaps(graph, max_untested=cap, include_tests=False)
    assert other["untested_hotspots"] == base["untested_hotspots"]
    assert "untested" in {h["name"] for h in find_knowledge_gaps(
        graph, max_untested=None, include_tests=False)["untested_hotspots"]}
