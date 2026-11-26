"""Output size, resolution defaults and ambiguity of ``query_graph``.

These pin the agent-facing behaviour of callees_of / callers_of: noise is
hidden but always counted, minimal mode says what it cut and how to read the
rest, and an ambiguous name never reads as "no callers".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query_graph


def _file(store: GraphStore, path: str, language: str = "java") -> None:
    store.upsert_node(NodeInfo(
        kind="File", name=path, file_path=path, line_start=1, line_end=200,
        language=language,
    ))


def _fn(store: GraphStore, path: str, name: str, start: int, parent: str | None = None,
        language: str = "java") -> None:
    store.upsert_node(NodeInfo(
        kind="Function", name=name, file_path=path, line_start=start,
        line_end=start + 5, language=language, parent_name=parent,
    ))


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    root = tmp_path
    (root / ".code-review-graph").mkdir()
    svc = (root / "Svc.java").as_posix()
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        _file(store, svc)
        _fn(store, svc, "export", 10)
        _fn(store, svc, "leaf", 100)
        _fn(store, svc, "other_leaf", 120)
        qn = f"{svc}::export"
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=qn, target=f"{svc}::leaf", file_path=svc, line=11,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=qn, target=f"{svc}::other_leaf", file_path=svc, line=12,
        ))
        # Bare names: three JDK/stream names and two project helpers.
        for line, name in enumerate(
            ("stream", "map", "toList", "buildReport", "sendMail"), start=13,
        ):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=qn, target=name, file_path=svc, line=line,
            ))
        store.commit()
        store.refresh_target_resolution()
    return {"root": str(root), "svc": svc, "qn": f"{svc}::export"}


class TestCalleesOfDefaultsToDirect:
    def test_default_shows_only_bound_calls_and_counts_the_rest(self, repo):
        result = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
        )
        assert {r["name"] for r in result["results"]} == {"leaf", "other_leaf"}
        assert result["resolution_split"] == {"direct": 2, "unresolved": 0}
        assert result["hidden_call_sites"] == {"unresolved": 5, "well_known": 3}
        assert "5 name-matched call site(s) hidden by default" in result["summary"]
        assert "resolution='all'" in result["summary"]

    def test_explicit_all_keeps_the_old_answer(self, repo):
        result = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
            resolution="all",
        )
        assert result["result_count"] == 7
        assert "hidden_call_sites" not in result

    def test_callers_of_default_is_unchanged(self, repo):
        result = query_graph(
            pattern="callers_of", target=f"{repo['svc']}::leaf", repo_root=repo["root"],
        )
        assert result["result_count"] == 1
        assert "hidden_call_sites" not in result

    def test_hide_unresolved_with_all_drops_noise_and_says_how_many(self, repo):
        result = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
            resolution="all", hide_unresolved=True,
        )
        assert {r["name"] for r in result["results"]} == {"leaf", "other_leaf"}
        assert result["hidden_call_sites"] == {"unresolved": 5, "well_known": 3}
        assert "3 JDK/stream/builtin" in result["summary"]

    def test_explicit_unresolved_resolution_is_not_overridden(self, repo):
        result = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
            resolution="unresolved",
        )
        assert result["result_count"] == 5
        assert "hidden_call_sites" not in result

    def test_zero_shown_does_not_read_as_no_calls(self, repo):
        svc = repo["svc"]
        with GraphStore(Path(repo["root"]) / ".code-review-graph" / "graph.db") as store:
            _fn(store, svc, "bare_only", 150)
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{svc}::bare_only", target="helper",
                file_path=svc, line=151,
            ))
            store.commit()
            store.refresh_target_resolution()
        result = query_graph(
            pattern="callees_of", target=f"{svc}::bare_only", repo_root=repo["root"],
        )
        assert result["result_count"] == 0
        assert "1 name-matched" in result["confidence"]
        assert "resolution='all'" in result["confidence"]

    def test_paging_cursor_survives_the_default(self, repo):
        first = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
            max_results=1,
        )
        assert first["next_cursor"]
        second = query_graph(
            pattern="callees_of", target=repo["qn"], repo_root=repo["root"],
            max_results=1, cursor=first["next_cursor"],
        )
        assert second["results"][0]["name"] != first["results"][0]["name"]
