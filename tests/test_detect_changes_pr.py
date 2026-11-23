# ruff: noqa: F811
"""detect_changes_tool scores a pull request's files, new ones included, from the base graph."""

from __future__ import annotations

from code_review_graph.tools.review import detect_changes_func

from .test_pr_blast_radius import pull_request, registry, run  # noqa: F401


def detect(api, **args):
    return detect_changes_func(repo_root=str(api), **args)


def test_a_changed_symbol_in_the_pull_request_is_scored(pull_request):
    result = detect(pull_request, head="pr", base="main")
    assert result["status"] == "ok"
    assert result["risk_score"] > 0
    names = [f["name"] for f in result["changed_functions"]]
    assert "loadItems" in names


def test_the_pull_request_section_lists_added_and_removed_symbols(pull_request):
    result = detect(pull_request, head="pr", base="main")
    section = result["pull_request"]
    assert "unusedHelper" in section["removed_symbols"]["server.js"]


def test_a_new_file_is_counted_among_the_changed_files(pull_request):
    result = detect(pull_request, head="pr", base="main")
    assert "routes.js" in result["changed_files"]


def test_the_working_tree_diff_is_not_read_in_pull_request_mode(pull_request, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("parse_diff_ranges must not run in pull request mode")

    monkeypatch.setattr("code_review_graph.tools.review.parse_diff_ranges", boom)
    assert detect(pull_request, head="pr", base="main")["status"] == "ok"


def test_changed_files_and_a_pull_request_together_are_refused(pull_request):
    result = detect(pull_request, head="pr", changed_files=["server.js"])
    assert result["status"] == "error"


def test_a_missing_head_is_an_error(pull_request):
    result = detect(pull_request, head="no-such-ref")
    assert result["status"] == "error"


def test_the_default_call_is_unchanged(pull_request):
    result = detect(pull_request)
    assert "pull_request" not in result
