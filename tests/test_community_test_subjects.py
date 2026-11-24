"""Test classes follow the class they test into its community.

Scala test classes (``FooSpec``, ``FooSuite``) have no TESTED_BY edge, so they
formed two-member communities split from the source they test. Tests with no
subject at all are grouped per module instead of one cluster each.
"""

from __future__ import annotations

import pytest

from code_review_graph import communities as cm
from code_review_graph.graph import GraphEdge, GraphNode

needs_igraph = pytest.mark.skipif(not cm.IGRAPH_AVAILABLE, reason="igraph not installed")


def _node(qn: str, kind: str = "Function") -> GraphNode:
    path, _, name = qn.partition("::")
    return GraphNode(
        id=0, kind=kind, name=name, qualified_name=qn, file_path=path,
        line_start=1, line_end=2, language="scala", parent_name=None, params=None,
        return_type=None, is_test=False, file_hash="x", extra={},
    )


def _edge(kind: str, source: str, target: str) -> GraphEdge:
    return GraphEdge(
        id=0, kind=kind, source_qualified=source, target_qualified=target,
        file_path="x", line=1, extra={},
    )


def _community(name: str, members: list[str]) -> dict:
    return {
        "name": name, "level": 0, "size": len(members), "cohesion": 0.5,
        "dominant_language": "scala", "description": "", "members": members,
    }


BASE = "mod/src/main/scala/app"
TEST = "mod/src/test/scala/app"


def _scala_graph() -> tuple[list[GraphNode], list[GraphEdge]]:
    """Two source classes, their test classes, and two classes that test nothing.

    ``FooSpec`` and ``BarSuite`` have no TESTED_BY edge: the link is the name.
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    def add_class(path: str, cls: str, methods: tuple[str, ...], kind: str = "Function") -> None:
        nodes.append(_node(f"{path}/{cls}.scala::{cls}", "Class"))
        for m in methods:
            qn = f"{path}/{cls}.scala::{cls}.{m}"
            nodes.append(_node(qn, kind))
            edges.append(_edge("CONTAINS", f"{path}/{cls}.scala::{cls}", qn))

    add_class(BASE, "Foo", ("a", "b", "c"))
    add_class(BASE, "Bar", ("a", "b", "c"))
    add_class(TEST, "FooSpec", ("t1", "t2", "t3"), kind="Test")
    add_class(TEST, "BarSuite", ("t1", "t2", "t3"), kind="Test")
    add_class(TEST, "Helpers", ("h1",), kind="Test")
    add_class(TEST, "Fixtures", ("f1",), kind="Test")
    for cls in ("Foo", "Bar"):
        edges.append(_edge("CALLS", f"{BASE}/{cls}.scala::{cls}.a", f"{BASE}/{cls}.scala::{cls}.b"))
        edges.append(_edge("CALLS", f"{BASE}/{cls}.scala::{cls}.b", f"{BASE}/{cls}.scala::{cls}.c"))
    return nodes, edges


def test_name_links_cover_the_common_test_suffixes():
    nodes = [
        _node("src/main/Foo.scala::Foo", "Class"),
        _node("src/test/FooTest.scala::FooTest", "Class"),
        _node("src/test/FooSpec.scala::FooSpec", "Class"),
        _node("src/test/FooSuite.scala::FooSuite", "Class"),
        _node("src/test/FooIT.scala::FooIT", "Class"),
        _node("src/test/TestFoo.scala::TestFoo", "Class"),
        _node("src/test/NoSubjectTest.scala::NoSubjectTest", "Class"),
    ]

    linked = cm._name_based_test_subjects(dict(enumerate(nodes)))

    assert linked == {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}


def test_the_candidate_in_the_mirrored_directory_wins():
    nodes = [
        _node("a/src/main/Foo.scala::Foo", "Class"),
        _node("b/src/main/Foo.scala::Foo", "Class"),
        _node("b/src/test/FooTest.scala::FooTest", "Class"),
    ]

    assert cm._name_based_test_subjects(dict(enumerate(nodes))) == {2: 1}


@needs_igraph
def test_test_classes_join_the_community_of_their_subject():
    nodes, edges = _scala_graph()

    result = cm._detect_leiden(nodes, edges, min_size=1)

    owner = {qn: i for i, c in enumerate(result) for qn in c["members"]}
    foo = owner[f"{BASE}/Foo.scala::Foo"]
    bar = owner[f"{BASE}/Bar.scala::Bar"]
    assert foo != bar
    assert owner[f"{TEST}/FooSpec.scala::FooSpec"] == foo
    assert owner[f"{TEST}/FooSpec.scala::FooSpec.t2"] == foo
    assert owner[f"{TEST}/BarSuite.scala::BarSuite"] == bar
    assert owner[f"{TEST}/BarSuite.scala::BarSuite.t3"] == bar


def test_subjectless_small_test_clusters_share_one_tests_community():
    nodes, edges = _scala_graph()
    # A second module, so "mod" is the module root rather than the shared prefix.
    nodes.append(_node("other/src/main/scala/X.scala::X", "Class"))
    helpers = [n.qualified_name for n in nodes if "/Helpers." in n.qualified_name]
    fixtures = [n.qualified_name for n in nodes if "/Fixtures." in n.qualified_name]
    communities = [
        _community("helpers", helpers),
        _community("fixtures", fixtures),
        _community("prod", [n.qualified_name for n in nodes if "/main/" in n.qualified_name]),
    ]

    grouped = cm._group_orphan_tests(communities, nodes, edges)
    cm._finalize_communities(grouped, nodes, edges)

    assert sorted(c["name"] for c in grouped) == ["mod-tests", "prod"]
    tests = next(c for c in grouped if c["name"] == "mod-tests")
    assert set(tests["members"]) == set(helpers + fixtures)
    assert tests["size"] == len(helpers + fixtures)
    assert "merged" not in tests and "keep_name" not in tests


def test_a_lone_subjectless_cluster_is_left_alone():
    nodes, edges = _scala_graph()
    helpers = [n.qualified_name for n in nodes if "/Helpers." in n.qualified_name]
    spec = [n.qualified_name for n in nodes if "/FooSpec." in n.qualified_name]
    communities = [_community("helpers", helpers), _community("spec", spec)]

    # FooSpec is as small as Helpers, so these two do group; one alone would not.
    assert len(cm._group_orphan_tests(communities[:1], nodes, edges)) == 1


def test_big_test_clusters_are_left_alone(monkeypatch):
    nodes, edges = _scala_graph()
    helpers = [n.qualified_name for n in nodes if "/Helpers." in n.qualified_name]
    fixtures = [n.qualified_name for n in nodes if "/Fixtures." in n.qualified_name]
    monkeypatch.setattr(cm, "_SMALL_TEST_CLUSTER", 2)

    communities = [_community("a", helpers), _community("b", fixtures)]

    assert len(cm._group_orphan_tests(communities, nodes, edges)) == 2
