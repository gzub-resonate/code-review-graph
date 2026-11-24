"""The ranking tools leave test code out unless ``include_tests`` is set."""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

import pytest

import code_review_graph.main as main_module
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import (
    get_bridge_nodes_func,
    get_hub_nodes_func,
    get_knowledge_gaps_func,
    get_suggested_questions_func,
    get_surprising_connections_func,
)

PROD = 12
TESTS = 18


def _node(kind, name, path, i, *, is_test=False, lang="java"):
    return NodeInfo(
        kind=kind, name=name, file_path=path, line_start=i * 3 + 1, line_end=i * 3 + 2,
        language=lang, is_test=is_test,
    )


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    prod = (root / "src" / "main" / "Svc.java").as_posix()
    tfile = (root / "src" / "test" / "SvcTest.java").as_posix()
    cfg = (root / ".claude" / "settings.json").as_posix()
    for i in range(PROD):
        store.upsert_node(_node("Function", f"p{i:02d}", prod, i))
    # Test nodes: typed Test, flagged is_test, or plain functions in a test file.
    for i in range(TESTS):
        kind = "Test" if i % 3 == 0 else "Function"
        store.upsert_node(_node(kind, f"testCase{i:02d}", tfile, i, is_test=(i % 3 == 1)))
    for i in range(4):
        store.upsert_node(_node("JsonPath", f"perm{i}", cfg, i, lang="json"))
    for i in range(1, PROD):
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{prod}::p{i:02d}", target=f"{prod}::p00",
            file_path=prod, line=i,
        ))
    for i in range(TESTS):
        for j in range(PROD):
            if (i + j) % 2 == 0:
                store.upsert_edge(EdgeInfo(
                    kind="CALLS", source=f"{tfile}::testCase{i:02d}",
                    target=f"{prod}::p{j:02d}", file_path=tfile, line=i,
                ))
    # Peripheral production callers of the hub, so there are production surprises.
    for i in range(3):
        store.upsert_node(_node("Function", f"q{i}", prod, 40 + i))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{prod}::q{i}", target=f"{prod}::p00",
            file_path=prod, line=i,
        ))
    # One test sits between production callers and callees, so it is a bridge.
    for i in range(1, 6):
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{prod}::p{i:02d}", target=f"{tfile}::testCase00",
            file_path=prod, line=i,
        ))
    store.commit()
    yield root, store
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _is_test_row(row):
    return "testCase" in row["qualified_name"]


@pytest.mark.parametrize("func,key", [
    (get_hub_nodes_func, "hub_nodes"), (get_bridge_nodes_func, "bridge_nodes"),
])
def test_tests_are_excluded_by_default_and_restored_by_the_flag(repo, func, key):
    root, _ = repo
    default = func(repo_root=str(root), top_n=100)
    rows = default[key]
    assert rows and not any(_is_test_row(r) for r in rows)
    assert default["total"] == len(rows)
    with_tests = func(repo_root=str(root), top_n=100, include_tests=True)
    assert any(_is_test_row(r) for r in with_tests[key])
    assert with_tests["total"] > default["total"]


def test_hub_total_and_paging_describe_the_filtered_list(repo):
    root, _ = repo
    whole = get_hub_nodes_func(repo_root=str(root), top_n=100)["hub_nodes"]
    assert len(whole) == PROD + 3
    seen, cursor = [], None
    while True:
        reply = get_hub_nodes_func(repo_root=str(root), top_n=5, cursor=cursor)
        assert reply["total"] == PROD + 3
        seen.extend(r["qualified_name"] for r in reply["hub_nodes"])
        cursor = reply.get("next_cursor")
        assert reply["truncated"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == [r["qualified_name"] for r in whole]


def test_surprising_connections_drop_an_edge_with_a_test_end(repo):
    root, _ = repo
    default = get_surprising_connections_func(repo_root=str(root), top_n=100)
    for row in default["surprising_connections"]:
        assert "testCase" not in row["source_qualified"]
        assert "testCase" not in row["target_qualified"]
    with_tests = get_surprising_connections_func(
        repo_root=str(root), top_n=100, include_tests=True,
    )
    assert with_tests["total"] > default["total"]
    assert any("testCase" in r["source_qualified"] for r in with_tests["surprising_connections"])


def test_suggested_questions_ignore_tests_by_default(repo):
    root, _ = repo
    default = get_suggested_questions_func(repo_root=str(root))
    assert not any("testCase" in q["target"] for q in default["questions"])
    with_tests = get_suggested_questions_func(repo_root=str(root), include_tests=True)
    assert any("testCase" in q["target"] for q in with_tests["questions"])


def test_knowledge_gaps_exclude_tests_and_totals_follow(repo):
    root, _ = repo
    default = get_knowledge_gaps_func(repo_root=str(root), max_per_category=50)
    for category in ("isolated_nodes", "untested_hotspots"):
        rows = default["gaps"][category]
        assert not any("testCase" in r["qualified_name"] for r in rows), category
        assert default["summary"][category] == len(rows)
    assert default["total_gaps"] == sum(default["summary"].values())


def test_config_document_nodes_are_never_isolated(repo):
    root, _ = repo
    for include_tests in (False, True):
        reply = get_knowledge_gaps_func(
            repo_root=str(root), max_per_category=50, include_tests=include_tests,
        )
        isolated = reply["gaps"]["isolated_nodes"]
        assert not any(r["kind"] in {"JsonPath", "YamlPath"} for r in isolated)
        assert reply["summary"]["isolated_nodes"] == len(isolated)


def test_test_only_isolated_nodes_return_with_include_tests(repo):
    root, store = repo
    tfile = (root / "src" / "test" / "SvcTest.java").as_posix()
    store.upsert_node(_node("Test", "testLonely", tfile, 99))
    store.commit()
    default = get_knowledge_gaps_func(repo_root=str(root), max_per_category=50)
    with_tests = get_knowledge_gaps_func(
        repo_root=str(root), max_per_category=50, include_tests=True,
    )
    assert not any(r["name"] == "testLonely" for r in default["gaps"]["isolated_nodes"])
    assert any(r["name"] == "testLonely" for r in with_tests["gaps"]["isolated_nodes"])


@pytest.mark.parametrize("func", [
    get_hub_nodes_func, get_bridge_nodes_func, get_surprising_connections_func,
    get_knowledge_gaps_func,
])
def test_cursor_is_refused_across_include_tests(repo, func):
    root, _ = repo
    arg = {"max_per_category": 1} if func is get_knowledge_gaps_func else {"top_n": 1}
    for first, second in ((False, True), (True, False)):
        reply = func(repo_root=str(root), include_tests=first, **arg)
        cursor = reply.get("next_cursor")
        assert cursor
        ok = func(repo_root=str(root), include_tests=first, cursor=cursor, **arg)
        assert ok["status"] == "ok"
        refused = None
        try:
            refused = func(repo_root=str(root), include_tests=second, cursor=cursor, **arg)
        except ValueError as exc:
            refused = {"status": "error", "error": str(exc)}
        assert refused["status"] != "ok"


@pytest.mark.parametrize("name", [
    "get_hub_nodes_tool", "get_bridge_nodes_tool", "get_surprising_connections_tool",
    "get_suggested_questions_tool", "get_knowledge_gaps_tool",
])
def test_mcp_tools_default_include_tests_false(name):
    tool = getattr(main_module, name)
    fn = getattr(tool, "fn", tool)
    assert inspect.signature(fn).parameters["include_tests"].default is False
    assert "include_tests" in (fn.__doc__ or "")
