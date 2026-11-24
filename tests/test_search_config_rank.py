"""Config-document nodes (YamlPath, JsonPath, DocSection) rank below code by default."""

from __future__ import annotations

import pytest

from code_review_graph import search
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.search import hybrid_search, rebuild_fts_index


@pytest.fixture
def store(tmp_path):
    s = GraphStore(str(tmp_path / "graph.db"))
    s.upsert_node(NodeInfo(
        kind="YamlPath", name="$.on.schedule", file_path=str(tmp_path / "wf.yml"),
        line_start=3, line_end=5, language="yaml",
    ))
    s.upsert_node(NodeInfo(
        kind="DocSection", name="Schedule environment shutdown",
        file_path=str(tmp_path / "README.md"), line_start=1, line_end=9, language="markdown",
    ))
    s.upsert_node(NodeInfo(
        kind="Function", name="schedule_environment_shutdown",
        file_path=str(tmp_path / "shutdown.py"),
        line_start=1, line_end=30, language="python",
        extra={"docstring": "Shut down an environment on a schedule"},
    ))
    s.commit()
    rebuild_fts_index(s)
    yield s
    s.close()


def _kinds(store, query, **kw):
    return [r["kind"] for r in hybrid_search(store, query, **kw)]


def test_config_nodes_rank_below_code_by_default(store, monkeypatch):
    query = "schedule"
    monkeypatch.setattr(search, "_CONFIG_RANK_FACTOR", 1.0)
    assert _kinds(store, query)[0] == "YamlPath"  # without the factor the key wins
    monkeypatch.undo()
    assert _kinds(store, query)[0] == "Function"


def test_the_factor_halves_the_config_score(store, monkeypatch):
    def score(factor):
        monkeypatch.setattr(search, "_CONFIG_RANK_FACTOR", factor)
        rows = hybrid_search(store, "schedule")
        return next(r["score"] for r in rows if r["kind"] == "YamlPath")

    assert score(0.5) == pytest.approx(score(1.0) * 0.5, rel=1e-3)


@pytest.mark.parametrize(
    ("query", "kind"),
    [("schedule", "YamlPath"), ("$.on.schedule", None), ("wf.yml schedule", None),
     ("on.schedule", None)],
)
def test_config_comes_first_when_asked_for(store, query, kind):
    assert _kinds(store, query, kind=kind)[0] == "YamlPath"
