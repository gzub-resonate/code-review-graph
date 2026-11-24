"""get_architecture_overview_tool caps and pages its ``communities`` list.

With ``max_results=6`` it returned every community (482 on a Scala service) and
only capped the cross-community rows, so minimal mode was not minimal.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_review_graph.communities import store_communities
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.tools import get_architecture_overview_func

COMMUNITY_COUNT = 12


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    communities = []
    for i in range(COMMUNITY_COUNT):
        members = []
        for j in range(i + 2):  # sizes 2..13, all different
            path = (root / f"m{i}.py").as_posix()
            store.upsert_node(NodeInfo(
                kind="Function", name=f"f{i}_{j}", file_path=path,
                line_start=j + 1, line_end=j + 2, language="python",
            ))
            members.append(f"{path}::f{i}_{j}")
        communities.append({
            "name": f"c{i}", "level": 0, "size": len(members), "cohesion": 0.5,
            "dominant_language": "python", "description": "", "members": members,
        })
    store.commit()
    store_communities(store, communities)
    store.close()
    yield root
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("detail_level", ["minimal", "standard"])
def test_communities_are_capped_in_both_detail_levels(repo, detail_level):
    result = get_architecture_overview_func(
        repo_root=str(repo), detail_level=detail_level, max_results=5,
    )

    assert result["status"] == "ok"
    assert len(result["communities"]) == 5
    assert result["communities_total"] == COMMUNITY_COUNT
    assert result["truncated"] is True
    assert result["next_cursor"]
    assert "showing 5 of 12" in result["summary"]
    sizes = [c["size"] for c in result["communities"]]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] == COMMUNITY_COUNT + 1  # the largest community comes first
    # Other fields are untouched.
    assert "cross_community_edges" in result
    assert "warnings" in result


def test_the_cursor_reads_every_community_once(repo):
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        result = get_architecture_overview_func(
            repo_root=str(repo), max_results=5, cursor=cursor,
        )
        assert result["status"] == "ok"
        seen.extend(c["name"] for c in result["communities"])
        pages += 1
        cursor = result.get("next_cursor")
        if not cursor:
            break

    assert pages == 3
    assert len(seen) == COMMUNITY_COUNT
    assert len(set(seen)) == COMMUNITY_COUNT
    assert seen[0] == f"c{COMMUNITY_COUNT - 1}"


def test_a_list_that_fits_has_no_cursor(repo):
    result = get_architecture_overview_func(repo_root=str(repo))

    assert len(result["communities"]) == COMMUNITY_COUNT
    assert result["communities_total"] == COMMUNITY_COUNT
    assert "next_cursor" not in result
    assert result["truncated"] is False


def test_a_cursor_from_an_older_graph_is_refused(repo):
    first = get_architecture_overview_func(repo_root=str(repo), max_results=5)
    store = GraphStore(str(repo / ".code-review-graph" / "graph.db"))
    store.upsert_node(NodeInfo(
        kind="Function", name="later", file_path=(repo / "later.py").as_posix(),
        line_start=1, line_end=2, language="python",
    ))
    store.commit()
    store.close()

    result = get_architecture_overview_func(
        repo_root=str(repo), max_results=5, cursor=first["next_cursor"],
    )

    assert result["status"] == "error"
    assert "start over" in result["error"]
