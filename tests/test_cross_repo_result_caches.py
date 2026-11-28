"""Whole-call result caches of ``origin="consumers"`` and ``cross_repo_search_tool``.

These tests count how many times the expensive step runs across identical calls: a test that
only compares the replies cannot tell a cached call from one that recomputes every time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import consumer_join as cj
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools import registry_tools as rt
from code_review_graph.tools.registry_tools import (
    cross_repo_requests_func,
    cross_repo_search_func,
)

from .test_external_and_consumers import world as world  # noqa: F401
from .test_service_aliases_infra_join import registry as registry  # noqa: F401


def _count(monkeypatch, owner, name: str) -> dict[str, int]:
    counts = {"n": 0}
    original = getattr(owner, name)

    def counting(*args, **kwargs):
        counts["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counting)
    return counts


def _add_file(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.write_text(text, encoding="utf-8")
    file_nodes, edges = CodeParser(root).parse_file(path)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.store_file_nodes_edges(str(path), file_nodes, edges)


class TestConsumersRepeat:
    def test_per_repo_reads_run_once_across_identical_calls(self, world, monkeypatch):  # noqa: F811
        decls = _count(monkeypatch, cj, "read_declarations")
        uses = _count(monkeypatch, cj, "read_uses")
        cross_repo_requests_func(origin="consumers", max_results=2)
        read = (decls["n"], uses["n"])
        assert read[0] > 0 and read[1] > 0
        for _ in range(2):
            cross_repo_requests_func(origin="consumers", max_results=2)
        assert (decls["n"], uses["n"]) == read

    def test_join_runs_once_for_a_repeat_and_for_cursor_pages(self, world, monkeypatch):  # noqa: F811
        join = _count(monkeypatch, cj, "undeclared_reads")
        first = cross_repo_requests_func(origin="consumers", max_results=2)
        after_first = join["n"]
        assert after_first > 0
        again = cross_repo_requests_func(origin="consumers", max_results=2)
        assert join["n"] == after_first
        assert again == first
        assert first["next_cursor"]
        page = cross_repo_requests_func(
            origin="consumers", max_results=2, cursor=first["next_cursor"],
        )
        assert join["n"] == after_first
        assert page["results"] and page["results"] != first["results"]

    def test_named_query_join_runs_once(self, world, monkeypatch):  # noqa: F811
        join = _count(monkeypatch, cj, "use_matches")
        kw = {"origin": "consumers", "service": "/prod/orders/db/password", "max_results": 1}
        first = cross_repo_requests_func(**kw)
        after_first = join["n"]
        assert after_first > 0
        assert cross_repo_requests_func(**kw) == first
        assert join["n"] == after_first

    def test_a_changed_graph_is_read_again(self, world, tmp_path, monkeypatch):  # noqa: F811
        join = _count(monkeypatch, cj, "undeclared_reads")
        before = cross_repo_requests_func(origin="consumers", max_results=100)
        cross_repo_requests_func(origin="consumers", max_results=100)
        settled = join["n"]
        _add_file(
            (tmp_path / "reports-job").resolve(), "extra.py",
            "import boto3\nssm = boto3.client('ssm')\n"
            "def go():\n    ssm.get_parameter(Name='/prod/new/added-after-first-call')\n",
        )
        after = cross_repo_requests_func(origin="consumers", max_results=100)
        assert join["n"] > settled
        names = {r["name"] for r in after["results"]}
        assert "/prod/new/added-after-first-call" in names
        assert "/prod/new/added-after-first-call" not in {r["name"] for r in before["results"]}


class TestSearchRepeat:
    def test_repeat_and_cursor_pages_do_not_search_again(self, world, monkeypatch):  # noqa: F811
        searched = _count(monkeypatch, rt, "_search_one_repo")
        first = cross_repo_search_func("run", max_results=1)
        after_first = searched["n"]
        assert after_first > 0
        assert cross_repo_search_func("run", max_results=1) == first
        assert searched["n"] == after_first
        if first.get("next_cursor"):
            page = cross_repo_search_func("run", max_results=1, cursor=first["next_cursor"])
            assert page["status"] == "ok"
            assert searched["n"] == after_first

    def test_other_arguments_search_again(self, world, monkeypatch):  # noqa: F811
        searched = _count(monkeypatch, rt, "_search_one_repo")
        cross_repo_search_func("run")
        seen = searched["n"]
        cross_repo_search_func("run", match="exact")
        assert searched["n"] > seen

    def test_a_changed_graph_is_searched_again(self, world, tmp_path, monkeypatch):  # noqa: F811
        searched = _count(monkeypatch, rt, "_search_one_repo")
        cross_repo_search_func("run")
        cross_repo_search_func("run")
        settled = searched["n"]
        _add_file((tmp_path / "reports-job").resolve(), "more.py", "def run_more():\n    pass\n")
        after = cross_repo_search_func("run_more")
        assert searched["n"] > settled
        assert any(r["name"] == "run_more" for r in after["results"])

    def test_a_budget_cut_result_is_not_kept(self, world, monkeypatch):  # noqa: F811
        monkeypatch.setenv("CRG_CROSS_REPO_BUDGET_SECONDS", "0.000001")
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "1")
        searched = _count(monkeypatch, rt, "_search_one_repo")
        first = cross_repo_search_func("run")
        if not first.get("skipped_repos_total"):
            pytest.skip("budget did not cut this call short")
        seen = searched["n"]
        cross_repo_search_func("run")
        assert searched["n"] > seen


class TestWalHeaderDoesNotMoveTheStamp:
    """An empty ``-wal`` is rewritten by every connection open; it must not change the key."""

    def test_header_only_wal_is_ignored_and_a_real_wal_is_counted(self, world, tmp_path):  # noqa: F811
        import os

        from code_review_graph import repo_cache as rc

        root = (tmp_path / "reports-job").resolve()
        db = root / ".code-review-graph" / "graph.db"
        wal = db.with_name("graph.db-wal")
        bare = rc._file_signature(db)
        wal.write_bytes(b"\0" * 32)
        assert rc._file_signature(db) == bare
        os.utime(wal, ns=(1, 1))
        assert rc._file_signature(db) == bare
        wal.write_bytes(b"\0" * 4152)
        assert rc._file_signature(db) != bare

    def test_marker_is_read_once_while_an_empty_wal_is_rewritten(
        self, world, tmp_path, monkeypatch,  # noqa: F811
    ):
        import os

        from code_review_graph import repo_cache as rc

        root = (tmp_path / "reports-job").resolve()
        wal = root / ".code-review-graph" / "graph.db-wal"
        reads = _count(monkeypatch, rc, "_graph_marker")
        entry = {"path": str(root)}
        first = rc.graph_stamp(entry)
        for tick in range(1, 4):
            wal.write_bytes(b"\0" * 32)
            os.utime(wal, ns=(tick, tick))
            assert rc.graph_stamp(entry) == first
        assert reads["n"] == 1
