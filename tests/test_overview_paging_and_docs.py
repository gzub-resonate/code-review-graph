"""The architecture overview pages its communities; the docs section needs no repo on a
multi-repo server.
"""

from __future__ import annotations

import pytest

import code_review_graph.main as main_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.tools import community_tools


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata("repo_root", str(tmp_path))
        store.commit()
    fake = {
        "communities": [
            {"id": i, "name": f"c{i}", "size": i % 7 + 1, "cohesion": 0.5} for i in range(250)
        ],
        "cross_community_edges": [],
        "warnings": [],
    }
    monkeypatch.setattr(community_tools, "get_architecture_overview", lambda *a, **k: fake)
    return tmp_path


def test_communities_come_one_page_at_a_time_largest_first(repo):
    first = community_tools.get_architecture_overview_func(repo_root=str(repo), max_results=40)
    assert len(first["communities"]) == 40
    assert first["communities_total"] == 250
    sizes = [c["size"] for c in first["communities"]]
    assert sizes == sorted(sizes, reverse=True)
    assert first["next_cursor"]


def test_the_cursor_reads_every_community_exactly_once(repo):
    seen, cursor = [], None
    for _ in range(20):
        page = community_tools.get_architecture_overview_func(
            repo_root=str(repo), max_results=60, cursor=cursor,
        )
        seen += [c["id"] for c in page["communities"]]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert sorted(seen) == list(range(250))


def test_a_multi_repo_server_serves_the_docs_without_a_repo_root(monkeypatch):
    monkeypatch.setattr(main_module, "_multi_repo", True)
    reply = main_module.get_docs_section_tool(section_name="usage")
    assert reply["status"] == "ok" and reply["content"]
