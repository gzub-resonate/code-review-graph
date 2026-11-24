"""Tool-quality batch: explicit changed files, generated code, tests_for counts, hub
ranking, surprising-connection ties and ``file_hint``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_review_graph.analysis import (
    find_surprising_connections,
    generate_suggested_questions,
    is_generated_file,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo, normalize_file_path
from code_review_graph.tools import (
    get_bridge_nodes_func,
    get_hub_nodes_func,
    get_knowledge_gaps_func,
    get_suggested_questions_func,
    get_surprising_connections_func,
    query_graph,
)
from code_review_graph.tools.review import detect_changes_func


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        s.root = tmp_path
        yield s


def _path(repo, rel):
    return normalize_file_path(repo.root / rel)


def _node(repo, name, rel, kind="Function", parent=None, language="java", **kw):
    file = _path(repo, rel)
    repo.upsert_node(NodeInfo(kind, name, file, 1, 5, language, parent_name=parent, **kw))
    return f"{file}::{parent + '.' if parent else ''}{name}"


def _edge(repo, kind, source, target):
    repo.upsert_edge(EdgeInfo(kind, source, target, source.split("::")[0], 3))


def _star(repo, hub, n=6, prefix="caller"):
    """*n* callers in distinct files, so *hub* reaches degree >= 5."""
    for i in range(n):
        caller = _node(repo, f"{prefix}{i}", f"src/main/{prefix}{i}.java")
        _edge(repo, "CALLS", caller, hub)


GEN = "src/generated/jooq/Keys.java"


# ------------------------------------------------------------------- item 1


def _detect(repo, ranges, **kw):
    with (
        patch("code_review_graph.tools.review.resolve_review_base", return_value="HEAD~1"),
        patch("code_review_graph.tools.review.parse_diff_ranges", return_value=ranges),
    ):
        return detect_changes_func(repo_root=str(repo.root), **kw)


def test_explicit_files_without_diff_lines_assume_their_symbols_changed(repo):
    _node(repo, "doWork", "src/main/Mgr.java", parent="Mgr")
    _node(repo, "doOther", "src/test/MgrTest.java", kind="Test", is_test=True)
    repo.commit()

    result = _detect(repo, {"unrelated.java": [(1, 3)]}, changed_files=["src/main/Mgr.java"])

    assert result["status"] == "ok"
    assert result["changed_symbols_assumed"] is True
    assert "changed_symbols_assumed" in result["summary"]
    assert result["risk_score"] > 0
    assert result["test_gaps_total"] == 1
    assert [f["name"] for f in result["changed_functions"]] == ["doWork"]


def test_explicit_files_without_any_diff_also_score(repo):
    _node(repo, "doWork", "src/main/Mgr.java", parent="Mgr")
    repo.commit()

    result = _detect(repo, {}, changed_files=["src/main/Mgr.java"])

    assert result["risk_score"] > 0
    assert result["test_gaps_total"] == 1


def test_a_real_diff_is_unchanged(repo):
    _node(repo, "doWork", "src/main/Mgr.java", parent="Mgr")
    repo.commit()

    result = _detect(repo, {"src/main/Mgr.java": [(1, 5)]}, changed_files=["src/main/Mgr.java"])

    assert "changed_symbols_assumed" not in result
    assert result["risk_score"] > 0
    assert [f["name"] for f in result["changed_functions"]] == ["doWork"]


def test_minimal_detail_reports_gap_split(repo):
    _node(repo, "doWork", "src/main/Mgr.java", parent="Mgr")
    repo.commit()

    result = _detect(repo, {"x.java": [(1, 2)]}, changed_files=["src/main/Mgr.java"],
                     detail_level="minimal")

    assert result["test_gaps_uncovered"] == 1
    assert result["test_gaps_indirect"] == 0
    assert result["changed_symbols_assumed"] is True


# ------------------------------------------------------------------- item 2


def _generated_graph(repo):
    keys = _node(repo, "Keys", GEN, kind="Class")
    _star(repo, keys, n=6, prefix="user")
    real = _node(repo, "Service", "src/main/Service.java", kind="Class")
    _star(repo, real, n=6, prefix="client")
    # Isolated generated node and isolated production node.
    _node(repo, "setPrefix", "src/generated/jooq/Attribute.java", parent="Attribute")
    _node(repo, "lonely", "src/main/Lonely.java")
    repo.commit()
    return keys, real


def test_generated_path_detection():
    assert is_generated_file("src/generated/jooq/Keys.java")
    assert is_generated_file("/r/src/__generated__/x.ts", "/r")
    assert is_generated_file("/r/api/foo_pb2.py", "/r")
    assert not is_generated_file("/generated/r/src/Keys.java", "/generated/r")
    assert not is_generated_file("src/main/Service.java")


def test_hubs_skip_generated_by_default(repo):
    keys, real = _generated_graph(repo)

    default = get_hub_nodes_func(repo_root=str(repo.root))
    names = [h["qualified_name"] for h in default["hub_nodes"]]
    assert keys not in names and real in names
    assert default["generated_excluded"] == 1
    assert "generated node(s) excluded" in default["summary"]

    included = get_hub_nodes_func(repo_root=str(repo.root), include_generated=True)
    assert keys in [h["qualified_name"] for h in included["hub_nodes"]]
    assert included["generated_excluded"] == 0


def test_hub_cursor_is_refused_for_the_other_generated_value(repo):
    _generated_graph(repo)
    first = get_hub_nodes_func(repo_root=str(repo.root), top_n=1)
    cursor = first["next_cursor"]
    with pytest.raises(Exception):  # noqa: B017 - CursorError subclass of ValueError
        get_hub_nodes_func(repo_root=str(repo.root), top_n=1, cursor=cursor,
                           include_generated=True)


def test_bridges_skip_generated_by_default(repo):
    a = _node(repo, "a", "src/main/A.java")
    gen = _node(repo, "mid", GEN, parent="Keys")
    b = _node(repo, "b", "src/main/B.java")
    _edge(repo, "CALLS", a, gen)
    _edge(repo, "CALLS", gen, b)
    repo.commit()

    default = get_bridge_nodes_func(repo_root=str(repo.root), include_config=True)
    assert gen not in [r["qualified_name"] for r in default["bridge_nodes"]]
    assert default["generated_excluded"] >= 1

    included = get_bridge_nodes_func(repo_root=str(repo.root), include_generated=True)
    assert gen in [r["qualified_name"] for r in included["bridge_nodes"]]


def test_knowledge_gaps_skip_generated_by_default(repo):
    keys, _real = _generated_graph(repo)

    default = get_knowledge_gaps_func(repo_root=str(repo.root), max_per_category=50)
    isolated = [r["qualified_name"] for r in default["gaps"]["isolated_nodes"]]
    hotspots = [r["qualified_name"] for r in default["gaps"]["untested_hotspots"]]
    assert not any("generated" in q for q in isolated + hotspots)
    assert any("lonely" in q for q in isolated)
    assert default["generated_excluded"]["isolated_nodes"] == 1
    assert default["generated_excluded"]["untested_hotspot_candidates"] == 1

    included = get_knowledge_gaps_func(
        repo_root=str(repo.root), max_per_category=50, include_generated=True,
    )
    assert keys in [r["qualified_name"] for r in included["gaps"]["untested_hotspots"]]
    assert any("setPrefix" in r["qualified_name"] for r in included["gaps"]["isolated_nodes"])


def test_surprising_connections_skip_generated_by_default(repo):
    a = _node(repo, "a", "src/main/A.java")
    gen = _node(repo, "g", "src/generated/jooq/Keys.py", parent="Keys", language="py")
    repo.upsert_edge(EdgeInfo("CALLS", a, gen, "x", 3))
    b = _node(repo, "b", "src/main/B.java")
    c = _node(repo, "c", "src/main/C.py", language="py")
    _edge(repo, "CALLS", b, c)
    repo.commit()

    default = get_surprising_connections_func(repo_root=str(repo.root))
    pairs = [
        (r["source_qualified"], r["target_qualified"])
        for r in default["surprising_connections"]
    ]
    assert (a, gen) not in pairs and (b, c) in pairs
    assert default["generated_excluded"] == 1

    included = get_surprising_connections_func(repo_root=str(repo.root), include_generated=True)
    assert (a, gen) in [
        (r["source_qualified"], r["target_qualified"]) for r in included["surprising_connections"]
    ]


def test_suggested_questions_skip_generated_by_default(repo):
    keys, real = _generated_graph(repo)

    default = get_suggested_questions_func(repo_root=str(repo.root))
    assert keys not in [q["target"] for q in default["questions"]]
    assert default["generated_excluded"] >= 1

    included = get_suggested_questions_func(repo_root=str(repo.root), include_generated=True)
    assert keys in [q["target"] for q in included["questions"]]


def test_analysis_level_default_keeps_generated(repo):
    keys, _ = _generated_graph(repo)
    questions = generate_suggested_questions(repo)
    assert keys in [q["target"] for q in questions]


# ------------------------------------------------------------------- item 3


def test_tests_for_reports_direct_and_inferred_counts(repo):
    target = _node(repo, "createAttribute", "src/main/Mgr.java", parent="Mgr")
    direct = _node(repo, "testCreate", "src/test/MgrTest.java", kind="Test", is_test=True)
    _edge(repo, "TESTED_BY", target, direct)
    # A test named after the method, with no edge: a naming-convention guess.
    _node(repo, "test_createAttribute", "src/test/NameTest.java", kind="Test", is_test=True)
    repo.commit()

    for level in ("standard", "minimal"):
        result = query_graph(
            pattern="tests_for", target=target, repo_root=str(repo.root), detail_level=level,
        )
        assert result["tests_direct"] + result["tests_inferred"] == result["result_count"]
        assert result["tests_direct"] == 1
        assert result["tests_inferred"] == 1
        assert "1 direct, 1 inferred" in result["summary"]


def test_other_patterns_carry_no_tests_counts(repo):
    target = _node(repo, "createAttribute", "src/main/Mgr.java", parent="Mgr")
    repo.commit()
    result = query_graph(pattern="callers_of", target=target, repo_root=str(repo.root))
    assert "tests_direct" not in result


# ------------------------------------------------------------------- item 4


def test_hubs_carry_ranking_degree_in_every_detail_level(repo):
    _generated_graph(repo)
    for level in ("standard", "minimal"):
        result = get_hub_nodes_func(repo_root=str(repo.root), detail_level=level)
        assert all("ranking_degree" in h and "total_degree" in h for h in result["hub_nodes"])
        assert "ranking_degree" in result["summary"]
        assert "not strictly descending" in result["summary"]


# ------------------------------------------------------------------- item 5


def _family_graph(repo):
    src = _node(repo, "createAttribute", "src/main/AttributeMgr.java", parent="AttributeMgr")
    for name in ("canRead", "canWrite", "canDelete"):
        t = _node(repo, name, "src/main/AttributeAccessMgr.py", parent="AttributeAccessMgr",
                  language="py")
        _edge(repo, "CALLS", src, t)
    other = _node(repo, "other", "src/main/Other.java", parent="Other")
    lone = _node(repo, "lone", "src/main/Lone.py", parent="Lone", language="py")
    _edge(repo, "CALLS", other, lone)
    # Make "lone" busier, so it wins the degree tie-break over the can* calls.
    for i in range(3):
        extra = _node(repo, f"x{i}", f"src/main/X{i}.java", parent=f"X{i}")
        _edge(repo, "CALLS", extra, lone)
    repo.commit()
    return src, lone


def test_surprising_ties_are_broken_by_combined_degree_then_name(repo):
    src, lone = _family_graph(repo)

    rows = find_surprising_connections(repo, top_n=100)
    again = find_surprising_connections(repo, top_n=100)

    assert rows == again
    tied = [r for r in rows if r["surprise_score"] == rows[0]["surprise_score"]]
    assert len(tied) >= 4
    keys = [(-r["combined_degree"], r["source_qualified"], r["target_qualified"]) for r in tied]
    assert keys == sorted(keys)
    assert tied[0]["target_qualified"] == lone


def test_group_count_counts_repeated_families_without_dropping_rows(repo):
    src, lone = _family_graph(repo)

    rows = find_surprising_connections(repo, top_n=100)

    family = [r for r in rows if r["source_qualified"] == src]
    assert len(family) == 3
    assert {r["group_count"] for r in family} == {3}
    singles = [r for r in rows if r["target_qualified"] == lone]
    assert {r["group_count"] for r in singles} == {1}

    tool = get_surprising_connections_func(repo_root=str(repo.root), detail_level="minimal")
    assert tool["total"] == len(rows)
    assert all("group_count" in r for r in tool["surprising_connections"])


# ------------------------------------------------------------------- item 6


def _ambiguous_graph(repo):
    ctrl = _node(repo, "createAttribute", "src/main/AttributeController.java",
                 parent="AttributeController")
    mgr = _node(repo, "createAttribute", "src/main/AttributeMgr.java", parent="AttributeMgr")
    repo.commit()
    return ctrl, mgr


def test_file_hint_narrows_an_ambiguous_name(repo):
    ctrl, mgr = _ambiguous_graph(repo)

    plain = query_graph(pattern="callers_of", target="createAttribute", repo_root=str(repo.root))
    assert plain["status"] == "ambiguous"

    hinted = query_graph(
        pattern="callers_of", target="createAttribute", repo_root=str(repo.root),
        file_hint="attributemgr",
    )
    assert hinted["status"] == "ok"
    assert hinted["target"] == mgr


def test_a_hint_matching_nothing_stays_ambiguous(repo):
    _ambiguous_graph(repo)
    result = query_graph(
        pattern="callers_of", target="createAttribute", repo_root=str(repo.root),
        file_hint="nowhere",
    )
    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2


def test_a_hint_matching_both_stays_ambiguous(repo):
    _ambiguous_graph(repo)
    result = query_graph(
        pattern="callers_of", target="createAttribute", repo_root=str(repo.root),
        file_hint="Attribute",
    )
    assert result["status"] == "ambiguous"
