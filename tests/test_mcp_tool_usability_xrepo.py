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


# ---- B: awssdk is_test / include_tests / path_prefix / source, stepfunction machine ----------

from .test_asl_cross_repo_links import MACHINES  # noqa: E402
from .test_asl_cross_repo_links import make_repo as make_asl_repo  # noqa: E402
from .test_cross_repo_requests_paging import make_repo as make_plain_repo  # noqa: E402


def boto(bucket: str, func: str = "f") -> str:
    return (
        "import boto3\ns3 = boto3.client('s3')\n"
        f"def {func}():\n    s3.get_object(Bucket='{bucket}', Key='k')\n"
    )


@pytest.fixture
def sdk_repo(tmp_path, registry):
    """Five files with one S3 call each. The parser emits no AWS SDK edge from a test path
    (graphs built before that rule do hold them, as the live one does), so the test rows
    are parsed under a plain name and then moved into test paths in the graph."""
    root = make_plain_repo(tmp_path, registry, "svc", {
        "app/main.py": boto("prod-bkt", "load"),
        "mcp/server.py": boto("mcp-bkt", "serve"),
        "mcp/tools/more.py": boto("mcp-bkt2", "more"),
        "app/t_a.py": boto("test-bkt", "check"),
        "app/t_b.py": boto("test-bkt2", "check_b"),
    })
    import sqlite3

    conn = sqlite3.connect(root / ".code-review-graph" / "graph.db")
    for old, new in (("app/t_a.py", "src/test/py/test_a.py"), ("app/t_b.py", "tests/test_b.py")):
        conn.execute(
            "UPDATE edges SET file_path = ? WHERE file_path = ?",
            (str(root / new), str(root / old)),
        )
    conn.commit()
    conn.close()


def sdk(**args):
    reply = cross_repo_requests_func(origin="awssdk", **args)
    assert reply["status"] == "ok", reply
    return reply


def files_of(reply):
    return sorted(r["file"] for r in reply["results"])


def test_awssdk_rows_carry_is_test_and_tests_are_kept_by_default(sdk_repo):
    reply = sdk()
    flags = {r["file"]: r["is_test"] for r in reply["results"]}
    assert flags == {
        "app/main.py": False, "mcp/server.py": False, "mcp/tools/more.py": False,
        "src/test/py/test_a.py": True, "tests/test_b.py": True,
    }
    assert reply["total"] == 5 and reply["tests_hidden"] == 0


def test_include_tests_false_hides_them_and_counts_them(sdk_repo):
    reply = sdk(include_tests=False)
    assert files_of(reply) == ["app/main.py", "mcp/server.py", "mcp/tools/more.py"]
    assert reply["total"] == 3 and reply["tests_hidden"] == 2
    assert "2 test row(s) hidden" in reply["summary"]
    assert sum(reply["counts"].values()) == 3


def test_path_prefix_and_source_filters(sdk_repo):
    assert files_of(sdk(path_prefix="mcp/")) == ["mcp/server.py", "mcp/tools/more.py"]
    assert files_of(sdk(path_prefix="mcp/server.py")) == ["mcp/server.py"]
    assert files_of(sdk(source="serve")) == ["mcp/server.py"]
    assert files_of(sdk(path_prefix="mcp", source="more")) == ["mcp/tools/more.py"]
    assert sdk(path_prefix="nothing/")["results"] == []


def test_filters_apply_before_paging_and_cursors_are_tied_to_them(sdk_repo):
    first = sdk(path_prefix="mcp/", max_results=1)
    assert first["total"] == 2 and len(first["results"]) == 1 and first["next_cursor"]
    second = sdk(path_prefix="mcp/", max_results=1, cursor=first["next_cursor"])
    assert files_of(first) + files_of(second) == ["mcp/server.py", "mcp/tools/more.py"]
    assert "next_cursor" not in second
    for other in ({"path_prefix": "app/"}, {"include_tests": False}, {"source": "serve"}, {}):
        with pytest.raises(Exception, match="different query"):
            sdk(max_results=1, cursor=first["next_cursor"], **other)


def test_new_filters_are_refused_for_other_origins():
    with pytest.raises(ValueError, match="machine"):
        cross_repo_requests_func(origin="awssdk", machine="orders")
    with pytest.raises(ValueError, match="path_prefix"):
        cross_repo_requests_func(origin="trigger", path_prefix="x")
    with pytest.raises(ValueError, match="include_tests"):
        cross_repo_requests_func(origin="stepfunction", include_tests=False)


def test_stepfunction_machine_filter(tmp_path, registry):
    make_asl_repo(tmp_path, registry, "machines", MACHINES)
    everything = cross_repo_requests_func(origin="stepfunction")
    only = cross_repo_requests_func(origin="stepfunction", machine="orders")
    assert only["total"] == everything["total"] > 0
    assert {r["machine"] for r in only["results"]} == {"orders"}
    none = cross_repo_requests_func(origin="stepfunction", machine="nope")
    assert none["results"] == [] and none["total"] == 0
    first = cross_repo_requests_func(origin="stepfunction", machine="orders", max_results=1)
    assert first["next_cursor"]
    with pytest.raises(Exception, match="different query"):
        cross_repo_requests_func(
            origin="stepfunction", machine="nope", max_results=1, cursor=first["next_cursor"],
        )


# ---- C: pr_blast_radius_tool pages its files ---------------------------------------------

from code_review_graph.paging import CursorError  # noqa: E402

from .test_pr_blast_radius import pull_request  # noqa: E402,F401
from .test_pr_blast_radius import run as pr_run  # noqa: E402


def test_pr_files_page_with_a_cursor_and_nothing_is_lost(pull_request):  # noqa: F811
    whole = pr_run(pull_request)
    names = [f["file"] for f in whole["files"]]
    assert len(names) >= 3 and whole["files_truncated"] is False
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = pr_run(pull_request, files_limit=1, cursor=cursor)
        pages += 1
        seen += [f["file"] for f in page["files"]]
        assert page["files_total"] == len(names)
        assert page["totals"]["files"] == whole["totals"]["files"]
        if cursor:
            assert page["offset"] == pages - 1
            # the heavy lists are on the first page only
            assert "endpoints" not in page and "continuation_note" in page
        else:
            assert page["endpoints"] == whole["endpoints"]
        cursor = page.get("next_cursor")
        assert page["files_truncated"] is bool(cursor)
        if not cursor:
            break
    assert seen == names and pages == len(names)


def test_pr_cursor_is_refused_for_other_arguments(pull_request):  # noqa: F811
    first = pr_run(pull_request, files_limit=1)
    cursor = first["next_cursor"]
    with pytest.raises(CursorError, match="different query"):
        pr_run(pull_request, files_limit=1, depth=2, cursor=cursor)
    with pytest.raises(CursorError, match="different query"):
        pr_run(pull_request, files_limit=1, include_external=False, cursor=cursor)
    with pytest.raises(CursorError):
        pr_run(pull_request, files_limit=1, cursor="not-a-cursor")


def test_pr_files_limit_is_bounded_and_validated(pull_request):  # noqa: F811
    with pytest.raises(ValueError):
        pr_run(pull_request, files_limit=0)
    assert len(pr_run(pull_request, files_limit=10_000)["files"]) <= 200
