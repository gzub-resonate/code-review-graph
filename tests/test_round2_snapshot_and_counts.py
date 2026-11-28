"""Whole-graph read tools run on an in-memory copy and keep their untruncated result per graph
state, and the cursor stamp comes from cached row counts. None of it may change an answer.

``old`` below is the original behaviour: the same read on the file, no cache, and the stamp
counted by ``paging.graph_stamp``. Every case compares the new path with it, counts how often
the read actually ran (an output-only test cannot tell a cache whose key never matches), and
checks that an edge insert and every row-changing argument reach the key.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from code_review_graph import analysis_snapshot, graph_counts, paging
from code_review_graph.analysis_snapshot import RESULTS, cached_on_snapshot, file_state
from code_review_graph.communities import detect_communities, store_communities
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo, normalize_file_path
from code_review_graph.tools import (
    analysis_tools,
    community_tools,
    context,
    find_large_functions,
    flows_tools,
    get_architecture_overview_func,
    get_hub_nodes_func,
    get_impact_radius,
    get_minimal_context,
    get_surprising_connections_func,
    list_communities_func,
    list_flows,
    list_graph_stats,
    query,
)

MODULES = (analysis_tools, community_tools, flows_tools, query)


@pytest.fixture(autouse=True)
def _fresh():
    RESULTS.clear()
    graph_counts.clear()
    yield
    RESULTS.clear()
    graph_counts.clear()


def _fn(root, name, file, lines=3, kind="Function"):
    path = normalize_file_path(root / file)
    return path, NodeInfo(kind, name, path, 1, lines, "python")


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        qns = []
        for g in range(4):
            for i in range(8):
                file, node = _fn(tmp_path, f"f{g}_{i}", f"pkg{g}/m{i}.py", lines=40 + 20 * i)
                s.upsert_node(node)
                qns.append((g, file, f"{file}::f{g}_{i}"))
            fpath = normalize_file_path(tmp_path / f"pkg{g}/m0.py")
            s.upsert_node(NodeInfo("File", f"pkg{g}/m0.py", fpath, 1, 200, "python"))
        for g, file, qn in qns:
            # a chain inside each package, plus hub-ward edges to f0_0 and one cross edge
            peers = [q for gg, _f, q in qns if gg == g]
            idx = peers.index(qn)
            if idx + 1 < len(peers):
                s.upsert_edge(EdgeInfo("CALLS", qn, peers[idx + 1], file, 5))
            if g and idx == 0:
                s.upsert_edge(EdgeInfo("CALLS", qn, qns[0][2], file, 6))
            s.upsert_edge(EdgeInfo("CALLS", qn, qns[0][2], file, 7))
        s.set_metadata("last_updated", "2026-01-01T00:00:00")
        s.commit()
        store_communities(s, detect_communities(s))
        store_flows(s, trace_flows(s))
        s.commit()
    return tmp_path


def _add_edge(root, n=1):
    with GraphStore(get_db_path(root)) as s:
        for i in range(n):
            src = normalize_file_path(root / f"pkg3/m{i + 1}.py")
            tgt = normalize_file_path(root / "pkg1/m5.py")
            s.upsert_edge(EdgeInfo("CALLS", f"{src}::f3_{i + 1}", f"{tgt}::f1_5", src, 11))
        s.commit()


@pytest.fixture
def old_path(monkeypatch):
    """The behaviour before this change, for comparing answers."""
    for mod in MODULES:
        monkeypatch.setattr(
            mod, "cached_on_snapshot", lambda store, name, args, compute: compute(),
        )
        monkeypatch.setattr(mod, "graph_stamp", paging.graph_stamp)


@pytest.fixture
def runs(monkeypatch):
    """Count the reads that really ran, and whether each ran on an in-memory copy."""
    seen: list[tuple[str, bool]] = []

    def wrapper(store, name, args, compute):
        def counted():
            on_file = store._conn.execute("PRAGMA database_list").fetchone()[2]
            seen.append((name, not on_file))
            return compute()

        return cached_on_snapshot(store, name, args, counted)

    for mod in MODULES:
        monkeypatch.setattr(mod, "cached_on_snapshot", wrapper)
    return seen


def _new_and_old(ask, request):
    new = ask()
    request.getfixturevalue("old_path")
    old = ask()
    return new, old


def _strip_hints(result):
    """``_hints`` come from a per-process session, not the graph."""
    return {k: v for k, v in result.items() if k != "_hints"}


# (name, ask(root, **kw), row-changing variants, extra kwargs for a paged call)
def _cases(root):
    r = str(root)
    return [
        ("hub_nodes", lambda **kw: get_hub_nodes_func(repo_root=r, **kw),
         [{"include_tests": True}, {"rank_by": "total"}, {"exclude_leaf_primitives": True},
          {"include_generated": True}, {"exclude_data_types": True}],
         {"top_n": 3}),
        ("surprising_connections",
         lambda **kw: get_surprising_connections_func(repo_root=r, **kw),
         [{"include_tests": True}, {"include_generated": True}], {"top_n": 3}),
        ("list_communities", lambda **kw: list_communities_func(repo_root=r, **kw),
         [{"sort_by": "name"}, {"min_size": 3}, {"include_config": True}],
         {"max_results": 2}),
        ("architecture_overview",
         lambda **kw: get_architecture_overview_func(repo_root=r, **kw),
         [{"include_config": True}], {"max_results": 1, "detail_level": "standard"}),
        ("list_flows", lambda **kw: list_flows(repo_root=r, **kw),
         [{"sort_by": "name"}, {"kind": "Function"}, {"entry_kind": "other"},
          {"group_duplicates": False}], {"limit": 2}),
        ("find_large_functions", lambda **kw: find_large_functions(repo_root=r, **kw),
         [{"min_lines": 80}, {"kind": "File"}, {"file_path_pattern": "pkg1"},
          {"include_tests": True}], {"limit": 3, "min_lines": 40}),
    ]


NAMES = ["hub_nodes", "surprising_connections", "list_communities", "architecture_overview",
         "list_flows", "find_large_functions"]


def _case(root, name):
    return next(c for c in _cases(root) if c[0] == name)


@pytest.mark.parametrize("name", NAMES)
def test_new_path_equals_old_path_at_every_detail_level(root, request, name):
    _n, ask, _variants, _paged = _case(root, name)
    for detail in ("standard", "minimal"):
        new, old = _new_and_old(lambda: _strip_hints(ask(detail_level=detail)), request)
        assert new == old, (name, detail)
        RESULTS.clear()
        request.getfixturevalue("monkeypatch").undo()
    # the compared answers are real ones, not two errors
    assert ask().get("status", "ok") == "ok"


@pytest.mark.parametrize("name", NAMES)
def test_a_repeat_call_computes_once_on_a_memory_copy(root, runs, name):
    _n, ask, _variants, _paged = _case(root, name)
    first = ask()
    second = ask()
    assert _strip_hints(first) == _strip_hints(second)
    assert [n for n, _ in runs] == [name]
    assert [mem for _, mem in runs] == [True]


@pytest.mark.parametrize("name", NAMES)
def test_every_row_changing_argument_is_in_the_key(root, runs, name):
    _n, ask, variants, _paged = _case(root, name)
    ask()
    expected = 1
    assert len(runs) == expected
    for variant in variants:
        ask(**variant)
        expected += 1
        assert len(runs) == expected, (name, variant)
    # all of them again: every one is a hit now
    ask()
    for variant in variants:
        ask(**variant)
    assert len(runs) == expected


@pytest.mark.parametrize("name", NAMES)
def test_an_edge_insert_invalidates_the_cache(root, runs, request, name):
    _n, ask, _variants, _paged = _case(root, name)
    ask()
    ask()
    assert len(runs) == 1
    _add_edge(root, 3)
    after = _strip_hints(ask())
    assert len(runs) == 2
    # and the recomputed answer is the one the file gives
    request.getfixturevalue("old_path")
    assert after == _strip_hints(ask())


@pytest.mark.parametrize("name", NAMES)
def test_a_cursor_from_page_one_works_on_page_two(root, runs, request, name):
    _n, ask, _variants, paged = _case(root, name)
    first = ask(**paged)
    cursor = first.get("next_cursor")
    assert cursor, f"{name}: fixture too small to page"
    second = ask(cursor=cursor, **paged)
    assert second.get("status", "ok") == "ok", second
    assert len(runs) == 1  # page two read the cache
    request.getfixturevalue("old_path")
    # a cursor made on the new path is accepted by the old one, and gives the same page
    assert _strip_hints(ask(cursor=cursor, **paged)) == _strip_hints(second)


def test_hubs_after_an_edit_refuse_the_old_cursor(root):
    ask = _case(root, "hub_nodes")[1]
    cursor = ask(top_n=3)["next_cursor"]
    _add_edge(root)
    with pytest.raises(paging.CursorError):
        ask(top_n=3, cursor=cursor)


# -- impact radius -----------------------------------------------------------------------


def _impact(root, **kw):
    kw.setdefault("changed_files", ["pkg0/m0.py"])
    return get_impact_radius(repo_root=str(root), **kw)


@pytest.mark.parametrize("detail", ["standard", "minimal", "compact"])
def test_impact_radius_new_equals_old(root, request, detail):
    new, old = _new_and_old(lambda: _impact(root, detail_level=detail, max_depth=3), request)
    assert new == old
    assert new["status"] == "ok"


def test_impact_radius_repeat_computes_once_and_keys_cover_the_arguments(root, runs):
    _impact(root, max_depth=3)
    _impact(root, max_depth=3)
    assert [(n, m) for n, m in runs] == [("impact_radius", True)]
    for variant in ({"max_depth": 1}, {"resolution": "direct"},
                    {"changed_files": ["pkg1/m0.py"]}, {"max_results": 2}):
        before = len(runs)
        _impact(root, **{"max_depth": 3, **variant})
        assert len(runs) == before + 1, variant
    before = len(runs)
    # the detail level only changes how the one traversal is shown
    _impact(root, max_depth=3, detail_level="minimal")
    _impact(root, max_depth=3, detail_level="compact")
    assert len(runs) == before


def test_impact_radius_edge_insert_invalidates(root, runs, request):
    _impact(root, max_depth=3)
    _add_edge(root, 2)
    after = _impact(root, max_depth=3)
    assert len(runs) == 2
    request.getfixturevalue("old_path")
    assert after == _impact(root, max_depth=3)


def test_impact_radius_cursor_from_page_one_works_on_page_two(root):
    first = _impact(root, max_depth=3, max_results=2)
    assert first["next_cursor"]
    second = _impact(root, max_depth=3, max_results=2, cursor=first["next_cursor"])
    assert second["status"] == "ok" and second["offset"] == 2
    assert second["impacted_nodes"] != first["impacted_nodes"]


# -- list_graph_stats and get_minimal_context --------------------------------------------


def _warm_embedding_table(root, runs):
    # The first call creates the (empty) embeddings table, which is a write to the file; a
    # real graph has it already.
    list_graph_stats(repo_root=str(root))
    RESULTS.clear()
    runs.clear()


def test_graph_stats_new_equals_old_and_computes_once(root, runs, request):
    _warm_embedding_table(root, runs)
    new = list_graph_stats(repo_root=str(root))
    list_graph_stats(repo_root=str(root))
    assert runs == [("graph_stats", True)]
    request.getfixturevalue("old_path")
    assert list_graph_stats(repo_root=str(root)) == new
    assert new["total_edges"] > 0 and new["files_count"] == 4


def test_graph_stats_edge_insert_invalidates(root, runs):
    _warm_embedding_table(root, runs)
    before = list_graph_stats(repo_root=str(root))
    _add_edge(root, 2)
    after = list_graph_stats(repo_root=str(root))
    assert len(runs) == 2
    assert after["total_edges"] == before["total_edges"] + 2


def _minimal(root):
    return get_minimal_context(repo_root=str(root), changed_files=[], skip_discovery=True)


def test_minimal_context_new_equals_old_and_reads_the_counts_once(root, request):
    new = _minimal(root)
    assert "nodes," in new["summary"] and "edges across 4 files" in new["summary"]
    request.getfixturevalue("old_path")
    # the old read was store.get_stats(); the summary must say the same numbers
    with GraphStore(get_db_path(root)) as s:
        st = s.get_stats()
    assert new["summary"].startswith(
        f"{st.total_nodes} nodes, {st.total_edges} edges across {st.files_count} files."
    )
    assert context.count_nodes is graph_counts.count_nodes


def test_minimal_context_counts_follow_an_edge_insert(root):
    before = _minimal(root)["summary"]
    _add_edge(root, 2)
    assert _minimal(root)["summary"] != before


# -- counts and the stamp ----------------------------------------------------------------


class _Counting:
    """Count the COUNT(*) statements a store runs."""

    def __init__(self, store):
        self.seen: list[str] = []
        store._conn.set_trace_callback(
            lambda sql: self.seen.append(sql) if "COUNT(*)" in sql else None
        )

    def of(self, table):
        return [s for s in self.seen if f"FROM {table}" in s]


def test_graph_stamp_is_the_paging_digest(root):
    with GraphStore(get_db_path(root)) as s:
        for extra in ((), ("x",), ("x", [1, 2], {"a": 1})):
            assert graph_counts.graph_stamp(s, *extra) == paging.graph_stamp(s, *extra)


def test_counts_are_not_rescanned_on_an_unchanged_graph(root):
    with GraphStore(get_db_path(root)) as first:
        c1 = _Counting(first)
        stamp = graph_counts.graph_stamp(first)
        assert len(c1.of("edges")) == 1 and len(c1.of("nodes")) == 1
    with GraphStore(get_db_path(root)) as second:
        c2 = _Counting(second)
        assert graph_counts.graph_stamp(second) == stamp
        assert graph_counts.graph_stamp(second, "x") != stamp
        assert c2.of("edges") == [] and c2.of("nodes") == []


def test_counts_rescan_after_an_edge_insert_and_the_stamp_moves(root):
    with GraphStore(get_db_path(root)) as s:
        before = graph_counts.graph_stamp(s)
    _add_edge(root)
    with GraphStore(get_db_path(root)) as s:
        counting = _Counting(s)
        after = graph_counts.graph_stamp(s)
        assert len(counting.of("edges")) == 1
        assert after != before and after == paging.graph_stamp(s)


def test_counts_are_not_cached_inside_an_open_transaction(root):
    with GraphStore(get_db_path(root)) as s:
        graph_counts.graph_stamp(s)
        s._conn.execute("BEGIN IMMEDIATE")
        s._conn.execute("DELETE FROM edges")
        assert graph_counts.count_edges(s) == 0
        s._conn.execute("ROLLBACK")
        assert graph_counts.count_edges(s) > 0


def test_counts_of_an_in_memory_database_are_never_cached():
    with GraphStore(":memory:") as s:
        assert graph_counts.count_nodes(s) == 0
        s.upsert_node(NodeInfo("Function", "a", "/x/a.py", 1, 2, "python"))
        s.commit()
        assert graph_counts.count_nodes(s) == 1


def test_a_snapshot_fills_the_counts_for_the_file_state(root):
    with GraphStore(get_db_path(root)) as s:
        with analysis_snapshot.in_memory_snapshot(s) as used:
            assert used
            stamp = graph_counts.graph_stamp(s)
    with GraphStore(get_db_path(root)) as s:
        counting = _Counting(s)
        assert graph_counts.graph_stamp(s) == stamp
        assert counting.of("edges") == []


# -- snapshot helper and file state ------------------------------------------------------


def test_cached_on_snapshot_restores_the_connection_even_when_compute_fails(root):
    with GraphStore(get_db_path(root)) as s:
        file_conn = s._conn
        seen = []

        def ok():
            seen.append(s._conn)
            return {"rows": [1]}

        assert cached_on_snapshot(s, "t", (1,), ok) == {"rows": [1]}
        assert seen[0] is not file_conn and s._conn is file_conn

        def boom():
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            cached_on_snapshot(s, "t", (2,), boom)
        assert s._conn is file_conn
        assert s.get_all_nodes()


def test_cached_on_snapshot_does_not_confuse_none_with_a_string_that_looks_like_it(root):
    # An argument left at ``None`` and a caller's own text must never share a cache entry.
    with GraphStore(get_db_path(root)) as s:
        RESULTS.clear()
        calls = []

        def compute():
            calls.append(1)
            return {"n": len(calls)}

        for value in (None, "<none>", "none", ("none",), None):
            cached_on_snapshot(s, "t_none_key", (value,), compute)
        # None twice is one computation; the three other spellings are one each.
        assert len(calls) == 4


def test_cached_on_snapshot_hands_out_copies_and_keys_on_args(root):
    with GraphStore(get_db_path(root)) as s:
        calls = []

        def compute():
            calls.append(1)
            return {"rows": [1, 2]}

        got = cached_on_snapshot(s, "t", ("a",), compute)
        got["rows"].append(3)
        assert cached_on_snapshot(s, "t", ("a",), compute) == {"rows": [1, 2]}
        cached_on_snapshot(s, "t", ("b",), compute)
        cached_on_snapshot(s, "u", ("a",), compute)
        assert len(calls) == 3


def test_an_empty_wal_does_not_change_the_file_state(root):
    db = get_db_path(root)
    states = set()
    for _ in range(3):
        store = GraphStore(db)
        states.add(file_state(store.db_path))
        store.close()
    assert len(states) == 1
    wal = db.with_name(db.name + "-wal")
    wal.write_bytes(b"")
    assert file_state(db) in states
    wal.unlink(missing_ok=True)


def test_nothing_the_tools_read_is_written(root):
    """The reads run on a copy that is thrown away: a write would be lost, so there must be none."""
    r = str(root)
    statements: list[str] = []
    real = sqlite3.connect

    def tracing(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(
            lambda sql: statements.append(sql)
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
            else None
        )
        return conn

    import code_review_graph.graph as graph_module

    original = graph_module.sqlite3.connect
    graph_module.sqlite3.connect = tracing  # type: ignore[assignment]
    try:
        get_hub_nodes_func(repo_root=r)
        get_surprising_connections_func(repo_root=r)
        list_communities_func(repo_root=r)
        get_architecture_overview_func(repo_root=r)
        list_flows(repo_root=r)
        find_large_functions(repo_root=r, min_lines=1)
        list_graph_stats(repo_root=r)
    finally:
        graph_module.sqlite3.connect = original  # type: ignore[assignment]
    writes = [s for s in statements if "_impact_" not in s]
    assert writes == [], json.dumps(writes[:5])
