"""``get_knowledge_gaps_tool`` runs on an in-memory copy of the graph and keeps its untruncated
result per graph state, so cursor pages stop recomputing it. Neither may change an answer."""

from __future__ import annotations

import pytest

from code_review_graph import analysis_snapshot
from code_review_graph.analysis import find_knowledge_gaps
from code_review_graph.analysis_snapshot import RESULTS, ResultCache, in_memory_snapshot
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo, normalize_file_path
from code_review_graph.tools import get_knowledge_gaps_func


@pytest.fixture(autouse=True)
def _fresh_cache():
    RESULTS.clear()
    yield
    RESULTS.clear()


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        for i in range(12):
            file = normalize_file_path(tmp_path / f"src/main/Hot{i}.java")
            s.upsert_node(NodeInfo("Class", f"Hot{i}", file, 1, 50, "java"))
            qn = f"{file}::Hot{i}"
            for j in range(6):
                caller = normalize_file_path(tmp_path / f"src/main/Use{i}x{j}.java")
                s.upsert_node(NodeInfo("Function", f"use{i}x{j}", caller, 1, 3, "java"))
                s.upsert_edge(EdgeInfo("CALLS", f"{caller}::use{i}x{j}", qn, caller, 3))
        for i in range(8):
            file = normalize_file_path(tmp_path / f"src/main/Lonely{i}.java")
            s.upsert_node(NodeInfo("Function", f"lonely{i}", file, 1, 3, "java"))
        s.commit()
    return tmp_path


def _ask(root, **kw):
    return get_knowledge_gaps_func(repo_root=str(root), **kw)


def test_snapshot_gives_the_same_lists_as_the_file(root):
    with GraphStore(get_db_path(root)) as store:
        on_file = find_knowledge_gaps(store, repo_root=root, max_isolated=None, max_untested=None)
        with in_memory_snapshot(store) as used:
            assert used is True
            in_memory = find_knowledge_gaps(
                store, repo_root=root, max_isolated=None, max_untested=None,
            )
        assert in_memory == on_file
        assert on_file["isolated_nodes"] and on_file["untested_hotspots"]


def test_store_gets_its_file_connection_back(root):
    with GraphStore(get_db_path(root)) as store:
        file_conn = store._conn
        with in_memory_snapshot(store):
            assert store._conn is not file_conn
        assert store._conn is file_conn
        assert store.get_all_nodes()


def test_oversized_graph_stays_on_the_file(root, monkeypatch):
    monkeypatch.setattr(analysis_snapshot, "_MAX_MB", 0)
    with GraphStore(get_db_path(root)) as store:
        file_conn = store._conn
        with in_memory_snapshot(store) as used:
            assert used is False
            assert store._conn is file_conn


def test_pages_equal_whether_cached_or_computed_fresh(root):
    first = _ask(root, max_per_category=3)
    assert first["truncated"] and first["next_cursor"]
    second_cached = _ask(root, max_per_category=3, cursor=first["next_cursor"])
    RESULTS.clear()
    second_fresh = _ask(root, max_per_category=3, cursor=first["next_cursor"])
    assert second_cached == second_fresh
    assert _ask(root, max_per_category=3) == first


def test_edge_insert_invalidates_the_cache(root):
    before = _ask(root, max_per_category=50)
    with GraphStore(get_db_path(root)) as s:
        lonely = normalize_file_path(root / "src/main/Lonely0.java")
        for i in range(2):
            caller = normalize_file_path(root / f"src/main/Use0x{i}.java")
            s.upsert_edge(EdgeInfo("CALLS", f"{caller}::use0x{i}", f"{lonely}::lonely0", caller, 3))
        s.commit()
    after = _ask(root, max_per_category=50)
    assert after != before

    def names():
        found, cursor = set(), None
        while True:
            page = _ask(root, max_per_category=50, cursor=cursor)
            found |= {row["name"] for row in page["gaps"]["isolated_nodes"]}
            cursor = page.get("next_cursor")
            if not cursor:
                return found

    assert "lonely0" not in names()
    assert before["summary"]["isolated_nodes"] > after["summary"]["isolated_nodes"]


def test_flags_are_part_of_the_key(root):
    default = _ask(root, max_per_category=50)
    with_tests = _ask(root, max_per_category=50, include_tests=True)
    assert _ask(root, max_per_category=50) == default
    assert _ask(root, max_per_category=50, include_tests=True) == with_tests


def test_result_cache_hands_out_copies():
    cache = ResultCache(max_entries=2)
    calls = []

    def compute():
        calls.append(1)
        return {"rows": [1, 2]}

    got, hit = cache.get_or_compute(("k", 1), compute)
    assert hit is False
    got["rows"].append(3)
    again, hit = cache.get_or_compute(("k", 1), compute)
    assert hit is True and again == {"rows": [1, 2]} and len(calls) == 1


def test_result_cache_skips_keys_with_no_file_state():
    cache = ResultCache()
    calls = []
    for _ in range(2):
        cache.get_or_compute(("k", None), lambda: calls.append(1))
    assert len(calls) == 2


def test_result_cache_is_bounded():
    cache = ResultCache(max_entries=2)
    for i in range(3):
        cache.get_or_compute(("k", i), lambda: i)
    calls = []
    cache.get_or_compute(("k", 0), lambda: calls.append(1))
    assert calls == [1]
