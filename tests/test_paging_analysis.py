"""Cursor paging for the analysis, architecture, flow, community and registry tools."""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

import pytest

import code_review_graph.main as main_module
import code_review_graph.tools.analysis_tools as analysis_module
import code_review_graph.tools.community_tools as community_module
import code_review_graph.tools.flows_tools as flows_module
import code_review_graph.tools.registry_tools as registry_module
from code_review_graph import paging
from code_review_graph.analysis import find_bridge_nodes, find_knowledge_gaps
from code_review_graph.communities import store_communities
from code_review_graph.flows import store_flows
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools import (
    get_architecture_overview_func,
    get_bridge_nodes_func,
    get_community_func,
    get_flow,
    get_knowledge_gaps_func,
    get_surprising_connections_func,
)
from code_review_graph.tools.registry_tools import cross_repo_search_func

COUNT = 60
ISOLATED = 70
GROUPS = 4  # communities of COUNT / GROUPS nodes each


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    src = (root / "m.py").as_posix()
    other = (root / "n.py").as_posix()
    ids = []
    for i in range(COUNT):
        ids.append(store.upsert_node(NodeInfo(
            kind="Function", name=f"f{i:03d}", file_path=src,
            line_start=i * 3 + 1, line_end=i * 3 + 2, language="python",
        )))
    for i in range(ISOLATED):
        store.upsert_node(NodeInfo(
            kind="Function", name=f"iso{i:03d}", file_path=other,
            line_start=i * 3 + 1, line_end=i * 3 + 2, language="python",
        ))
    # Degrees tie in groups of five, and every node reaches every community.
    for i in range(COUNT):
        targets = {(i + j + 1) % COUNT for j in range(i % 5)}
        targets |= {(i + step) % COUNT for step in (15, 30, 45)}
        for t in sorted(targets):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{src}::f{i:03d}",
                target=f"{src}::f{t:03d}", file_path=src, line=i,
            ))
    store.commit()
    size = COUNT // GROUPS
    store_communities(store, [
        {
            "name": f"comm{g}", "size": size, "cohesion": 0.5,
            "members": [f"{src}::f{i:03d}" for i in range(g * size, (g + 1) * size)],
        }
        for g in range(GROUPS)
    ] + [
        {"name": f"thin{t:02d}", "size": 1, "cohesion": 0.1, "members": []}
        for t in range(20)
    ])
    store_flows(store, [{
        "name": "long-flow", "entry_point_id": ids[0], "depth": 3, "node_count": COUNT,
        "file_count": 1, "criticality": 0.9, "path": ids,
    }])
    yield root, store
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def error_of(call, **args) -> str:
    """The message of a refused call: an error reply, or a raised error."""
    try:
        reply = call(**args)
    except paging.CursorError as exc:
        return str(exc)
    assert reply["status"] == "error", reply
    return reply["error"]


def read_pages(call, extract, limit_name, limit, **args):
    """Read a whole list page by page: (rows, replies)."""
    rows: list = []
    replies: list = []
    cursor = None
    while True:
        reply = call(**{limit_name: limit}, cursor=cursor, **args)
        assert reply["status"] == "ok", reply
        replies.append(reply)
        rows.extend(extract(reply))
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, replies
        assert len(replies) < 1000


def key_of(row, *fields):
    return tuple(row[f] for f in fields)


def make_specs(root):
    r = str(root)
    return {
        "bridges": dict(
            call=lambda **a: get_bridge_nodes_func(repo_root=r, **a),
            extract=lambda x: [key_of(h, "qualified_name") for h in x["bridge_nodes"]],
            limit_name="top_n", args={},
        ),
        "surprises": dict(
            call=lambda **a: get_surprising_connections_func(repo_root=r, **a),
            extract=lambda x: [
                key_of(s, "source_qualified", "target_qualified", "edge_kind")
                for s in x["surprising_connections"]
            ],
            limit_name="top_n", args={},
        ),
        "flow_steps": dict(
            call=lambda **a: get_flow(repo_root=r, flow_id=1, **a),
            extract=lambda x: [key_of(s, "node_id") for s in x["flow"]["steps"]],
            limit_name="max_steps", args={},
        ),
        "members": dict(
            call=lambda **a: get_community_func(repo_root=r, community_id=1, **a),
            extract=lambda x: x["community"]["members"],
            limit_name="max_members", args={},
        ),
        "member_details": dict(
            call=lambda **a: get_community_func(
                repo_root=r, community_id=1, include_members=True, **a),
            extract=lambda x: [
                key_of(m, "qualified_name") for m in x["community"]["member_details"]
            ],
            limit_name="max_members", args={},
        ),
        "cross_edges": dict(
            call=lambda **a: get_architecture_overview_func(
                repo_root=r, detail_level="standard", **a),
            extract=lambda x: [
                key_of(e, "source", "target", "edge_kind") for e in x["cross_community_edges"]
            ],
            limit_name="max_results", args={},
        ),
        "cross_pairs": dict(
            call=lambda **a: get_architecture_overview_func(repo_root=r, **a),
            extract=lambda x: [
                key_of(e, "source_community", "target_community")
                for e in x["cross_community_edges"]
            ],
            limit_name="max_results", args={},
        ),
        "warnings": dict(
            call=lambda **a: get_architecture_overview_func(repo_root=r, **a),
            extract=lambda x: x["warnings"],
            limit_name="max_results", args={},
        ),
    }


SPEC_NAMES = [
    "bridges", "surprises", "flow_steps", "members", "member_details",
    "cross_edges", "cross_pairs", "warnings",
]


def whole_of(spec):
    reply = spec["call"](**{spec["limit_name"]: 200}, **spec["args"])
    assert reply["status"] == "ok"
    return reply


@pytest.mark.parametrize("name", SPEC_NAMES)
@pytest.mark.parametrize("limit", [1, 7, 25])
def test_paging_reads_every_row_once_in_order(repo, name, limit):
    root, _ = repo
    spec = make_specs(root)[name]
    # The biggest page the tool allows, read to the end: the reference order.
    expected, _ = read_pages(
        spec["call"], spec["extract"], spec["limit_name"], 200, **spec["args"])
    assert len(expected) > 1

    rows, replies = read_pages(
        spec["call"], spec["extract"], spec["limit_name"], limit, **spec["args"],
    )
    assert rows == expected
    assert [r.get("offset", 0) for r in replies][0] == 0
    assert "next_cursor" not in replies[-1]
    assert all("next_cursor" in r for r in replies[:-1])


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_the_first_page_is_the_old_output_plus_a_cursor(repo, name):
    root, _ = repo
    spec = make_specs(root)[name]
    whole_rows = spec["extract"](whole_of(spec))
    first = spec["call"](**{spec["limit_name"]: 3}, **spec["args"])
    assert spec["extract"](first) == whole_rows[:3]
    assert "offset" not in first
    if len(whole_rows) > 3:
        assert "next_cursor" in first


@pytest.mark.parametrize("name", ["bridges", "surprises", "flow_steps", "cross_edges"])
def test_truncated_is_true_exactly_when_a_cursor_is_present(repo, name):
    root, _ = repo
    spec = make_specs(root)[name]
    _, replies = read_pages(spec["call"], spec["extract"], spec["limit_name"], 9)
    truncated = (lambda r: r["flow"]["truncated"]) if name == "flow_steps" else (
        lambda r: r["truncated"]
    )
    assert [truncated(r) for r in replies] == [True] * (len(replies) - 1) + [False]


def test_the_summary_names_a_later_page(repo):
    root, _ = repo
    call = make_specs(root)["bridges"]["call"]
    first = call(top_n=10)
    total = first["total"]
    assert f", showing 10 of {total}" in first["summary"]
    second = call(top_n=10, cursor=first["next_cursor"])
    assert second["offset"] == 10 and f", showing 11-20 of {total}" in second["summary"]
    assert second["total"] == total


def test_page_size_may_change_between_pages(repo):
    root, _ = repo
    call = make_specs(root)["surprises"]["call"]
    whole = [r["source_qualified"] for r in call(top_n=200)["surprising_connections"]]
    first = call(top_n=4)
    second = call(top_n=9, cursor=first["next_cursor"])
    assert [r["source_qualified"] for r in second["surprising_connections"]] == whole[4:13]


def test_ceilings_bound_a_page_not_the_list(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(analysis_module, "_MAX_BRIDGE_NODES", 10)
    monkeypatch.setattr(analysis_module, "_MAX_SURPRISING", 10)
    monkeypatch.setattr(flows_module, "_MAX_FLOW_STEPS", 10)
    monkeypatch.setattr(community_module, "_MAX_MEMBERS", 5)
    monkeypatch.setattr(community_module, "_MAX_CROSS_EDGES", 20)
    specs = make_specs(root)
    for name, size in [
        ("bridges", 10), ("surprises", 10), ("flow_steps", 10), ("members", 5),
        ("cross_edges", 20),
    ]:
        spec = specs[name]
        rows, replies = read_pages(spec["call"], spec["extract"], spec["limit_name"], 500)
        assert len(replies) > 1, name
        assert all(len(spec["extract"](r)) <= size for r in replies), name
        if "total" in replies[0]:
            assert len(rows) == replies[0]["total"], name


def test_flow_steps_and_community_members_report_totals(repo):
    root, _ = repo
    specs = make_specs(root)
    first = specs["flow_steps"]["call"](max_steps=10)
    assert first["flow"]["total_steps"] == COUNT and first["flow"]["truncated"] is True
    later = specs["flow_steps"]["call"](max_steps=10, cursor=first["next_cursor"])
    assert later["offset"] == 10 and later["flow"]["total_steps"] == COUNT
    first = specs["members"]["call"](max_members=4)
    assert first["community"]["members_truncated"] and first["community"]["members_total"] == 15
    later = specs["members"]["call"](max_members=4, cursor=first["next_cursor"])
    assert later["offset"] == 4 and later["community"]["members_total"] == 15
    last = specs["members"]["call"](max_members=100, cursor=first["next_cursor"])
    assert "members_truncated" not in last["community"] and "next_cursor" not in last


def test_members_and_details_advance_together(repo):
    root, _ = repo
    reply = get_community_func(
        repo_root=str(root), community_id=1, include_members=True, max_members=6,
    )
    again = get_community_func(
        repo_root=str(root), community_id=1, include_members=True, max_members=6,
        cursor=reply["next_cursor"],
    )
    names = [m["qualified_name"] for m in again["community"]["member_details"]]
    assert names == again["community"]["members"]
    assert names and names != [m["qualified_name"] for m in reply["community"]["member_details"]]


def test_the_overview_pages_edges_and_warnings_together(repo):
    root, _ = repo
    call = make_specs(root)["warnings"]["call"]
    whole = call(max_results=200)
    assert len(whole["warnings"]) == GROUPS * (GROUPS - 1) // 2
    first = call(max_results=4)
    assert len(first["warnings"]) == 4 and first["truncated"] is True
    second = call(max_results=4, cursor=first["next_cursor"])
    assert second["warnings"] == whole["warnings"][4:8]
    assert second["cross_community_edges_total"] == whole["cross_community_edges_total"]
    assert "showing 5-" in second["summary"]


# ---------------------------------------------------------------------------
# Knowledge gaps: every category advances together
# ---------------------------------------------------------------------------


def gap_rows(reply):
    return {c: [r["qualified_name"] if "qualified_name" in r else r["name"] for r in rows]
            for c, rows in reply["gaps"].items()}


def test_knowledge_gaps_page_every_category_once(repo):
    root, _ = repo
    call = lambda **a: get_knowledge_gaps_func(repo_root=str(root), **a)  # noqa: E731
    whole = call(max_per_category=50)
    full = {c: [] for c in whole["gaps"]}
    cursor = None
    pages = 0
    while True:
        reply = call(max_per_category=12, cursor=cursor)
        assert reply["status"] == "ok"
        pages += 1
        for category, rows in gap_rows(reply).items():
            full[category].extend(rows)
        assert reply["summary"] == whole["summary"]
        cursor = reply.get("next_cursor")
        assert reply["truncated"] is (cursor is not None)
        if cursor is None:
            break
        assert pages < 100
    for category, rows in full.items():
        assert len(rows) == whole["summary"][category], category
        assert len(set(rows)) == len(rows), category
    # Isolated nodes are no longer cut at 50 inside the analysis: all of them are reachable.
    assert whole["summary"]["isolated_nodes"] > 50
    assert whole["summary"]["thin_communities"] == 20


def test_knowledge_gaps_first_page_matches_the_unpaged_form(repo):
    root, store = repo
    first = get_knowledge_gaps_func(repo_root=str(root), max_per_category=15)
    raw = find_knowledge_gaps(store, repo_root=root)
    for category, rows in first["gaps"].items():
        assert [r.get("qualified_name", r.get("name")) for r in rows] == [
            r.get("qualified_name", r.get("name")) for r in raw[category][:15]
        ], category
    assert "offset" not in first and "next_cursor" in first


def test_knowledge_gaps_cursor_past_a_shorter_list_leaves_it_empty(repo):
    root, _ = repo
    call = lambda **a: get_knowledge_gaps_func(repo_root=str(root), **a)  # noqa: E731
    cursor = call(max_per_category=15)["next_cursor"]
    for _ in range(3):
        reply = call(max_per_category=15, cursor=cursor)
        cursor = reply.get("next_cursor") or cursor
    reply = call(max_per_category=15, cursor=cursor)
    assert reply["gaps"]["import_cycles"] == []
    assert reply["offset"] > 0


def test_knowledge_gaps_minimal_detail_may_change_between_pages(repo):
    root, _ = repo
    first = get_knowledge_gaps_func(repo_root=str(root), max_per_category=5)
    second = get_knowledge_gaps_func(
        repo_root=str(root), max_per_category=5, detail_level="minimal",
        cursor=first["next_cursor"],
    )
    assert second["status"] == "ok" and "file" not in second["gaps"]["isolated_nodes"][0]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def refusal_calls(root):
    r = str(root)
    return {
        "bridges": (lambda **a: get_bridge_nodes_func(repo_root=r, **a), "top_n"),
        "surprises": (lambda **a: get_surprising_connections_func(repo_root=r, **a), "top_n"),
        "gaps": (lambda **a: get_knowledge_gaps_func(repo_root=r, **a), "max_per_category"),
        "flow": (lambda **a: get_flow(repo_root=r, flow_id=1, **a), "max_steps"),
        "community": (
            lambda **a: get_community_func(repo_root=r, community_id=1, **a), "max_members"),
        "overview": (lambda **a: get_architecture_overview_func(repo_root=r, **a), "max_results"),
    }


@pytest.mark.parametrize("tool", sorted(refusal_calls(Path("."))))
def test_a_bad_cursor_is_refused(repo, tool):
    root, _ = repo
    call, limit_name = refusal_calls(root)[tool]
    for bad in ("garbage", "A" * 600, "e30"):
        assert "start over" in error_of(call, **{limit_name: 3}, cursor=bad)


@pytest.mark.parametrize("tool", sorted(refusal_calls(Path("."))))
def test_a_tampered_cursor_is_refused(repo, tool):
    root, _ = repo
    call, limit_name = refusal_calls(root)[tool]
    cursor = call(**{limit_name: 3})["next_cursor"]
    tampered = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
    assert error_of(call, **{limit_name: 3}, cursor=tampered)


def test_a_cursor_does_not_cross_tools_or_queries(repo):
    root, _ = repo
    calls = refusal_calls(root)
    cursors = {n: c(**{lim: 3})["next_cursor"] for n, (c, lim) in calls.items()}
    for name, (call, limit_name) in calls.items():
        for other, cursor in cursors.items():
            if other != name:
                assert "different query" in error_of(call, **{limit_name: 3}, cursor=cursor), (
                    name, other)
    # The same tool on another community / detail level is another query.
    community = get_community_func(repo_root=str(root), community_id=1, max_members=3)
    assert "different query" in error_of(
        lambda **a: get_community_func(repo_root=str(root), community_id=2, **a),
        max_members=3, cursor=community["next_cursor"],
    )
    overview = get_architecture_overview_func(
        repo_root=str(root), detail_level="standard", max_results=3,
    )
    assert "different query" in error_of(
        lambda **a: get_architecture_overview_func(repo_root=str(root), **a),
        max_results=3, cursor=overview["next_cursor"],
    )


def test_a_community_cursor_works_by_name_or_id(repo):
    root, _ = repo
    by_id = get_community_func(repo_root=str(root), community_id=1, max_members=3)
    by_name = get_community_func(
        repo_root=str(root), community_name="comm0", max_members=3,
        cursor=by_id["next_cursor"],
    )
    assert by_name["offset"] == 3


@pytest.mark.parametrize("tool", sorted(refusal_calls(Path("."))))
def test_a_cursor_does_not_survive_a_graph_change(repo, tool):
    root, store = repo
    call, limit_name = refusal_calls(root)[tool]
    cursor = call(**{limit_name: 3})["next_cursor"]
    store.upsert_node(NodeInfo(
        kind="Function", name="late", file_path=(root / "m.py").as_posix(),
        line_start=900, line_end=901, language="python",
    ))
    store.commit()
    assert "graph changed" in error_of(call, **{limit_name: 3}, cursor=cursor)
    assert "next_cursor" in call(**{limit_name: 3})


# ---------------------------------------------------------------------------
# Deterministic order with ties
# ---------------------------------------------------------------------------


def test_bridge_ties_break_by_qualified_name(repo):
    root, store = repo
    rows = find_bridge_nodes(store, top_n=10**9)
    keys = [(-r["betweenness"], r["qualified_name"]) for r in rows]
    assert keys == sorted(keys)
    assert len({r["betweenness"] for r in rows}) < len(rows)  # the fixture does tie


def test_surprise_ties_break_by_endpoints(repo):
    root, _ = repo
    rows = get_surprising_connections_func(repo_root=str(root), top_n=100)[
        "surprising_connections"]
    keys = [
        (-r["surprise_score"], r["source_qualified"], r["target_qualified"], r["edge_kind"])
        for r in rows
    ]
    assert keys == sorted(keys)
    assert len({r["surprise_score"] for r in rows}) < len(rows)


def test_gap_lists_have_a_fixed_order(repo):
    _, store = repo
    gaps = find_knowledge_gaps(store, max_isolated=None, max_untested=None)
    hot = [(-g["degree"], g["qualified_name"]) for g in gaps["untested_hotspots"]]
    assert hot == sorted(hot) and len(hot) > 20
    isolated = [g["qualified_name"] for g in gaps["isolated_nodes"]]
    assert isolated == sorted(isolated) and len(isolated) == ISOLATED
    assert [c["community_id"] for c in gaps["thin_communities"]] == sorted(
        c["community_id"] for c in gaps["thin_communities"])


def test_gap_defaults_keep_the_old_caps(repo):
    _, store = repo
    gaps = find_knowledge_gaps(store)
    assert len(gaps["isolated_nodes"]) == 50 and len(gaps["untested_hotspots"]) == 20


def test_reads_are_the_same_every_time(repo):
    root, _ = repo
    for name, spec in make_specs(root).items():
        reads = [spec["extract"](whole_of(spec)) for _ in range(3)]
        assert reads[0] == reads[1] == reads[2], name


def test_large_graph_bridge_sampling_is_seeded(monkeypatch):
    seen = {}
    import networkx as nx

    real = nx.betweenness_centrality

    def spy(graph, *args, **kwargs):
        seen.update(kwargs)
        return real(graph, *args, **kwargs)

    class FakeStore:
        def _build_networkx_graph(self):
            graph = nx.DiGraph()
            graph.add_edges_from((f"a{i}", f"a{i + 1}") for i in range(5100))
            return graph

        def get_all_community_ids(self):
            return {}

        def get_all_nodes(self, exclude_files=True):
            return []

        def shrink_memory(self, clear_nx_cache=False):
            return None

    monkeypatch.setattr(nx, "betweenness_centrality", spy)
    find_bridge_nodes(FakeStore(), top_n=5)  # type: ignore[arg-type]
    assert seen.get("k") == 500 and seen.get("seed") == 0


# ---------------------------------------------------------------------------
# Cross-repo search
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def indexed_repo(root, registry, *, alias, count):
    root = root.resolve()
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "service.py"),
            [
                NodeInfo(
                    kind="Function", name=f"lookup_{alias}_{i:03}",
                    file_path=str(root / "service.py"),
                    line_start=i + 1, line_end=i + 1, language="python",
                )
                for i in range(count)
            ],
            [],
        )
    registry.register(str(root), alias=alias)
    return root


def ids_of(reply):
    return [(r["repo"], r["qualified_name"]) for r in reply["results"]]


@pytest.fixture
def two_repos(tmp_path, registry):
    first = indexed_repo(tmp_path / "one", registry, alias="one", count=40)
    second = indexed_repo(tmp_path / "two", registry, alias="two", count=25)
    return first, second


@pytest.mark.parametrize("limit", [1, 7, 30])
def test_cross_repo_paging_reads_every_result_once(two_repos, limit):
    whole = cross_repo_search_func(query="lookup", limit=40, max_results=100)
    expected = ids_of(whole)
    assert whole["total"] == 65 and "next_cursor" not in whole and not whole["truncated"]
    rows, replies = read_pages(
        lambda **a: cross_repo_search_func(query="lookup", limit=40, **a),
        ids_of, "max_results", limit,
    )
    assert rows == expected and len(set(rows)) == len(rows)
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    assert all(r["total"] == 65 for r in replies)


def test_cross_repo_first_page_and_summary(two_repos):
    whole = cross_repo_search_func(query="lookup", limit=40, max_results=100)
    first = cross_repo_search_func(query="lookup", limit=40, max_results=10)
    assert ids_of(first) == ids_of(whole)[:10] and "offset" not in first
    assert ", showing 10 of 65" in first["summary"]
    second = cross_repo_search_func(
        query="lookup", limit=40, max_results=10, cursor=first["next_cursor"],
    )
    assert second["offset"] == 10 and ", showing 11-20 of 65" in second["summary"]


def test_cross_repo_ceiling_bounds_a_page(two_repos, monkeypatch):
    monkeypatch.setattr(registry_module, "_MAX_CROSS_REPO_RESULTS", 20)
    rows, replies = read_pages(
        lambda **a: cross_repo_search_func(query="lookup", limit=40, **a),
        ids_of, "max_results", 500,
    )
    assert [len(r["results"]) for r in replies] == [20, 20, 20, 5] and len(rows) == 65


def test_cross_repo_cursor_is_refused_for_another_query(two_repos):
    first = cross_repo_search_func(query="lookup", limit=40, max_results=10)
    cursor = first["next_cursor"]
    for other in (
        dict(query="lookup", limit=39), dict(query="lookup_one", limit=40),
        dict(query="lookup", limit=40, kind="Function"),
        dict(query="lookup", limit=40, repos=["one"]),
    ):
        assert "different query" in error_of(
            cross_repo_search_func, max_results=10, cursor=cursor, **other), other
    assert "start over" in error_of(
        cross_repo_search_func, query="lookup", limit=40, max_results=10, cursor="garbage")


def test_cross_repo_cursor_is_refused_when_the_rows_changed(tmp_path, registry):
    root = indexed_repo(tmp_path / "one", registry, alias="one", count=40)
    first = cross_repo_search_func(query="lookup", limit=100, max_results=10)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "more.py"),
            [NodeInfo(
                kind="Function", name="lookup_late", file_path=str(root / "more.py"),
                line_start=1, line_end=1, language="python",
            )],
            [],
        )
    message = error_of(
        cross_repo_search_func, query="lookup", limit=100, max_results=10,
        cursor=first["next_cursor"],
    )
    assert "graph changed" in message


def test_cross_repo_repos_order_does_not_change_the_query(two_repos):
    first = cross_repo_search_func(
        query="lookup", limit=40, max_results=10, repos=["one", "two"])
    second = cross_repo_search_func(
        query="lookup", limit=40, max_results=10, repos=["two", "one"],
        cursor=first["next_cursor"],
    )
    assert second["status"] == "ok" and second["offset"] == 10


def test_cross_repo_cursor_with_nothing_to_search_is_refused(registry):
    first = cross_repo_search_func(query="lookup")
    assert "next_cursor" not in first
    assert "start over" in error_of(cross_repo_search_func, query="lookup", cursor="garbage")
    cursor = paging.paginate(
        list(range(5)), 2, 10, scope="x", stamp="y",
    ).next_cursor
    assert "start over" in error_of(cross_repo_search_func, query="lookup", cursor=cursor)


# ---------------------------------------------------------------------------
# MCP wrappers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "get_bridge_nodes_tool", "get_knowledge_gaps_tool", "get_surprising_connections_tool",
    "get_community_tool", "get_architecture_overview_tool", "get_flow_tool",
    "cross_repo_search_tool",
])
def test_the_mcp_tools_take_a_cursor_and_document_it(name):
    tool = getattr(main_module, name)
    assert inspect.signature(tool).parameters["cursor"].default is None
    assert "next_cursor" in (tool.__doc__ or "")
