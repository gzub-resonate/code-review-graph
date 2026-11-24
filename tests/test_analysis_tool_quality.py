"""Quality of the analysis tools on layouts a real service produces.

Surprising connections leave out containment, minimal rows of bridges, surprises
and flows say where they are, ``tests_for`` and the untested-hotspot rule share
one meaning of "tested", and a config document is not a thin community.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.analysis import (
    find_bridge_nodes,
    find_hub_nodes,
    find_knowledge_gaps,
    find_surprising_connections,
)
from code_review_graph.communities import store_communities
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import (
    get_bridge_nodes_func,
    get_hub_nodes_func,
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


# ----------------------------------------------- repeated and bare-name edges


def test_one_call_made_five_times_is_one_surprising_row(tmp_path, store):
    a = _node(store, "main", "/r/a.py")
    b = _node(store, "parse", "/r/b.js", language="js")
    for line in range(5):
        store.upsert_edge(EdgeInfo("CALLS", a, b, "/r/a.py", line + 1))
    _edge(store, "REFERENCES", a, b)  # another kind is another row
    store.commit()

    rows = find_surprising_connections(store, top_n=100)

    assert sorted((r["source"], r["target"], r["edge_kind"]) for r in rows) == [
        ("main", "parse", "CALLS"), ("main", "parse", "REFERENCES"),
    ]
    assert get_surprising_connections_func(repo_root=str(tmp_path))["total"] == 2


def _noisy_graph(store):
    """``main`` makes 12 calls to bare names and 1 to a real helper."""
    main = _node(store, "main", "/r/app.py")
    helper = _node(store, "helper", "/r/lib.py")
    real = _node(store, "real_hub", "/r/hub.py")
    callers = [_node(store, f"user{i}", f"/r/u{i}.py") for i in range(4)]
    _edge(store, "CALLS", main, helper)
    for i in range(12):
        _edge(store, "CALLS", main, f"generic{i}")
    for c in callers:
        _edge(store, "CALLS", c, real)
    store.commit()
    return main, helper, real


def test_bare_name_edges_do_not_make_a_hub(store):
    _noisy_graph(store)

    hubs = find_hub_nodes(store, top_n=10)

    by_name = {h["name"]: h for h in hubs}
    assert hubs[0]["name"] == "real_hub"
    assert by_name["main"]["total_degree"] == 1
    assert by_name["main"]["resolution_split"] == {"direct": 1, "bare_name": 12}
    assert by_name["real_hub"]["resolution_split"] == {"direct": 4, "bare_name": 0}


def _shared_name_graph(store):
    """``get`` is defined twice; ``w1..w6`` call the one in reach by name only, and
    ``real`` is called by 4 callers through a typed receiver (direct evidence)."""
    get = _node(store, "get", "/r/a.java", language="java", parent="A")
    _node(store, "get", "/r/b.java", language="java", parent="B")
    real = _node(store, "fetch", "/r/c.java", language="java", parent="C")
    for i in range(6):
        w = _node(store, f"w{i}", f"/r/w{i}.java", language="java")
        _edge(store, "CALLS", w, get, bare_call_target="get")
    for i in range(4):
        c = _node(store, f"c{i}", f"/r/k{i}.java", language="java")
        _edge(store, "CALLS", c, real, bare_call_target="fetch", receiver_class="C")
    store.commit()
    return get, real


def test_name_matched_calls_to_shared_names_are_down_ranked(store):
    _shared_name_graph(store)

    hubs = find_hub_nodes(store, top_n=10)

    by_name = {h["qualified_name"].split("::")[-1]: h for h in hubs}
    assert by_name["A.get"]["total_degree"] == 6
    assert by_name["A.get"]["ranking_degree"] == 1.5
    assert by_name["C.fetch"]["ranking_degree"] == 4.0
    assert hubs[0]["name"] == "fetch"


def test_unshared_name_matched_calls_keep_full_weight(store):
    get = _node(store, "get", "/r/a.java", language="java", parent="A")
    for i in range(3):
        w = _node(store, f"w{i}", f"/r/w{i}.java", language="java")
        _edge(store, "CALLS", w, get, bare_call_target="get")
    store.commit()

    (hub,) = [h for h in find_hub_nodes(store) if h["name"] == "get"]

    assert hub["ranking_degree"] == hub["total_degree"] == 3


def test_name_matched_calls_are_no_path_between_bridges(store):
    left = _node(store, "left", "/r/l.java", language="java")
    get = _node(store, "get", "/r/a.java", language="java", parent="A")
    _node(store, "get", "/r/b.java", language="java", parent="B")
    right = _node(store, "right", "/r/r.java", language="java")
    _edge(store, "CALLS", left, get, bare_call_target="get")
    _edge(store, "CALLS", get, right)
    store.commit()

    assert "get" not in {b["name"] for b in find_bridge_nodes(store)}


def test_a_bare_name_is_no_path_between_bridges(store):
    left = _node(store, "left", "/r/l.py")
    mid = _node(store, "mid", "/r/m.py")
    right = _node(store, "right", "/r/r.py")
    _edge(store, "CALLS", left, mid)
    _edge(store, "CALLS", mid, right)
    # ``shared`` is no node: callers of the same bare name are not connected.
    for name in ("a", "b", "c"):
        n = _node(store, name, f"/r/{name}.py")
        _edge(store, "CALLS", n, "shared")
        _edge(store, "CALLS", "shared", n)
    store.commit()

    names = [b["name"] for b in find_bridge_nodes(store, top_n=10)]

    assert names == ["mid"]


def test_bare_name_edges_are_not_surprising(store):
    a = _node(store, "caller", "/r/a.py")
    _node(store, "other", "/r/b.js", language="js")
    _edge(store, "CALLS", a, "other")  # a bare name that happens to match a node's name
    store.commit()

    assert find_surprising_connections(store, top_n=100) == []


def test_leaf_primitives_are_opt_in(store):
    page = _node(store, "Page", "/r/page.tsx", language="tsx")
    box = _node(store, "FlexBox", "/r/box.tsx", language="tsx")
    test = _node(store, "spec", "/r/box.spec.tsx", kind="Test", language="tsx", is_test=True)
    for i in range(3):
        _edge(store, "CALLS", _node(store, f"View{i}", f"/r/v{i}.tsx", language="tsx"), box)
    _edge(store, "CALLS", page, box)
    _edge(store, "CALLS", box, test)  # a call into test code is no building block
    store.commit()

    assert "FlexBox" in {h["name"] for h in find_hub_nodes(store)}
    kept = {h["name"] for h in find_hub_nodes(store, exclude_leaf_primitives=True)}
    assert "FlexBox" not in kept
    assert {"Page", "View0"} <= kept


# ------------------------------------------------------------ non-code gaps


NON_CODE = [
    ("#exec-summary", "/r/docs/report.html", "DocSection"),
    ("provider", "/r/infra/.terraform.lock.hcl", "YamlPath"),
    ("prev", "/r/ui/.storybook/preview.tsx", "Function"),
    ("m", "/r/ui/.storybook/mocks.ts", "Function"),
    ("stage:0", "/r/Dockerfile", "Function"),
    ("build", "/r/.github/actions/x/Dockerfile", "Function"),
    ("ci", "/r/.github/scripts/ci.py", "Function"),
    ("resolved", "/r/package-lock.json", "JsonPath"),
]


def test_non_code_is_not_an_isolated_node_or_untested_hotspot(tmp_path, store):
    for name, file, kind in NON_CODE:
        qn = _node(store, name, file, kind=kind)
        for i in range(5):
            _edge(store, "CALLS", qn, _node(store, f"{name}_{i}", "/r/deps.py"))
    lonely = _node(store, "lonely_code", "/r/src/app.py")
    hot = _hotspot(store, "real_hotspot", "/r/src/app.py")
    store.commit()

    gaps = find_knowledge_gaps(store)

    isolated = {n["qualified_name"] for n in gaps["isolated_nodes"]}
    hotspots = {h["qualified_name"] for h in gaps["untested_hotspots"]}
    assert lonely in isolated and hot in hotspots
    for name, file, _kind in NON_CODE:
        qn = f"{file}::{name}"
        assert qn not in hotspots, qn
    # The non-code roots have five callees and no caller: opting in brings them back.
    everything = find_knowledge_gaps(store, include_non_code=True)
    assert {f"{f}::{n}" for n, f, _k in NON_CODE} <= {
        h["qualified_name"] for h in everything["untested_hotspots"]
    }
    via_tool = get_knowledge_gaps_func(repo_root=str(tmp_path), include_non_code=True)
    assert via_tool["summary"]["untested_hotspots"] >= len(NON_CODE)


def test_isolated_non_code_nodes_are_left_out_unless_asked_for(tmp_path, store):
    for name, file, kind in NON_CODE:
        _node(store, name, file, kind=kind)
    lonely = _node(store, "lonely_code", "/r/src/app.py")
    store.commit()

    quiet = {n["qualified_name"] for n in find_knowledge_gaps(store)["isolated_nodes"]}
    loud = {
        n["qualified_name"]
        for n in find_knowledge_gaps(store, include_non_code=True)["isolated_nodes"]
    }

    assert quiet == {lonely}
    assert lonely in loud and len(loud) == len(NON_CODE) + 1


def test_a_hotspot_is_ranked_by_edges_that_name_a_node(store):
    # Twelve bare-name calls make no hotspot; five traced edges do.
    noisy = _node(store, "noisy_main", "/r/app.py")
    for i in range(12):
        _edge(store, "CALLS", noisy, f"generic{i}")
    solid = _hotspot(store, "solid", "/r/app2.py")
    store.commit()

    names = {h["name"] for h in find_knowledge_gaps(store)["untested_hotspots"]}

    assert "solid" in names and "noisy_main" not in names
    assert solid


def test_hub_tool_exposes_leaf_primitive_switch_and_keeps_default(tmp_path, store):
    box = _node(store, "FlexBox", "/r/box.tsx", language="tsx")
    for i in range(3):
        _edge(store, "CALLS", _node(store, f"View{i}", f"/r/v{i}.tsx", language="tsx"), box)
    store.commit()

    default = get_hub_nodes_func(repo_root=str(tmp_path))
    filtered = get_hub_nodes_func(repo_root=str(tmp_path), exclude_leaf_primitives=True)

    assert default["hub_nodes"][0]["name"] == "FlexBox"
    assert default["hub_nodes"][0]["resolution_split"] == {"direct": 3, "bare_name": 0}
    assert "FlexBox" not in {h["name"] for h in filtered["hub_nodes"]}


def test_framework_wired_constructors_are_not_isolated_nodes(store):
    cls = _node(store, "AppModule", "/r/AppModule.java", kind="Class", language="java")
    ctor = _node(store, "AppModule", "/r/AppModule.java", language="java", parent="AppModule")
    method = _node(store, "configure", "/r/AppModule.java", language="java", parent="AppModule")
    orphan_ctor = _node(store, "Widget", "/r/Widget.java", language="java", parent="Widget")
    _edge(store, "CONTAINS", cls, ctor)
    _edge(store, "CONTAINS", cls, method)
    store.commit()

    isolated = {n["qualified_name"] for n in find_knowledge_gaps(store)["isolated_nodes"]}

    assert ctor not in isolated
    assert method in isolated  # a method nothing calls is still a gap
    assert orphan_ctor in isolated  # no CONTAINS edge either: still reported
    everything = find_knowledge_gaps(store, include_non_code=True)["isolated_nodes"]
    assert ctor in {n["qualified_name"] for n in everything}


def test_a_constructor_is_not_an_untested_hotspot(store):
    cls = _node(store, "Feed", "/r/Feed.java", kind="Class", language="java")
    ctor = _node(store, "Feed", "/r/Feed.java", language="java", parent="Feed")
    _edge(store, "CONTAINS", cls, ctor)
    for i in range(6):
        _edge(store, "CALLS", _node(store, f"make{i}", f"/r/m{i}.java", language="java"), ctor)
    store.commit()

    hot = {h["qualified_name"] for h in find_knowledge_gaps(store)["untested_hotspots"]}

    assert ctor not in hot
