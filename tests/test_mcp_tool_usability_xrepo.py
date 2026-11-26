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
