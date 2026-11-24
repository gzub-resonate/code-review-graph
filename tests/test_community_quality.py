"""Community quality: owners stay with their members, config documents do not
clone, thin communities fold into a neighbour, names and descriptions say
something.

Measured on a Java Spring service where classes were split from their own
methods, every environment's copy of a JSON/YAML file was a community of its
own, and 15 of 45 communities had two members.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_review_graph import communities as cm
from code_review_graph.graph import GraphEdge, GraphNode, GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

needs_igraph = pytest.mark.skipif(not cm.IGRAPH_AVAILABLE, reason="igraph not installed")


def _node(qn: str, kind: str = "Function", file_path: str | None = None) -> GraphNode:
    path, _, name = qn.partition("::")
    return GraphNode(
        id=0, kind=kind, name=name, qualified_name=qn, file_path=file_path or path,
        line_start=1, line_end=2, language="java", parent_name=None, params=None,
        return_type=None, is_test=False, file_hash="x", extra={},
    )


def _edge(kind: str, source: str, target: str) -> GraphEdge:
    return GraphEdge(
        id=0, kind=kind, source_qualified=source, target_qualified=target,
        file_path="x", line=1, extra={},
    )


def _controllers_graph() -> tuple[list[GraphNode], list[GraphEdge]]:
    """Two controllers; ``A.heavy`` calls three of B's methods, none of A's."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for cls in ("A", "B"):
        path = f"ctl/{cls}.java"
        nodes.append(_node(f"{path}::{cls}", "Class"))
        for method in ("m1", "m2", "m3", "heavy"):
            qn = f"{path}::{cls}.{method}"
            nodes.append(_node(qn))
            edges.append(_edge("CONTAINS", f"{path}::{cls}", qn))
    nodes.append(_node("ctl/A.java::GET /heavy", "Endpoint"))
    edges.append(_edge("HANDLES", "ctl/A.java::A.heavy", "ctl/A.java::GET /heavy"))
    for method in ("m1", "m2", "m3"):
        edges.append(_edge("CALLS", "ctl/A.java::A.heavy", f"ctl/B.java::B.{method}"))
        edges.append(_edge("CALLS", f"ctl/B.java::B.{method}", "ctl/B.java::B.heavy"))
    return nodes, edges


class TestOwnersStayWithTheirMembers:
    def test_containment_roots_follow_contains_and_handles(self):
        nodes, edges = _controllers_graph()
        qn_to_idx = {n.qualified_name: i for i, n in enumerate(nodes)}

        roots = cm._containment_roots(nodes, qn_to_idx, edges)

        class_a = qn_to_idx["ctl/A.java::A"]
        for qn in ("A.m1", "A.heavy"):
            assert roots[qn_to_idx[f"ctl/A.java::{qn}"]] == class_a
        # The endpoint belongs to its handler, which belongs to the class.
        assert roots[qn_to_idx["ctl/A.java::GET /heavy"]] == class_a
        assert roots[qn_to_idx["ctl/B.java::B.m1"]] == qn_to_idx["ctl/B.java::B"]

    @needs_igraph
    def test_leiden_keeps_class_methods_and_endpoint_together(self):
        nodes, edges = _controllers_graph()

        result = cm._detect_leiden(nodes, edges, min_size=1)

        owner = {qn: i for i, c in enumerate(result) for qn in c["members"]}
        a_group = {owner[n.qualified_name] for n in nodes if n.file_path == "ctl/A.java"}
        b_group = {owner[n.qualified_name] for n in nodes if n.file_path == "ctl/B.java"}
        assert len(a_group) == 1
        assert len(b_group) == 1

    @needs_igraph
    def test_without_the_grouping_the_method_would_leave_its_class(self, monkeypatch):
        """Guards the test above: with no pinned edge kinds the split happens."""
        monkeypatch.setattr(cm, "_PIN_EDGE_KINDS", frozenset())
        nodes, edges = _controllers_graph()

        result = cm._detect_leiden(nodes, edges, min_size=1)

        owner = {qn: i for i, c in enumerate(result) for qn in c["members"]}
        a_group = {owner[n.qualified_name] for n in nodes if n.file_path == "ctl/A.java"}
        assert len(a_group) > 1

    @needs_igraph
    def test_split_of_an_oversized_community_keeps_owners_whole(self):
        nodes, edges = _controllers_graph()
        everything = {
            "name": "all", "level": 0, "size": len(nodes), "cohesion": 0.5,
            "dominant_language": "java", "description": "",
            "members": [n.qualified_name for n in nodes],
        }

        result = cm._split_oversized(
            [everything], nodes, edges, threshold_pct=0.1, min_split_size=4,
        )

        owner = {qn: i for i, c in enumerate(result) for qn in c["members"]}
        for path in ("ctl/A.java", "ctl/B.java"):
            assert len({owner[n.qualified_name] for n in nodes if n.file_path == path}) == 1

    def test_detect_communities_end_to_end(self):
        nodes, edges = _controllers_graph()
        tmp = tempfile.TemporaryDirectory()
        store = GraphStore(str(Path(tmp.name) / "g.db"))
        try:
            for n in nodes:
                store.upsert_node(NodeInfo(
                    kind=n.kind, name=n.name, file_path=n.file_path,
                    line_start=1, line_end=2, language="java",
                ))
            for e in edges:
                store.upsert_edge(EdgeInfo(
                    kind=e.kind, source=e.source_qualified, target=e.target_qualified,
                    file_path="x", line=1,
                ))
            store.commit()
            result = cm.detect_communities(store, min_size=2)
        finally:
            store.close()
            tmp.cleanup()
        owner = {qn: i for i, c in enumerate(result) for qn in c["members"]}
        a_members = [n.qualified_name for n in nodes if n.file_path == "ctl/A.java"]
        assert len({owner.get(qn) for qn in a_members}) == 1


def _config_store(tmp: str, layout: dict[str, tuple[str, int]]) -> GraphStore:
    """One document per path: ``{path: (kind, number of rows)}``, rows chained."""
    store = GraphStore(str(Path(tmp) / "g.db"))
    for path, (kind, rows) in layout.items():
        previous = None
        for i in range(rows):
            store.upsert_node(NodeInfo(
                kind=kind, name=f"$.k{i}", file_path=path,
                line_start=i + 1, line_end=i + 1, language="json",
            ))
            qn = f"{path}::$.k{i}"
            if previous:
                store.upsert_edge(EdgeInfo(
                    kind="CONTAINS", source=previous, target=qn, file_path=path, line=i + 1,
                ))
            previous = qn
    store.commit()
    return store


class TestConfigDocumentsDoNotClone:
    def test_environment_copies_in_one_directory_are_one_community(self):
        layout = {
            f"ecspresso/taskdefs/taskdef-{env}.json": ("JsonPath", 6)
            for env in ("dev", "qa", "prod", "prodb")
        }
        tmp = tempfile.TemporaryDirectory()
        store = _config_store(tmp.name, layout)
        try:
            result = cm.detect_communities(store, min_size=2)
        finally:
            store.close()
            tmp.cleanup()

        assert len(result) == 1
        assert result[0]["size"] == 24
        assert result[0]["name"] == "taskdefs-json"
        assert "config_document" not in result[0]

    def test_json_and_yaml_and_other_directories_stay_apart(self):
        layout = {
            "a/x.json": ("JsonPath", 4),
            "a/y.yml": ("YamlPath", 4),
            "b/z.json": ("JsonPath", 4),
        }
        tmp = tempfile.TemporaryDirectory()
        store = _config_store(tmp.name, layout)
        try:
            result = cm.detect_communities(store, min_size=2)
        finally:
            store.close()
            tmp.cleanup()

        assert sorted(c["name"] for c in result) == ["a-json", "a-yaml", "b-json"]

    def test_no_two_communities_share_size_and_cohesion(self):
        layout = {
            f"env{i}/docs/service.yml": ("YamlPath", 8) for i in range(4)
        }
        tmp = tempfile.TemporaryDirectory()
        store = _config_store(tmp.name, layout)
        try:
            result = cm.detect_communities(store, min_size=2)
        finally:
            store.close()
            tmp.cleanup()

        # Four sibling directories, one file each: still four distinct names,
        # none of them a pair of identical clusters from Leiden.
        names = [c["name"] for c in result]
        assert len(names) == len(set(names))
        assert all(c["size"] == 8 for c in result)


def _community(name: str, members: list[str], **extra) -> dict:
    return {
        "name": name, "level": 0, "size": len(members), "cohesion": 0.5,
        "dominant_language": "java", "description": "", "members": members, **extra,
    }


class TestThinCommunitiesMerge:
    def test_folds_into_the_neighbour_with_the_most_edge_weight(self):
        nodes = [_node(f"f{i}.java::n{i}") for i in range(8)]
        qn = [n.qualified_name for n in nodes]
        communities = [
            _community("thin", qn[0:2]),
            _community("heavy", qn[2:5]),
            _community("light", qn[5:8]),
        ]
        edges = [
            _edge("CALLS", qn[0], qn[2]), _edge("CALLS", qn[1], qn[3]),
            _edge("CALLS", qn[1], qn[5]),
        ]

        result = cm._merge_thin_communities(communities, nodes, edges)

        assert [c["name"] for c in result] == ["heavy", "light"]
        heavy = result[0]
        assert set(heavy["members"]) == set(qn[0:5])
        assert heavy["size"] == 5
        assert len(result[1]["members"]) == 3

    def test_without_edges_uses_the_same_file_then_the_same_directory(self):
        nodes = [
            _node("d1/A.java::a1"), _node("d1/A.java::a2"), _node("d1/A.java::a3"),
            _node("d2/B.java::b1"), _node("d2/B.java::b2"), _node("d2/B.java::b3"),
            _node("d2/C.java::c1"),
        ]
        qn = [n.qualified_name for n in nodes]
        communities = [
            _community("a", qn[0:3]), _community("b", qn[3:6]),
            _community("thin-in-d2", [qn[6]]),
        ]

        result = cm._merge_thin_communities(communities, nodes, [])

        assert [c["name"] for c in result] == ["a", "b"]
        assert qn[6] in result[1]["members"]

    def test_never_creates_a_community_and_leaves_an_isolated_one(self):
        nodes = [_node("x/A.java::a"), _node("y/B.java::b")]
        communities = [_community("only", ["x/A.java::a"])]

        assert cm._merge_thin_communities(communities, nodes, []) == communities

        two = [_community("p", ["x/A.java::a"]), _community("q", ["y/B.java::b"])]
        result = cm._merge_thin_communities(two, nodes, [])
        assert len(result) <= len(two)

    def test_config_documents_only_merge_with_each_other(self):
        nodes = [_node(f"d/f.json::k{i}", "JsonPath") for i in range(2)] + [
            _node(f"d/C.java::c{i}") for i in range(3)
        ]
        qn = [n.qualified_name for n in nodes]
        communities = [
            _community("d-json", qn[0:2], config_document=True),
            _community("code", qn[2:5]),
        ]
        edges = [_edge("CALLS", qn[0], qn[2])]

        result = cm._merge_thin_communities(communities, nodes, edges)

        assert len(result) == 2

    def test_merged_community_gets_fresh_cohesion_and_a_description(self):
        nodes, edges = _controllers_graph()
        a = [n.qualified_name for n in nodes if n.file_path == "ctl/A.java"]
        b = [n.qualified_name for n in nodes if n.file_path == "ctl/B.java"]
        communities = [_community("a", a), _community("b", b[:-1]), _community("tail", b[-1:])]

        merged = cm._merge_thin_communities(communities, nodes, edges)
        cm._finalize_communities(merged, nodes, edges)

        assert len(merged) == 2
        b_comm = next(c for c in merged if b[-1] in c["members"])
        assert b_comm["size"] == len(b)
        assert b_comm["cohesion"] != 0.5
        assert "merged" not in b_comm


class TestNamesAndDescriptions:
    def test_name_comes_from_the_dominant_directory_not_a_stray_token(self):
        nodes = [_node(f"src/main/java/app/controller/C{i}.java::C{i}", "Class") for i in range(5)]
        nodes.append(_node("src/main/java/app/service/S.java::S", "Class"))

        name = cm._generate_community_name(nodes)

        assert name.startswith("controller")

    def test_dominant_file_names_the_community_after_its_class(self):
        path = "src/main/java/app/controller/HierarchyController.java"
        nodes = [_node(f"{path}::HierarchyController", "Class")] + [
            _node(f"{path}::HierarchyController.m{i}") for i in range(6)
        ] + [_node("src/main/java/app/util/U.java::u")]

        assert cm._generate_community_name(nodes) == "controller-hierarchy-controller"

    def test_test_files_do_not_vote_for_the_name(self):
        prod = [_node(f"app/service/S{i}.java::S{i}", "Class") for i in range(2)]
        tests = [_node(f"app/controller/C{i}Test.java::C{i}Test", "Class") for i in range(5)]

        assert cm._generate_community_name(prod + tests).startswith("service")

    def test_description_lists_files_kinds_endpoints_and_entry_points(self):
        nodes = [
            _node("a/X.java::X", "Class"),
            _node("a/X.java::X.run"),
            _node("a/X.java::X.helper"),
            _node("a/Y.java::GET /y", "Endpoint"),
        ]

        text = cm._describe_community(nodes, called_targets={"a/X.java::X.helper"})

        assert text.startswith("4 nodes (")
        assert "2 Function" in text
        assert "a/X.java (3)" in text
        assert "1 endpoints, 1 entry points" in text
        assert text != "Community of 4 nodes"

    def test_detection_is_deterministic_and_names_are_unique(self):
        layout = {f"d{i % 3}/f{i}.json": ("JsonPath", 5) for i in range(9)}
        runs = []
        for _ in range(2):
            tmp = tempfile.TemporaryDirectory()
            store = _config_store(tmp.name, layout)
            try:
                runs.append(cm.detect_communities(store, min_size=2))
            finally:
                store.close()
                tmp.cleanup()

        assert runs[0] == runs[1]
        names = [c["name"] for c in runs[0]]
        assert len(names) == len(set(names))
