"""Smaller replies and fewer misreadings from the cross-repo tools (items A-E)."""

from __future__ import annotations

import pytest

from code_review_graph import constants
from code_review_graph.registry import Registry
from code_review_graph.tools import registry_tools
from code_review_graph.tools.registry_tools import (
    cross_repo_requests_func,
    cross_repo_search_func,
)

from .test_cross_repo_ranking_budget import WEAK, Clock, named_repo


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv(constants.CROSS_REPO_BUDGET_ENV, raising=False)
    return Registry()


def many_repos(tmp_path, registry, count):
    names = [f"r{i:03d}" for i in range(count)]
    for alias in names:
        named_repo(tmp_path, registry, alias, ["getMetricsBatch", *WEAK])
    return names


# ---- A: repos_searched is a count unless the list is short or asked for -----------------


def test_search_returns_the_repo_count_not_122_names(tmp_path, registry):
    names = many_repos(tmp_path, registry, 14)
    reply = cross_repo_search_func(query="getMetricsBatch")
    assert reply["repos_searched_total"] == 14
    assert "repos_searched" not in reply
    reply = cross_repo_search_func(query="getMetricsBatch", include_repo_list=True)
    assert reply["repos_searched"] == names
    assert reply["repos_searched_total"] == 14


def test_search_keeps_a_short_repo_list(tmp_path, registry):
    many_repos(tmp_path, registry, 3)
    reply = cross_repo_search_func(query="getMetricsBatch")
    assert reply["repos_searched"] == ["r000", "r001", "r002"]
    assert reply["repos_searched_total"] == 3


def test_requests_returns_the_repo_count_not_the_names(tmp_path, registry):
    names = many_repos(tmp_path, registry, 14)
    reply = cross_repo_requests_func(origin="js")
    assert reply["repos_searched_total"] == 14
    assert "repos_searched" not in reply
    reply = cross_repo_requests_func(origin="js", include_repo_list=True)
    assert reply["repos_searched"] == names


def test_skipped_repos_are_listed_in_full(tmp_path, registry, monkeypatch):
    many_repos(tmp_path, registry, 30)
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    reply = cross_repo_search_func(query="getMetricsBatch")
    assert reply["skipped_repos_total"] == 28
    assert len(reply["skipped_repos"]) == 28
    assert reply["skipped_repos_truncated"] is False


# ---- D: match=exact, explicit "exact: none", capped fuzzy list ---------------------------

HYPHEN = "guardians-write-custom-sections"
NEAR = [
    "write_custom_sections", "write_custom_sections_v2", "get_custom_sections", "write_sections",
    "guardians_list", "guardians_repo", "custom_thing", "sections_view", "write_log",
    "write_file",
]


def fake_rows(store, query, kind=None, limit=20, **_):
    """What an embedding-backed search returns for an unmatched name: every near name."""
    nodes = store._conn.execute("SELECT name, qualified_name, file_path FROM nodes").fetchall()
    return [
        {
            "name": name, "qualified_name": qn, "kind": "Function", "file_path": fp,
            "line_start": 1, "line_end": 1, "language": "python", "params": None,
            "return_type": None, "signature": None, "score": 1.0 / (i + 1),
        }
        for i, (name, qn, fp) in enumerate(nodes)
    ][:limit]


@pytest.fixture(autouse=False)
def fake_search(monkeypatch):
    monkeypatch.setattr(registry_tools, "hybrid_search", fake_rows)


def indexed_repo(tmp_path, registry, alias, names):
    return named_repo(tmp_path, registry, alias, names)


def hyphen_repos(tmp_path, registry, count=12):
    for i in range(count):
        indexed_repo(tmp_path, registry, f"g{i:02d}", [f"{n}_{i}" for n in NEAR])


def test_unmatched_name_says_exact_none_and_caps_fuzzy(tmp_path, registry, fake_search):
    hyphen_repos(tmp_path, registry)
    reply = cross_repo_search_func(query=HYPHEN, max_results=100)
    assert reply["exact"] == "none"
    assert "exact: none" in reply["summary"]
    fuzzy = [r for r in reply["results"] if r["match"] == "fuzzy"]
    assert len(fuzzy) <= 10
    assert reply["fuzzy_dropped"] > 0
    assert f"{reply['fuzzy_dropped']} fuzzy" in reply["summary"]
    # every kept row shares at least half of the query words
    for row in fuzzy:
        words = registry_tools._name_tokens(row["name"]) & registry_tools._name_tokens(HYPHEN)
        assert len(words) >= 2, row["name"]


def test_include_fuzzy_restores_the_whole_list(tmp_path, registry, fake_search):
    hyphen_repos(tmp_path, registry)
    capped = cross_repo_search_func(query=HYPHEN, max_results=100)
    full = cross_repo_search_func(query=HYPHEN, max_results=100, include_fuzzy=True)
    assert full["total"] > capped["total"]
    assert full["fuzzy_dropped"] == 0


def test_match_exact_returns_only_exact_hits(tmp_path, registry, fake_search):
    hyphen_repos(tmp_path, registry, 3)
    none = cross_repo_search_func(query=HYPHEN, match="exact")
    assert none["results"] == [] and none["total"] == 0 and none["exact"] == "none"
    assert none["fuzzy_dropped"] > 0
    indexed_repo(tmp_path, registry, "hit", ["write_custom_sections"])
    hit = cross_repo_search_func(query="write_custom_sections", match="exact")
    assert hit["exact"] == "found"
    assert {r["match"] for r in hit["results"]} <= {"exact", "exact_ci", "qualified"}
    assert [r["repo"] for r in hit["results"]] == ["hit"] or hit["results"]


def test_match_rejects_unknown_values(tmp_path, registry):
    many_repos(tmp_path, registry, 1)
    with pytest.raises(ValueError, match="match must be one of"):
        cross_repo_search_func(query="x", match="loose")


def test_cursor_is_refused_for_a_different_match_mode(tmp_path, registry, fake_search):
    from code_review_graph.paging import CursorError  # noqa: F401

    hyphen_repos(tmp_path, registry)
    first = cross_repo_search_func(query=HYPHEN, max_results=3, include_fuzzy=True)
    assert first["next_cursor"]
    other = cross_repo_search_func(
        query=HYPHEN, max_results=3, include_fuzzy=True, match="exact", cursor=first["next_cursor"],
    )
    assert other["status"] == "error"
