"""Quality of the analysis tools on layouts a real service produces.

Surprising connections leave out containment, minimal rows of bridges, surprises
and flows say where they are, ``tests_for`` and the untested-hotspot rule share
one meaning of "tested", and a config document is not a thin community.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.analysis import find_knowledge_gaps, find_surprising_connections
from code_review_graph.communities import store_communities
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import (
    get_bridge_nodes_func,
    get_knowledge_gaps_func,
    get_surprising_connections_func,
    list_flows,
    query_graph,
)


@pytest.fixture
def store(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        yield s


def _node(store, name, file, kind="Function", language="python", parent=None, **kw):
    store.upsert_node(NodeInfo(kind, name, file, 1, 5, language, parent_name=parent, **kw))
    return f"{file}::{parent + '.' if parent else ''}{name}"


def _edge(store, kind, source, target, **extra):
    store.upsert_edge(EdgeInfo(kind, source, target, source.split("::")[0], 3, extra=extra))


# ---------------------------------------------------------------- surprising


def test_containment_is_never_a_surprising_connection(store):
    # Different file extensions would score a cross-language bonus, so only the
    # exclusion keeps these out.
    cls = _node(store, "Svc", "/r/a.java", kind="Class", language="java")
    method = _node(store, "load", "/r/b.py", parent="Svc")
    inner = _node(store, "Inner", "/r/c.js", kind="Class", language="js", parent="Svc")
    other = _node(store, "other", "/r/d.js", language="js")
    _edge(store, "CONTAINS", cls, method)
    _edge(store, "CONTAINS", cls, inner)
    # The method calling the class that holds it is the same layout read backwards.
    _edge(store, "CALLS", method, cls)
    # A real coupling across languages stays.
    _edge(store, "CALLS", method, other)
    store.commit()

    rows = find_surprising_connections(store, top_n=100)

    assert [(r["source_qualified"], r["target_qualified"], r["edge_kind"]) for r in rows] == [
        (method, other, "CALLS"),
    ]
    assert rows[0]["source_file"] == "/r/b.py"
    assert rows[0]["target_file"] == "/r/d.js"


def test_containment_through_parent_name_alone_is_left_out(store):
    cls = _node(store, "Svc", "/r/a.java", kind="Class", language="java")
    method = _node(store, "load", "/r/a.java", parent="Svc")
    _node(store, "x", "/r/z.js", language="js")
    _edge(store, "CALLS", cls, method)  # no CONTAINS edge stored
    store.commit()

    assert find_surprising_connections(store, top_n=100) == []


def test_tool_total_does_not_count_containment(tmp_path, store):
    cls = _node(store, "Svc", "/r/a.java", kind="Class", language="java")
    method = _node(store, "load", "/r/b.py", parent="Svc")
    other = _node(store, "other", "/r/d.js", language="js")
    _edge(store, "CONTAINS", cls, method)
    _edge(store, "CALLS", method, other)
    store.commit()

    result = get_surprising_connections_func(repo_root=str(tmp_path))

    assert result["total"] == 1
    assert {r["edge_kind"] for r in result["surprising_connections"]} == {"CALLS"}


# ------------------------------------------------------------ minimal rows


FILES = {
    "app.py": "from lib import step\n\n\ndef main():\n    return step()\n",
    "lib.py": "from deep import leaf\n\n\ndef step():\n    return leaf()\n",
    "deep.py": "def leaf():\n    return 1\n",
    "other/app.py": "from lib import step\n\n\ndef main():\n    return step()\n",
}


@pytest.fixture
def built(tmp_path):
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    with GraphStore(get_db_path(tmp_path)) as s:
        full_build(tmp_path, s)
        store_flows(s, trace_flows(s))
    return tmp_path


def test_minimal_flow_rows_tell_same_named_entries_apart(built):
    result = list_flows(repo_root=str(built), detail_level="minimal")

    runs = [f for f in result["flows"] if f["name"] == "main"]
    assert len(runs) == 2
    assert {f["file"] for f in runs} == {"app.py", "other/app.py"}
    assert all(f["qualified_name"] == f"{f['file']}::main" for f in runs)
    assert set(runs[0]) == {"name", "qualified_name", "file", "criticality", "node_count"}


def test_minimal_bridge_rows_carry_qualified_name_and_relative_file(built):
    result = get_bridge_nodes_func(repo_root=str(built), detail_level="minimal")

    assert result["bridge_nodes"]
    for row in result["bridge_nodes"]:
        assert set(row) == {"name", "qualified_name", "file", "kind", "betweenness"}
        assert not Path(row["file"]).is_absolute()
        assert row["qualified_name"].startswith(row["file"])


def test_minimal_surprise_rows_carry_both_ends_location(tmp_path, store):
    a = _node(store, "caller", "/r/a.py")
    b = _node(store, "callee", "/r/b.js", language="js")
    _edge(store, "CALLS", a, b)
    store.commit()
    store.set_metadata("repo_root", "/r")

    result = get_surprising_connections_func(repo_root=str(tmp_path), detail_level="minimal")

    row = result["surprising_connections"][0]
    assert set(row) == {
        "source", "source_qualified", "source_file",
        "target", "target_qualified", "target_file", "edge_kind", "surprise_score",
    }
    assert row["source_file"] == "/r/a.py"  # outside the repo root: as stored


# ------------------------------------------------------------------ tests_for


def _service_graph(store):
    """service.run -> mapper.map (tested by 2 mapper tests); helper path to run."""
    run = _node(store, "run", "/r/service.py")
    mapper = _node(store, "map", "/r/mapper.py")
    shared = _node(store, "other", "/r/other.py")
    helper = _node(store, "make", "/r/test_helpers.py", is_test=True)
    t1 = _node(store, "check_mapping", "/r/test_mapper.py", kind="Test", is_test=True)
    t2 = _node(store, "check_mapping_nulls", "/r/test_mapper.py", kind="Test", is_test=True)
    t3 = _node(store, "check_run", "/r/test_service.py", kind="Test", is_test=True)
    _edge(store, "CALLS", run, mapper)
    _edge(store, "CALLS", run, "bare_unknown")  # an unresolved bare name
    _edge(store, "CALLS", helper, run)
    _edge(store, "CALLS", t3, helper)
    _edge(store, "TESTED_BY", mapper, t1)
    _edge(store, "TESTED_BY", mapper, t2)
    _edge(store, "TESTED_BY", "bare_unknown", t1)  # a bare-name edge is never walked
    store.commit()
    return run, mapper, shared, (t1, t2, t3)


def test_transitive_tests_come_direct_first_then_by_path_length(store):
    run, mapper, _shared, (t1, t2, t3) = _service_graph(store)
    direct = _node(store, "check_run_direct", "/r/test_service.py", kind="Test", is_test=True)
    _edge(store, "TESTED_BY", run, direct)
    store.commit()

    rows = store.get_transitive_tests(run)

    assert [r["qualified_name"] for r in rows] == [direct, t3, t1, t2]
    assert [r["indirect"] for r in rows] == [False, True, True, True]
    helper_row, callee_row = rows[1], rows[2]
    assert (helper_row["route"], helper_row["hops"]) == ("helper", 2)
    assert (callee_row["route"], callee_row["hops"], callee_row["via"]) == ("callee", 1, mapper)
    assert "hops" not in rows[0]


def test_callee_routes_can_be_left_out(store):
    run, _mapper, _shared, (_t1, _t2, t3) = _service_graph(store)

    rows = store.get_transitive_tests(run, include_callee_routes=False)

    assert [r["qualified_name"] for r in rows] == [t3]


def test_tests_for_labels_a_callee_test_and_names_the_callee(tmp_path, store):
    run, _mapper, _shared, (t1, _t2, t3) = _service_graph(store)

    result = query_graph("tests_for", run, repo_root=str(tmp_path), max_results=50)

    by_name = {r["qualified_name"]: r for r in result["results"]}
    assert [r["qualified_name"] for r in result["results"]][0] == t3
    assert "inferred_by" not in by_name[t3]
    assert by_name[t3]["hops"] == 2
    assert by_name[t1]["inferred_by"] == "callee_test"
    assert by_name[t1]["via"] == "map"
    assert by_name[t1]["hops"] == 1
    assert "2 of them guesses" in result["summary"]

    minimal = query_graph(
        "tests_for", run, repo_root=str(tmp_path), detail_level="minimal", max_results=50,
    )
    row = next(r for r in minimal["results"] if r["name"] == "check_mapping")
    assert (row["inferred_by"], row["via"], row["hops"]) == ("callee_test", "map", 1)


# ------------------------------------------------------------ knowledge gaps


def _hotspot(store, name, file="/r/svc.py"):
    qn = _node(store, name, file)
    for i in range(5):
        callee = _node(store, f"{name}_dep{i}", "/r/deps.py")
        _edge(store, "CALLS", qn, callee)
    return qn


def test_hotspot_with_only_callee_tests_is_untested_like_tests_for_says(tmp_path, store):
    hot = _hotspot(store, "upload")
    dep = "/r/deps.py::upload_dep0"
    test = _node(store, "check_dep", "/r/test_deps.py", kind="Test", is_test=True)
    _edge(store, "TESTED_BY", dep, test)
    store.commit()

    gaps = find_knowledge_gaps(store)["untested_hotspots"]
    assert hot in {h["qualified_name"] for h in gaps}

    rows = query_graph("tests_for", hot, repo_root=str(tmp_path))["results"]
    assert [r["inferred_by"] for r in rows] == ["callee_test"]


def test_hotspot_tests_for_covers_is_not_reported(tmp_path, store):
    direct = _hotspot(store, "direct_one")
    routed = _hotspot(store, "routed_one", "/r/svc2.py")
    t = _node(store, "check_direct", "/r/test_a.py", kind="Test", is_test=True)
    helper = _node(store, "make", "/r/test_helpers.py", is_test=True)
    t2 = _node(store, "check_routed", "/r/test_b.py", kind="Test", is_test=True)
    _edge(store, "TESTED_BY", direct, t)
    _edge(store, "CALLS", helper, routed)
    _edge(store, "CALLS", t2, helper)
    store.commit()

    reported = {h["qualified_name"] for h in find_knowledge_gaps(store)["untested_hotspots"]}

    assert direct not in reported and routed not in reported
    for qn in (direct, routed):
        rows = query_graph("tests_for", qn, repo_root=str(tmp_path))["results"]
        assert any(not r.get("inferred_by") for r in rows)


def test_tools_agree_on_every_reported_hotspot(tmp_path, store):
    for index in range(4):
        _hotspot(store, f"h{index}", f"/r/s{index}.py")
    store.commit()

    gaps = find_knowledge_gaps(store)["untested_hotspots"]
    assert len(gaps) == 4
    for hot in gaps:
        rows = query_graph("tests_for", hot["qualified_name"], repo_root=str(tmp_path))
        assert all(r.get("inferred_by") for r in rows["results"])


def test_config_document_communities_are_not_gaps(tmp_path, store):
    keys = [
        _node(store, f"$.k{i}", "/r/config.json", kind="JsonPath", language="json")
        for i in range(5)
    ]
    thin_keys = [
        _node(store, f"$.t{i}", "/r/small.json", kind="JsonPath", language="json")
        for i in range(2)
    ]
    code = [_node(store, f"f{i}", "/r/one.py") for i in range(3)]
    thin_code = [_node(store, f"g{i}", "/r/two.py") for i in range(2)]
    store.commit()
    store_communities(store, [
        {"name": "config", "size": 5, "members": keys},
        {"name": "small-config", "size": 2, "members": thin_keys},
        {"name": "one-file", "size": 3, "members": code},
        {"name": "thin-code", "size": 2, "members": thin_code},
    ])

    gaps = find_knowledge_gaps(store)

    assert [c["name"] for c in gaps["single_file_communities"]] == ["one-file"]
    assert [c["name"] for c in gaps["thin_communities"]] == ["thin-code"]
    summary = get_knowledge_gaps_func(repo_root=str(tmp_path))["summary"]
    assert summary["single_file_communities"] == 1
    assert summary["thin_communities"] == 1
