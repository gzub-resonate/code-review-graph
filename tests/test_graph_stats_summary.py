"""``list_graph_stats_tool``: the summary does not restate the per-kind counts."""

from __future__ import annotations

from code_review_graph.tools.query import list_graph_stats

from .test_asl_cross_repo_links import make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401


def test_summary_leaves_the_per_kind_counts_to_the_structured_fields(tmp_path, registry) -> None:
    root = make_repo(tmp_path, registry, "app", {"a.py": "def f():\n    return 1\n"})
    reply = list_graph_stats(repo_root=str(root))
    assert reply["status"] == "ok"
    assert reply["nodes_by_kind"] and reply["edges_by_kind"] is not None
    assert "Graph statistics for app" in reply["summary"]
    assert f"Total nodes: {reply['total_nodes']}" in reply["summary"]
    assert "CALLS by target resolution" in reply["summary"]
    assert "Nodes by kind" not in reply["summary"]
    assert "Edges by kind" not in reply["summary"]
