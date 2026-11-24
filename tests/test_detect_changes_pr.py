# ruff: noqa: F811
"""detect_changes_tool scores a pull request's files, new ones included, from the base graph."""

from __future__ import annotations

import pytest

from code_review_graph.tools.review import detect_changes_func

from .test_pr_blast_radius import git, pull_request, registry, run, write  # noqa: F401


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
    assert set(section) == {
        "head", "base", "added_symbols_total", "removed_symbols_total", "new_code",
    }
    assert "symbols" not in section["new_code"]


def test_standard_mode_totals_match_the_lists_when_nothing_is_cut(pull_request):
    section = detect(pull_request, head="pr", base="main")["pull_request"]
    listed = sum(len(names) for names in section["added_symbols"].values())
    assert section["added_symbols_total"] == listed
    assert section["symbols_truncated"] is False
    assert "files_with_more_symbols" not in section


def _info(added, removed=None):
    return {"head": "h" * 12, "base": "b" * 12, "added_symbols": added,
            "removed_symbols": removed or {},
            "new_code": {"rows": [], "production_added": 0, "tests_added": 0, "untested": 0,
                         "score": 0.0}}


def _section(info, size=100):
    from code_review_graph.paging import paginate
    from code_review_graph.tools import review

    rows = review._pull_request_rows(info)
    page = paginate(rows, size, 100, scope="s", stamp="g")
    return review._pull_request_section(info, "standard", page)


def test_rows_keep_file_order_with_added_before_removed_within_a_file():
    from code_review_graph.tools import review

    rows = review._pull_request_rows(
        _info({"a.py": ["x"], "b.py": ["y"]}, {"a.py": ["gone"], "c.py": ["z"]}),
    )
    assert [(r["file"], r["change"], r["name"]) for r in rows] == [
        ("a.py", "added", "x"), ("a.py", "removed", "gone"),
        ("b.py", "added", "y"), ("c.py", "removed", "z"),
    ]


def test_a_page_of_rows_is_cut_says_so_and_the_total_counts_everything():
    names = [f"fn{i}" for i in range(30)]
    section = _section(_info({"big.py": names, "small.py": ["a"]}), size=10)
    assert len(section["added_symbols"]["big.py"]) == 10
    assert "small.py" not in section["added_symbols"]
    assert section["added_symbols_total"] == 31
    assert section["symbols_truncated"] is True
    assert "files_with_more_symbols" not in section


def test_a_whole_list_that_fits_one_page_is_not_truncated():
    section = _section(_info({"a.py": ["x"]}, {"b.py": ["y"]}), size=10)
    assert section["symbols_truncated"] is False
    assert section["removed_symbols"] == {"b.py": ["y"]}


def test_symbol_and_file_names_are_sanitised_like_any_node_name():
    section = _section(_info({"a\x00b.py": ["evil\x1b[31m" + "x" * 400]}))
    (path, names), = section["added_symbols"].items()
    assert "\x00" not in path
    assert "\x1b" not in names[0]
    assert len(names[0]) <= 256


MANY = "".join(f"function added{i:02d}() {{\n  return {i};\n}}\n" for i in range(12))


@pytest.fixture
def many_symbols(pull_request):
    """The pull request fixture plus a branch whose pull request adds twelve functions."""
    git(pull_request, "checkout", "-q", "pr")
    write(pull_request, "many.js", MANY)
    git(pull_request, "add", "-A")
    git(pull_request, "commit", "-q", "-m", "many")
    git(pull_request, "checkout", "-q", "main")
    return pull_request


def _walk(api, **args):
    """Every page of a pull request review, following next_cursor until it stops."""
    pages = [detect(api, head="pr", base="main", **args)]
    while pages[-1].get("next_cursor"):
        pages.append(detect(api, head="pr", base="main", cursor=pages[-1]["next_cursor"], **args))
    return pages


def _added(pages):
    return [name for page in pages
            for names in page["pull_request"]["added_symbols"].values() for name in names]


def test_max_results_caps_the_symbols_and_a_cursor_reads_the_rest(many_symbols):
    pages = _walk(many_symbols, max_results=5)
    first = pages[0]["pull_request"]
    assert sum(len(n) for n in first["added_symbols"].values()) <= 5
    assert first["symbols_truncated"] is True
    assert len(pages) > 1
    assert pages[-1]["pull_request"]["symbols_truncated"] is False


def test_paging_returns_every_symbol_once(many_symbols):
    whole = _added([detect(many_symbols, head="pr", base="main", max_results=100)])
    paged = _added(_walk(many_symbols, max_results=3))
    assert sorted(paged) == sorted(whole)
    assert len(paged) == len(set(paged))
    assert {f"added{i:02d}" for i in range(12)} <= set(paged)


def test_every_page_reports_the_same_totals(many_symbols):
    pages = _walk(many_symbols, max_results=4)
    totals = {page["pull_request"]["added_symbols_total"] for page in pages}
    assert len(totals) == 1


def test_the_first_page_says_how_many_symbols_it_shows(many_symbols):
    first = detect(many_symbols, head="pr", base="main", max_results=5)
    assert "Pull request symbol(s)" in first["summary"]
    assert "showing 5 of" in first["summary"]


def test_a_cursor_from_another_review_is_refused(many_symbols):
    first = detect(many_symbols, head="pr", base="main", max_results=5)
    other = detect(many_symbols, head="pr", base="main", max_results=5, max_depth=1,
                   cursor=first["next_cursor"])
    assert other["status"] == "error"
    assert "different query" in other["error"]


def test_minimal_mode_has_no_pages_in_pull_request_mode(many_symbols):
    first = detect(many_symbols, head="pr", base="main", max_results=5)
    refused = detect(many_symbols, head="pr", base="main", detail_level="minimal",
                     cursor=first["next_cursor"])
    assert refused["status"] == "error"
    assert "no pages" in refused["error"]


def test_a_review_that_fits_one_page_has_no_cursor(pull_request):
    result = detect(pull_request, head="pr", base="main")
    assert "next_cursor" not in result
    assert result["pull_request"]["symbols_truncated"] is False
