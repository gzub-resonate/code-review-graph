"""The change tools share one rule for a call that names no change.

With no ``pr``, ``head``, ``base`` or ``changed_files`` every change tool reviews the last
commit and labels it in a ``scope`` object plus a ``note`` carrying the same hint. A named
change reports ``scope.kind == "explicit"``. Fixtures are synthetic repositories built in
``tmp_path``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.change_scope import CHANGE_SCOPE_HINT, CHANGE_SCOPE_HINT_NO_PR
from code_review_graph.tools.pr_tools import pr_blast_radius_func
from code_review_graph.tools.query import get_impact_radius
from code_review_graph.tools.review import (
    detect_changes_func,
    get_affected_flows_func,
    get_review_context,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "detect_changes": detect_changes_func,
    "get_review_context": get_review_context,
    "get_impact_radius": get_impact_radius,
    "get_affected_flows": get_affected_flows_func,
    "pr_blast_radius": pr_blast_radius_func,
}
#: The tools that take ``pr`` and ``head``; the others take only ``base`` and ``changed_files``.
TAKES_PR = {"detect_changes", "pr_blast_radius"}


def _hint_for(name: str) -> str:
    return CHANGE_SCOPE_HINT if name in TAKES_PR else CHANGE_SCOPE_HINT_NO_PR


#: The tools that take ``changed_files``.
FILE_TOOLS = ["detect_changes", "get_review_context", "get_impact_radius", "get_affected_flows"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-review-graph/\n", encoding="utf-8")
    return repo


def _build(repo: Path) -> Path:
    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        full_build(repo, store)
    return repo


@pytest.fixture
def two_commits(tmp_path: Path) -> Path:
    """Default-branch checkout: the last commit edits one file and adds another."""
    repo = _init(tmp_path / "two")
    (repo / "service.py").write_text(
        "def handle(x):\n    return helper(x)\n\n\ndef helper(x):\n    return x\n",
        encoding="utf-8",
    )
    (repo / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "service.py").write_text(
        "def handle(x):\n    return helper(x) + 1\n\n\ndef helper(x):\n    return x\n",
        encoding="utf-8",
    )
    (repo / "added.py").write_text("def added():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    return _build(repo)


@pytest.fixture
def one_commit(tmp_path: Path) -> Path:
    repo = _init(tmp_path / "one")
    (repo / "service.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "only")
    return _build(repo)


def _call(name: str, repo: Path, **kwargs: Any) -> dict[str, Any]:
    return TOOLS[name](repo_root=str(repo), **kwargs)


# --- (a) default branch, no arguments -----------------------------------------------


@pytest.mark.parametrize("name", list(TOOLS))
def test_no_arguments_review_the_labelled_last_commit(name: str, two_commits: Path) -> None:
    result = _call(name, two_commits)

    assert result["status"] == "ok", result
    scope = result["scope"]
    assert scope["kind"] == "last_commit"
    assert scope["head"] == _git(two_commits, "rev-parse", "HEAD")
    assert scope["base"] == _git(two_commits, "rev-parse", "HEAD~1")
    assert scope["files_considered"] == 2
    assert scope["reason"]
    assert scope["hint"] == _hint_for(name)
    assert _hint_for(name) in result["note"]


def test_each_tool_hints_only_at_arguments_it_takes(two_commits: Path) -> None:
    for name in TOOLS:
        result = _call(name, two_commits)
        assert result["scope"]["hint"] == _hint_for(name), name
        assert _hint_for(name) in result["note"], name
        if name in TAKES_PR:
            assert "pr=" in result["note"] and "head=" in result["note"], name
        else:
            assert "pr=" not in result["note"] and "head=" not in result["note"], name
            assert "no base/changed_files given" in result["scope"]["reason"], name
    for token in ("pr", "head", "base", "changed_files", 'base="HEAD~20"'):
        assert token in CHANGE_SCOPE_HINT
    for token in ("base", "changed_files", 'base="HEAD~20"'):
        assert token in CHANGE_SCOPE_HINT_NO_PR
    assert "pr=" not in CHANGE_SCOPE_HINT_NO_PR and "head=" not in CHANGE_SCOPE_HINT_NO_PR


def test_detect_changes_without_arguments_finds_the_last_commit_changes(
    two_commits: Path,
) -> None:
    result = detect_changes_func(repo_root=str(two_commits), detail_level="minimal")

    assert result["status"] == "ok"
    assert result["changed_file_count"] == 2


def test_payload_stays_bounded_for_a_large_last_commit(tmp_path: Path) -> None:
    repo = _init(tmp_path / "big")
    (repo / "seed.py").write_text("def seed():\n    return 0\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    for i in range(40):
        (repo / f"mod_{i}.py").write_text(f"def fn_{i}():\n    return {i}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "many")
    _build(repo)

    result = detect_changes_func(repo_root=str(repo), max_results=5)

    assert result["scope"]["files_considered"] == 40
    assert len(result["changed_functions"]) <= 5
    assert result["changed_functions_total"] >= 40


# --- (b) explicit arguments keep their behaviour --------------------------------------


@pytest.mark.parametrize("name", FILE_TOOLS)
def test_explicit_changed_files_report_an_explicit_scope(name: str, two_commits: Path) -> None:
    result = _call(name, two_commits, changed_files=["service.py"])

    assert result["status"] == "ok", result
    assert result["scope"]["kind"] == "explicit"
    assert result["scope"]["source"] == "changed_files"
    assert result["scope"]["files_considered"] == 1
    assert "hint" not in result["scope"]
    assert CHANGE_SCOPE_HINT not in result.get("note", "")


@pytest.mark.parametrize("name", FILE_TOOLS)
def test_explicit_base_reports_an_explicit_scope(name: str, two_commits: Path) -> None:
    (two_commits / "service.py").write_text("def handle(x):\n    return 7\n", encoding="utf-8")
    _git(two_commits, "commit", "-q", "-am", "third")
    first = _git(two_commits, "rev-list", "--max-parents=0", "HEAD")

    result = _call(name, two_commits, base=first)

    assert result["status"] == "ok", result
    assert result["scope"]["kind"] == "explicit"
    assert result["scope"]["source"] == "base"
    assert result["scope"]["files_considered"] == 2
    assert "note" not in result or CHANGE_SCOPE_HINT not in result["note"]


def test_head_names_an_explicit_scope(two_commits: Path) -> None:
    head = _git(two_commits, "rev-parse", "HEAD")
    parent = _git(two_commits, "rev-parse", "HEAD~1")

    result = pr_blast_radius_func(repo_root=str(two_commits), head=head, base=parent)

    assert result["status"] == "ok", result
    assert result["scope"]["kind"] == "explicit"
    assert result["scope"]["source"] == "head"
    assert result["scope"]["head"] == head

    detect = detect_changes_func(repo_root=str(two_commits), head=head, base=parent)
    assert detect["status"] == "ok", detect
    assert detect["scope"]["kind"] == "explicit"
    assert detect["scope"]["source"] == "head"


def test_explicit_changed_files_with_no_diff_keep_their_answer(two_commits: Path) -> None:
    result = get_affected_flows_func(repo_root=str(two_commits), changed_files=[])

    assert result["summary"] == "No changed files detected."
    assert result["scope"]["kind"] == "explicit"


# --- (c) a repository with one commit -------------------------------------------------


@pytest.mark.parametrize("name", FILE_TOOLS)
def test_one_commit_repo_reviews_the_whole_commit(name: str, one_commit: Path) -> None:
    result = _call(name, one_commit)

    assert result["status"] == "ok", result
    scope = result["scope"]
    assert scope["kind"] == "last_commit"
    assert scope["head"] == _git(one_commit, "rev-parse", "HEAD")
    empty_tree = _git(one_commit, "hash-object", "-t", "tree", "/dev/null")
    assert scope["base"] == empty_tree
    assert scope["files_considered"] == 2  # service.py and .gitignore
    assert _hint_for(name) in result["note"]


def test_one_commit_pr_blast_radius_says_there_is_no_parent(one_commit: Path) -> None:
    result = pr_blast_radius_func(repo_root=str(one_commit))

    assert result["status"] == "ok"
    assert result["scope"]["kind"] == "last_commit"
    assert result["scope"]["analysed"] is False
    assert "parent" in result["summary"]
    assert CHANGE_SCOPE_HINT in result["note"]


# --- (d) no git history ----------------------------------------------------------------


@pytest.fixture
def no_git(tmp_path: Path) -> Path:
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "service.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    return _build(repo)


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    repo = _init(tmp_path / "empty")
    (repo / "service.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    return _build(repo)


@pytest.mark.parametrize("fixture", ["no_git", "empty_repo"])
@pytest.mark.parametrize("name", list(TOOLS))
def test_no_resolvable_range_is_a_clear_error_with_the_hint(
    name: str, fixture: str, request: pytest.FixtureRequest,
) -> None:
    repo = request.getfixturevalue(fixture)

    result = _call(name, repo)

    assert result["status"] == "error"
    assert "no commit range can be resolved" in result["error"]
    assert CHANGE_SCOPE_HINT in result["error"]
    assert "scope" not in result


# --- the tool entry points --------------------------------------------------------------


def test_the_server_tool_labels_the_scope_on_a_multi_repo_server(
    two_commits: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared server used to refuse a call naming no change; it now says what it reviewed."""
    import asyncio

    from code_review_graph import main as crg_main

    monkeypatch.setattr(crg_main, "_multi_repo", False)
    result = asyncio.run(crg_main.detect_changes_tool(repo_root=str(two_commits)))

    assert result["scope"]["kind"] == "last_commit"
    assert CHANGE_SCOPE_HINT in result["note"]
