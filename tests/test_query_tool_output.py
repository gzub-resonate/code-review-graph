"""Output size, resolution defaults and ambiguity of ``query_graph``.

These pin the agent-facing behaviour of callees_of / callers_of: noise is
hidden but always counted, minimal mode says what it cut and how to read the
rest, and an ambiguous name never reads as "no callers".
"""

from __future__ import annotations

import json
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


@pytest.fixture()
def wide(tmp_path: Path) -> dict:
    """One caller of many distinct callees, each only an ambiguous bare name."""
    root = tmp_path
    (root / ".code-review-graph").mkdir()
    svc = (root / "Wide.java").as_posix()
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        _file(store, svc)
        _fn(store, svc, "handle", 10)
        qn = f"{svc}::handle"
        for index in range(40):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=qn, target=f"helper{index}", file_path=svc,
                line=20 + index,
                extra={"unresolved_targets": [f"cand{n}.helper{index}" for n in range(20)]},
            ))
        store.commit()
        store.refresh_target_resolution()
    return {"root": str(root), "qn": qn}


class TestOutputSize:
    def test_minimal_drops_edges(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all", detail_level="minimal",
        )
        assert "edges" not in result

    def test_edges_never_outnumber_the_capped_rows(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all", max_results=7,
        )
        assert len(result["results"]) == 7
        assert len(result["edges"]) == 7

    def test_edges_do_not_repeat_the_candidate_lists_the_rows_carry(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all",
        )
        assert result["results"][0]["candidates"]
        for edge in result["edges"]:
            assert "unresolved_targets" not in edge
            assert edge["unresolved_target_count"] == 20
        # 40 call sites with 20 candidates apiece was ~90 KB when both lists were spelled out.
        assert len(json.dumps(result)) < 60_000


class TestMinimalHonoursMaxResults:
    def test_minimal_without_max_results_caps_at_five_and_says_how_to_read_more(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all", detail_level="minimal",
        )
        assert len(result["results"]) == 5
        assert result["results_omitted"] == 35
        assert result["next_cursor"]
        assert "35 more" in result["paging_hint"]
        assert "max_results" in result["paging_hint"] and "cursor" in result["paging_hint"]

    def test_minimal_honours_an_explicit_max_results(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all", detail_level="minimal", max_results=50,
        )
        assert len(result["results"]) == 40
        assert result["results_omitted"] == 0

    def test_minimal_honours_an_explicit_smaller_max_results(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all", detail_level="minimal", max_results=3,
        )
        assert len(result["results"]) == 3
        assert result["results_omitted"] == 37

    def test_standard_default_is_still_one_hundred(self, wide):
        result = query_graph(
            pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
            resolution="all",
        )
        assert len(result["results"]) == 40

    def test_zero_is_still_rejected(self, wide):
        with pytest.raises(ValueError):
            query_graph(
                pattern="callees_of", target=wide["qn"], repo_root=wide["root"],
                max_results=0,
            )
