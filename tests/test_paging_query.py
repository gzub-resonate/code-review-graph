"""Cursor paging for the query tools whose caps would otherwise hide rows."""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

import pytest

import code_review_graph.main as main_module
from code_review_graph import paging
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.search import rebuild_fts_index
from code_review_graph.tools import (
    find_large_functions,
    get_impact_radius,
    query_graph,
    semantic_search_nodes,
    traverse_graph_func,
)

COUNT = 60


def _add_node(store, path, name, start=1, end=2, kind="Function"):
    return store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=path, line_start=start, line_end=end,
        language="python",
    ))


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    core = (root / "core.py").as_posix()
    _add_node(store, core, "core_fn", 1, 5)
    for i in range(COUNT):
        # Every caller sits in its own file, so impact scores tie in COUNT-way
        # groups and files tie too.
        path = (root / f"caller_{i:03d}.py").as_posix()
        # Sizes tie in groups of ten, so find_large_functions has many ties.
        _add_node(store, path, f"handler_{i:03d}", 1, 10 + 5 * (i // 10) - 1)
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{path}::handler_{i:03d}", target=f"{core}::core_fn",
            file_path=path, line=3,
        ))
    store.commit()
    rebuild_fts_index(store)
    yield root, store
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def error_of(call, **args) -> str:
    """The message of a refused call, which these tools raise rather than return."""
    with pytest.raises(paging.CursorError) as caught:
        call(**args)
    return str(caught.value)


def read_all(call, rows_of, limit_name, limit, **args):
    """Page through a tool: (all rows, all replies)."""
    rows: list = []
    replies: list = []
    cursor = None
    while True:
        reply = call(**{limit_name: limit}, cursor=cursor, **args)
        assert reply.get("status", "ok") == "ok", reply
        replies.append(reply)
        rows.extend(rows_of(reply))
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, replies
        assert len(replies) < 1000


# ---------------------------------------------------------------------------
# get_impact_radius
# ---------------------------------------------------------------------------


def impact(root):
    return lambda **a: get_impact_radius(
        changed_files=["core.py"], repo_root=str(root), **a,
    )


def test_impact_first_page_is_unchanged_plus_a_cursor(repo):
    root, _ = repo
    call = impact(root)
    first = call(max_results=10)
    assert len(first["impacted_nodes"]) == 10
    assert first["total_impacted"] == COUNT and first["truncated"] is True
    assert first["nodes_omitted"] == COUNT - 10 and "offset" not in first
    assert "Results truncated: showing 10 of 60 impacted nodes" in first["summary"]
    whole = call(max_results=500)
    assert "next_cursor" not in whole and "offset" not in whole
    assert [n["qualified_name"] for n in first["impacted_nodes"]] == [
        n["qualified_name"] for n in whole["impacted_nodes"][:10]
    ]


@pytest.mark.parametrize("limit", [1, 7, 25, 100])
def test_impact_paging_reads_every_node_once_in_order(repo, limit):
    root, _ = repo
    call = impact(root)
    whole = call(max_results=500)
    expected = [n["qualified_name"] for n in whole["impacted_nodes"]]
    assert len(expected) == COUNT == len(set(expected))
    rows, replies = read_all(
        call, lambda r: r["impacted_nodes"], "max_results", limit,
    )
    assert [n["qualified_name"] for n in rows] == expected
    assert all(r["total_impacted"] == COUNT for r in replies)
    assert all(len(r["impacted_nodes"]) <= limit for r in replies)
    assert "next_cursor" not in replies[-1]
    for reply in replies[:-1]:
        assert reply["truncated"] is True and "next_cursor" in reply
    assert [r.get("offset", 0) for r in replies] == [
        sum(len(x["impacted_nodes"]) for x in replies[:i]) for i in range(len(replies))
    ]


def test_impact_paging_goes_past_the_traversal_budget(repo):
    root, _ = repo
    # max_results caps both the traversal and the page, so a budget of 25 used to
    # end at 25 nodes; with paging the rest is still reachable.
    rows, _ = read_all(
        impact(root), lambda r: r["impacted_nodes"], "max_results", 25,
    )
    assert len(rows) == COUNT


def test_impact_ties_are_ordered_by_qualified_name(repo):
    root, _ = repo
    nodes = impact(root)(max_results=500)["impacted_nodes"]
    keys = [(-n["impact_score"], n["qualified_name"]) for n in nodes]
    assert keys == sorted(keys)
    again = impact(root)(max_results=500)["impacted_nodes"]
    assert [n["qualified_name"] for n in again] == [n["qualified_name"] for n in nodes]


def test_impact_compact_pages_the_file_list(repo):
    root, _ = repo
    call = impact(root)
    whole = call(detail_level="compact", max_results=500, max_files=200)
    expected = whole["impacted_files"]
    assert len(expected) == COUNT and "next_cursor" not in whole
    first = call(detail_level="compact", max_results=500, max_files=10)
    assert first["impacted_files"] == expected[:10] and "offset" not in first
    assert first["truncated"] is True and "next_cursor" in first
    assert first["summary"] == "Showing 10 of 60 impacted files (best-first)."
    rows, replies = read_all(
        lambda **a: call(detail_level="compact", max_results=500, **a),
        lambda r: r["impacted_files"], "max_files", 7,
    )
    assert rows == expected
    assert replies[1]["summary"] == "Showing 8-14 of 60 impacted files (best-first)."


def test_impact_compact_cursor_is_tied_to_the_traversal_budget(repo):
    root, _ = repo
    call = impact(root)
    first = call(detail_level="compact", max_results=30, max_files=5)
    assert "different query" in error_of(
        call, detail_level="compact", max_results=40, max_files=5,
        cursor=first["next_cursor"],
    )


def test_impact_cursor_refusals(repo):
    root, store = repo
    call = impact(root)
    first = call(max_results=10)
    cursor = first["next_cursor"]
    assert "start over" in error_of(call, max_results=10, cursor="garbage")
    assert "different query" in error_of(call, max_results=10, max_depth=1, cursor=cursor)
    assert "different query" in error_of(
        call, max_results=10, resolution="direct", cursor=cursor,
    )
    assert "different query" in error_of(
        call, max_results=10, detail_level="compact", cursor=cursor,
    )
    assert "different query" in error_of(
        lambda **a: get_impact_radius(
            changed_files=["caller_000.py"], repo_root=str(root), **a,
        ),
        max_results=10, cursor=cursor,
    )
    _add_node(store, (root / "late.py").as_posix(), "late")
    store.commit()
    assert "graph changed" in error_of(call, max_results=10, cursor=cursor)
    assert "next_cursor" in call(max_results=10)


def test_impact_minimal_refuses_a_cursor(repo):
    root, _ = repo
    cursor = impact(root)(max_results=10)["next_cursor"]
    with pytest.raises(ValueError, match="minimal"):
        impact(root)(detail_level="minimal", cursor=cursor)
    assert "next_cursor" not in impact(root)(detail_level="minimal", max_results=10)


def test_impact_cursor_past_the_end_is_refused(repo):
    root, _ = repo
    call = impact(root)
    first = call(max_results=10)
    # Same query, but the cursor was made for a list that no longer ends there.
    assert "past the end" in error_of(
        lambda **a: call(max_results=10, **a),
        cursor=paging_cursor(first["next_cursor"], offset=COUNT + 5),
    )


def paging_cursor(cursor, **changes):
    """A cursor with its payload edited: opaque, not signed."""
    import base64
    import json

    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload["o"] = changes.get("offset", payload["o"])
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# query_graph
# ---------------------------------------------------------------------------


def query(root, **fixed):
    return lambda **a: query_graph(
        pattern="callers_of", target="core_fn", repo_root=str(root), **fixed, **a,
    )


@pytest.mark.parametrize("limit", [1, 7, 25])
def test_query_graph_paging_reads_every_row_once_in_order(repo, limit):
    root, _ = repo
    call = query(root)
    whole = call(max_results=500)
    expected = [(r["qualified_name"], r["call_site"]["line"]) for r in whole["results"]]
    assert len(expected) == COUNT
    assert "next_cursor" not in whole and "offset" not in whole
    rows, replies = read_all(call, lambda r: r["results"], "max_results", limit)
    assert [(r["qualified_name"], r["call_site"]["line"]) for r in rows] == expected
    assert all(r["result_count"] == COUNT for r in replies)
    assert "next_cursor" not in replies[-1]
    # Each page's edges are the edges of its own rows.
    for reply in replies:
        assert len(reply["edges"]) == len(reply["results"])


def test_query_graph_first_page_keeps_its_summary(repo):
    root, _ = repo
    first = query(root)(max_results=10)
    assert first["results_omitted"] == COUNT - 10 and "offset" not in first
    assert first["summary"].endswith(f"— showing 10, {COUNT - 10} omitted")
    second = query(root)(max_results=10, cursor=first["next_cursor"])
    assert second["offset"] == 10
    assert "— showing 11-20, 50 omitted" in second["summary"]


def test_query_graph_minimal_pages_by_five(repo):
    root, _ = repo
    rows, replies = read_all(
        query(root), lambda r: r["results"], "max_results", 500, detail_level="minimal",
    )
    assert len(rows) == COUNT and all(len(r["results"]) <= 5 for r in replies)


def test_query_graph_other_patterns_page_too(repo):
    root, _ = repo
    call = lambda **a: query_graph(  # noqa: E731
        pattern="file_summary", target="caller_001.py", repo_root=str(root), **a,
    )
    assert call(max_results=1)["result_count"] >= 1
    core_edges = lambda **a: query_graph(  # noqa: E731
        pattern="importers_of", target="core.py", repo_root=str(root), **a,
    )
    assert "next_cursor" not in core_edges(max_results=1)


def test_query_graph_cursor_refusals(repo):
    root, store = repo
    call = query(root)
    cursor = call(max_results=10)["next_cursor"]
    assert "start over" in error_of(call, max_results=10, cursor="nope")
    assert "different query" in error_of(
        lambda **a: query_graph(
            pattern="references_to", target="core_fn", repo_root=str(root), **a,
        ),
        max_results=10, cursor=cursor,
    )
    assert "different query" in error_of(
        lambda **a: query_graph(
            pattern="callers_of", target="handler_001", repo_root=str(root), **a,
        ),
        max_results=10, cursor=cursor,
    )
    assert "different query" in error_of(call, max_results=10, resolution="direct", cursor=cursor)
    _add_node(store, (root / "late.py").as_posix(), "late")
    store.commit()
    assert "graph changed" in error_of(call, max_results=10, cursor=cursor)


def test_query_graph_path_to_has_no_pages(repo):
    root, _ = repo
    with pytest.raises(ValueError, match="no pages"):
        query_graph(
            pattern="path_to", target="handler_001", to="core_fn",
            repo_root=str(root), cursor="x",
        )


# ---------------------------------------------------------------------------
# find_large_functions
# ---------------------------------------------------------------------------


def large(root):
    return lambda **a: find_large_functions(min_lines=5, repo_root=str(root), **a)


@pytest.mark.parametrize("limit", [1, 7, 25])
def test_find_large_functions_pages_every_row_once_in_order(repo, limit):
    root, _ = repo
    whole = large(root)(limit=100)
    expected = [r["qualified_name"] for r in whole["results"]]
    assert len(expected) == len(set(expected)) == whole["total"] > limit
    assert "next_cursor" not in whole and whole["truncated"] is False
    rows, replies = read_all(large(root), lambda r: r["results"], "limit", limit)
    assert [r["qualified_name"] for r in rows] == expected
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    sizes = [r["line_count"] for r in rows]
    assert sizes == sorted(sizes, reverse=True)


def test_find_large_functions_ties_break_by_id(repo):
    root, store = repo
    rows = large(root)(limit=100)["results"]
    by_size: dict[int, list[str]] = {}
    for row in rows:
        by_size.setdefault(row["line_count"], []).append(row["qualified_name"])
    ids = {n.qualified_name: n.id for n in store.get_nodes_by_size(min_lines=1, limit=1000)}
    assert any(len(v) > 1 for v in by_size.values())
    for names in by_size.values():
        assert [ids[n] for n in names] == sorted(ids[n] for n in names)


def test_find_large_functions_first_page_and_ceiling(repo, monkeypatch):
    root, _ = repo
    first = large(root)(limit=10)
    assert first["truncated"] is True and "offset" not in first
    assert first["results_omitted"] == first["total"] - 10
    assert f", showing 10 of {first['total']}" in first["summary"]
    second = large(root)(limit=10, cursor=first["next_cursor"])
    assert second["offset"] == 10
    assert f", showing 11-20 of {first['total']}" in second["summary"]


def test_find_large_functions_cursor_refusals(repo):
    root, store = repo
    cursor = large(root)(limit=10)["next_cursor"]
    assert "start over" in error_of(large(root), limit=10, cursor="bad")
    assert "different query" in error_of(
        lambda **a: find_large_functions(min_lines=6, repo_root=str(root), **a),
        limit=10, cursor=cursor,
    )
    assert "different query" in error_of(large(root), limit=10, kind="Class", cursor=cursor)
    # The page size may change between pages.
    assert large(root)(limit=3, cursor=cursor)["offset"] == 10
    _add_node(store, (root / "late.py").as_posix(), "late", 1, 80)
    store.commit()
    assert "graph changed" in error_of(large(root), limit=10, cursor=cursor)


# ---------------------------------------------------------------------------
# semantic_search_nodes
# ---------------------------------------------------------------------------


def search(root):
    return lambda **a: semantic_search_nodes(query="handler", repo_root=str(root), **a)


def test_search_first_page_is_unchanged_plus_a_cursor(repo):
    root, _ = repo
    first = search(root)(limit=10)
    assert len(first["results"]) == 10 and first["truncated"] is True
    assert first["results_omitted"] == first["total"] - 10 and "offset" not in first
    assert f", showing 10 of {first['total']}" in first["summary"]
    assert "next_cursor" in first


@pytest.mark.parametrize("limit", [5, 10, 20])
def test_search_paging_reads_the_ranked_window_once_in_order(repo, limit):
    root, _ = repo
    call = search(root)
    # The ranking window is three pages, so the whole of it is what one call at
    # that window size ranks.
    wide = call(limit=limit)
    total = wide["total"]
    assert total <= 3 * limit
    rows, replies = read_all(call, lambda r: r["results"], "limit", limit)
    names = [r["qualified_name"] for r in rows]
    assert len(names) == len(set(names)) == total
    assert all(r["total"] == total for r in replies)
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    assert names[:limit] == [r["qualified_name"] for r in wide["results"]]
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_search_ranking_is_the_same_on_every_read(repo):
    root, _ = repo
    reads = [
        [r["qualified_name"] for r in search(root)(limit=20)["results"]] for _ in range(3)
    ]
    assert reads[0] == reads[1] == reads[2]


def test_search_cursor_refusals(repo):
    root, store = repo
    call = search(root)
    cursor = call(limit=10)["next_cursor"]
    assert "start over" in error_of(call, limit=10, cursor="bad")
    # The window sets the ranking, so a different page size is a different query.
    assert "different query" in error_of(call, limit=20, cursor=cursor)
    assert "different query" in error_of(call, limit=10, kind="Function", cursor=cursor)
    assert "different query" in error_of(
        call, limit=10, context_files=["caller_001.py"], cursor=cursor,
    )
    assert "different query" in error_of(
        lambda **a: semantic_search_nodes(query="caller", repo_root=str(root), **a),
        limit=10, cursor=cursor,
    )
    assert "different query" in error_of(call, limit=10, provider="openai", cursor=cursor)
    _add_node(store, (root / "late.py").as_posix(), "late")
    store.commit()
    assert "graph changed" in error_of(call, limit=10, cursor=cursor)


def test_search_cursor_is_refused_when_the_ranking_changes(repo, monkeypatch):
    root, _ = repo
    call = search(root)
    cursor = call(limit=10)["next_cursor"]
    import code_review_graph.tools.query as query_module

    real = query_module.hybrid_search

    def reversed_search(*args, **kwargs):
        return list(reversed(real(*args, **kwargs)))

    monkeypatch.setattr(query_module, "hybrid_search", reversed_search)
    assert "graph changed" in error_of(call, limit=10, cursor=cursor)


# ---------------------------------------------------------------------------
# traverse_graph
# ---------------------------------------------------------------------------


def traverse(root, **fixed):
    return lambda **a: traverse_graph_func(
        query="core_fn", repo_root=str(root), depth=2, **fixed, **a,
    )


def test_traverse_first_page_is_unchanged_plus_a_cursor(repo):
    root, _ = repo
    call = traverse(root)
    whole = call(token_budget=10_000)
    assert whole["truncated"] is False and "next_cursor" not in whole
    assert whole["nodes_visited"] == COUNT + 1
    first = call(token_budget=200)
    assert first["truncated"] is True and "offset" not in first
    assert first["traversal"] == whole["traversal"][: first["nodes_visited"]]
    assert "next_cursor" in first


@pytest.mark.parametrize("mode", ["bfs", "dfs"])
@pytest.mark.parametrize("budget", [150, 400])
def test_traverse_paging_reads_every_node_once_in_order(repo, mode, budget):
    root, _ = repo
    call = traverse(root, mode=mode)
    whole = call(token_budget=10_000)
    expected = [n["qualified_name"] for n in whole["traversal"]]
    rows, replies = read_all(call, lambda r: r["traversal"], "token_budget", budget)
    assert [n["qualified_name"] for n in rows] == expected
    assert len(replies) > 1
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    assert "next_cursor" not in replies[-1]
    assert [r.get("offset", 0) for r in replies] == [
        sum(r["nodes_visited"] for r in replies[:i]) for i in range(len(replies))
    ]


def test_traverse_a_budget_too_small_for_one_node_has_no_cursor(repo):
    root, _ = repo
    reply = traverse(root)(token_budget=1)
    assert reply["traversal"] == [] and reply["truncated"] is True
    assert "next_cursor" not in reply


def test_traverse_cursor_refusals(repo):
    root, store = repo
    call = traverse(root)
    cursor = call(token_budget=200)["next_cursor"]
    assert "start over" in error_of(call, token_budget=200, cursor="bad")
    assert "different query" in error_of(call, token_budget=200, mode="dfs", cursor=cursor)
    assert "different query" in error_of(
        lambda **a: traverse_graph_func(
            query="core_fn", repo_root=str(root), depth=1, **a,
        ),
        token_budget=200, cursor=cursor,
    )
    assert "different query" in error_of(
        lambda **a: traverse_graph_func(
            query="handler_001", repo_root=str(root), depth=2, **a,
        ),
        token_budget=200, cursor=cursor,
    )
    assert "past the end" in error_of(
        call, token_budget=200, cursor=paging_cursor(cursor, offset=COUNT + 50),
    )
    # The page size may change between pages.
    assert traverse(root)(token_budget=400, cursor=cursor)["offset"] > 0
    _add_node(store, (root / "late.py").as_posix(), "late")
    store.commit()
    assert "graph changed" in error_of(call, token_budget=200, cursor=cursor)


# ---------------------------------------------------------------------------
# The MCP wrappers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "get_impact_radius_tool", "query_graph_tool", "semantic_search_nodes_tool",
    "find_large_functions_tool", "traverse_graph_tool",
])
def test_the_mcp_tools_take_a_cursor(name):
    parameter = inspect.signature(getattr(main_module, name)).parameters["cursor"]
    assert parameter.default is None
    assert "next_cursor" in (getattr(main_module, name).__doc__ or "")


# ---------------------------------------------------------------------------
# find_large_functions: default ranking
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    big = (root / "src" / "big.py").as_posix()
    _add_node(store, big, big, 1, 300, kind="File")
    _add_node(store, big, "Big", 1, 299, kind="Class")
    _add_node(store, big, "run", 10, 130)
    test_py = (root / "tests" / "test_big.py").as_posix()
    _add_node(store, test_py, test_py, 1, 900, kind="File")
    _add_node(store, test_py, "helper", 1, 500)
    sql = (root / "db" / "V1__baseline.sql").as_posix()
    _add_node(store, sql, "baseline_fn", 1, 700)
    story = (root / "ui" / "a.stories.tsx").as_posix()
    _add_node(store, story, "Story", 1, 400)
    store.commit()
    yield root
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_find_large_functions_defaults_to_production_functions(mixed_repo):
    reply = find_large_functions(min_lines=50, repo_root=str(mixed_repo))
    assert [(r["name"], r["kind"]) for r in reply["results"]] == [("run", "Function")]
    assert reply["include_tests"] is False


def test_find_large_functions_include_tests_adds_test_and_generated_files(mixed_repo):
    reply = find_large_functions(min_lines=50, repo_root=str(mixed_repo), include_tests=True)
    assert [r["name"] for r in reply["results"]] == ["baseline_fn", "helper", "Story", "run"]


def test_find_large_functions_kind_overrides_default(mixed_repo):
    reply = find_large_functions(min_lines=50, kind="Class", repo_root=str(mixed_repo))
    assert [r["name"] for r in reply["results"]] == ["Big"]
    reply = find_large_functions(min_lines=50, kind="File", repo_root=str(mixed_repo))
    assert [r["kind"] for r in reply["results"]] == ["File"]


def test_find_large_functions_drops_class_spanning_its_file(mixed_repo):
    reply = find_large_functions(
        min_lines=50, kind="File,Class,Function", repo_root=str(mixed_repo),
    )
    assert [(r["name"][-6:], r["kind"]) for r in reply["results"]] == [
        ("big.py", "File"), ("run", "Function"),
    ]


def test_find_large_functions_minimal_does_not_repeat_hits_in_summary(mixed_repo):
    reply = find_large_functions(
        min_lines=50, repo_root=str(mixed_repo), detail_level="minimal",
    )
    assert "run" not in reply["summary"]
    assert [r["name"] for r in reply["results"]] == ["run"]
