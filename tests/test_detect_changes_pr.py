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


def test_minimal_mode_carries_totals_but_no_symbol_names(pull_request):
    result = detect(pull_request, head="pr", base="main", detail_level="minimal")
    section = result["pull_request"]
    assert section["added_symbols_total"] > 0
    assert section["removed_symbols_total"] > 0
    assert "added_symbols" not in section
    assert "removed_symbols" not in section
    assert set(section) == {"head", "base", "added_symbols_total", "removed_symbols_total"}


def test_standard_mode_totals_match_the_lists_when_nothing_is_cut(pull_request):
    section = detect(pull_request, head="pr", base="main")["pull_request"]
    listed = sum(len(names) for names in section["added_symbols"].values())
    assert section["added_symbols_total"] == listed
    assert section["symbols_truncated"] is False
    assert "files_with_more_symbols" not in section


def _info(added, removed=None):
    return {"head": "h" * 12, "base": "b" * 12, "added_symbols": added,
            "removed_symbols": removed or {}}


def test_a_long_symbol_list_is_cut_and_says_so_while_the_total_counts_everything():
    from code_review_graph.tools import review

    names = [f"fn{i}" for i in range(review._PR_NAMES_PER_FILE + 7)]
    section = review._pull_request_section(_info({"big.py": names, "small.py": ["a"]}), "standard")
    assert len(section["added_symbols"]["big.py"]) == review._PR_NAMES_PER_FILE
    assert section["added_symbols"]["small.py"] == ["a"]
    assert section["added_symbols_total"] == len(names) + 1
    assert section["symbols_truncated"] is True
    assert section["files_with_more_symbols"] == ["big.py"]


def test_more_files_than_the_cap_are_cut_and_counted():
    from code_review_graph.tools import review

    many = {f"f{i}.py": ["x"] for i in range(review._PR_FILES_SHOWN + 5)}
    section = review._pull_request_section(_info(many), "standard")
    assert len(section["added_symbols"]) == review._PR_FILES_SHOWN
    assert section["added_symbols_total"] == len(many)
    assert section["symbols_truncated"] is True


def test_symbol_and_file_names_are_sanitised_like_any_node_name():
    from code_review_graph.tools import review

    section = review._pull_request_section(
        _info({"a\x00b.py": ["evil\x1b[31m" + "x" * 400]}), "standard",
    )
    (path, names), = section["added_symbols"].items()
    assert "\x00" not in path
    assert "\x1b" not in names[0]
    assert len(names[0]) <= 256
