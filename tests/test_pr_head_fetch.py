"""A pull request's commits are fetched on demand, named in errors, and cleaned up after.

The server's checkout is a single-branch clone of the default branch, so a commit inside a pull
request, or its base branch, is not in it. These tests use that shape: a bare origin with a
stacked pull request, and a clone that fetched only ``main``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from code_review_graph import pr_blast_radius as pbr
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.tools.pr_tools import pr_blast_radius_func
from code_review_graph.tools.review import detect_changes_func

from .test_pr_blast_radius import SERVER_BASE, git, registry, write  # noqa: F401

STEP_ONE = SERVER_BASE.replace("return [];", "return [0];")
STEP_TWO = STEP_ONE.replace("return 1;", "return 2;")
STEP_THREE = STEP_TWO.replace("return [0];", "return [1];")
TIP = STEP_THREE + "function brandNew() {\n  return 3;\n}\n"


class Shape:
    def __init__(self, server: Path, shas: dict[str, str], scratch: Path) -> None:
        self.server = server
        self.shas = shas
        self.scratch = scratch


@pytest.fixture
def shape(tmp_path, registry, monkeypatch):  # noqa: F811
    """origin (main, a parent branch, a PR of three commits on it) and a single-branch clone."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    work = (tmp_path / "work").resolve()
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    write(work, "server.js", SERVER_BASE)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    shas = {"main": git(work, "rev-parse", "HEAD")}
    git(work, "checkout", "-q", "-b", "parent")
    write(work, "server.js", STEP_ONE)
    git(work, "commit", "-q", "-am", "parent")
    shas["parent"] = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "-b", "pr")
    for name, text in (("c1", STEP_TWO), ("c2", STEP_THREE), ("c3", TIP)):
        write(work, "server.js", text)
        git(work, "commit", "-q", "-am", name)
        shas[name] = git(work, "rev-parse", "HEAD")
    origin = (tmp_path / "origin.git").resolve()
    git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(origin, "update-ref", "refs/pull/1/head", shas["c3"])
    server = (tmp_path / "server").resolve()
    # --no-local: a local clone would hardlink every object, including the pull request's.
    git(tmp_path, "clone", "-q", "--no-local", "--single-branch", "--branch", "main",
        str(origin), str(server))
    with GraphStore(get_db_path(server)) as store:
        full_build(server, store)
    return Shape(server, shas, scratch)


def tool(shape: Shape, **args):
    args.setdefault("include_external", False)
    return pr_blast_radius_func(repo_root=str(shape.server), **args)


def object_counts(repo: Path) -> dict[str, str]:
    out = git(repo, "count-objects", "-v")
    return dict(line.split(": ", 1) for line in out.splitlines())


def test_the_checkout_really_lacks_the_commit_and_the_branch(shape):
    with pytest.raises(subprocess.CalledProcessError):
        git(shape.server, "cat-file", "-e", shape.shas["c1"])
    with pytest.raises(subprocess.CalledProcessError):
        git(shape.server, "rev-parse", "--verify", "parent")


def test_a_head_commit_inside_the_pull_request_but_not_at_its_tip_is_fetched(shape):
    result = tool(shape, head=shape.shas["c1"], base="main")
    assert result["status"] == "ok"
    assert [f["file"] for f in result["files"]] == ["server.js"]


def test_the_tip_and_a_mid_commit_give_different_answers(shape):
    mid = tool(shape, head=shape.shas["c1"], base="main")
    tip = tool(shape, head=shape.shas["c3"], base="main")
    assert tip["totals"]["symbols_added"] > mid["totals"]["symbols_added"]


def test_a_stacked_pull_request_with_an_explicit_base_is_fetched_and_noted(shape):
    result = tool(shape, pr=1, base="parent")
    assert result["status"] == "ok"
    assert any("sits on top of other changes" in note for note in result["notes"])


def test_an_explicit_base_branch_the_checkout_lacks_is_fetched_for_a_head_sha(shape):
    result = tool(shape, head=shape.shas["c3"], base="parent")
    assert result["status"] == "ok"
    assert result["totals"]["files"] == 1


def test_an_unresolvable_base_names_the_ref_and_says_the_fetch_failed(shape):
    result = tool(shape, head=shape.shas["c1"], base="no-such-branch")
    assert result["status"] == "error"
    assert "base" in result["error"]
    assert "'no-such-branch'" in result["error"]
    assert "could not be fetched from origin" in result["error"]
    assert "no detail" not in result["error"]


def test_an_unknown_head_sha_names_the_ref(shape):
    unknown = "0" * 40
    result = tool(shape, head=unknown, base="main")
    assert result["status"] == "error"
    assert unknown in result["error"]
    assert "head" in result["error"]
    assert "no detail" not in result["error"]


def test_an_abbreviated_head_sha_the_checkout_lacks_asks_for_the_full_one(shape):
    result = tool(shape, head=shape.shas["c1"][:10], base="main")
    assert result["status"] == "error"
    assert "full 40-character SHA" in result["error"]


def test_a_resolution_error_carries_gits_own_message(shape):
    with pytest.raises(pbr.PullRequestError) as raised:
        pbr.resolve_commit(str(shape.server), "no-such-ref", "base")
    assert "base 'no-such-ref'" in str(raised.value)
    assert "fatal" in str(raised.value)


def test_a_pull_request_number_still_works_with_the_default_base(shape):
    result = tool(shape, pr=1, base="main")
    assert result["status"] == "ok"
    assert result["totals"]["files"] == 1


def test_detect_changes_takes_a_head_sha_and_an_unfetched_base(shape):
    result = detect_changes_func(
        head=shape.shas["c3"], base="parent", repo_root=str(shape.server),
    )
    assert result["status"] == "ok"
    assert result["pull_request"]["head"] == shape.shas["c3"][:12]
    assert result["pull_request"]["base"] == shape.shas["parent"][:12]


def test_detect_changes_names_an_unresolvable_base(shape):
    result = detect_changes_func(
        head=shape.shas["c3"], base="no-such-branch", repo_root=str(shape.server),
    )
    assert result["status"] == "error"
    assert "'no-such-branch'" in result["error"]


def test_nothing_is_left_in_the_checkout_or_the_temp_directory(shape):
    before = object_counts(shape.server)
    refs_before = git(shape.server, "for-each-ref")
    assert tool(shape, pr=1, base="parent")["status"] == "ok"
    assert tool(shape, head=shape.shas["c1"], base="main")["status"] == "ok"
    assert tool(shape, head=shape.shas["c3"], base="no-such-branch")["status"] == "error"
    assert object_counts(shape.server) == before
    assert git(shape.server, "for-each-ref") == refs_before
    assert git(shape.server, "for-each-ref", "refs/crg") == ""
    assert os.listdir(shape.scratch) == []


def test_the_scratch_directory_is_removed_even_when_the_call_fails(shape):
    with pytest.raises(pbr.PullRequestError):
        with pbr.scratch_session(str(shape.server)):
            pbr.ensure_commit(str(shape.server), shape.shas["c1"], "head")
            assert os.listdir(shape.scratch)
            raise pbr.PullRequestError("boom")
    assert os.listdir(shape.scratch) == []


def test_refs_an_earlier_version_left_in_the_checkout_are_removed(shape):
    git(shape.server, "update-ref", "refs/crg/pull/9", shape.shas["main"])
    git(shape.server, "update-ref", "refs/crg/base/9", shape.shas["main"])
    assert tool(shape, head=shape.shas["c1"], base="main")["status"] == "ok"
    assert git(shape.server, "for-each-ref", "refs/crg") == ""


def test_scratch_directories_a_killed_call_left_behind_are_swept(shape):
    old = shape.scratch / "crg-pr-old"
    fresh = shape.scratch / "crg-pr-fresh"
    other = shape.scratch / "unrelated"
    for folder in (old, fresh, other):
        folder.mkdir()
    long_ago = time.time() - pbr.SCRATCH_MAX_AGE - 60
    os.utime(old, (long_ago, long_ago))
    assert tool(shape, head=shape.shas["c1"], base="main")["status"] == "ok"
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


def test_a_failed_load_is_logged_with_the_arguments(shape, caplog):
    with caplog.at_level(logging.WARNING, logger="code_review_graph.tools.pr_tools"):
        tool(shape, head=shape.shas["c1"], base="no-such-branch")
    text = caplog.text
    assert "pull request load failed" in text
    assert "no-such-branch" in text
    assert shape.shas["c1"] in text


def test_a_tool_error_reply_is_logged_by_name(caplog):
    from code_review_graph.main import _log_tool_error

    with caplog.at_level(logging.WARNING, logger="code_review_graph.main"):
        _log_tool_error("pr_blast_radius_tool", "/repo", {"status": "error", "error": "bad ref"})
        _log_tool_error("pr_blast_radius_tool", "/repo", {"status": "ok"})
    assert caplog.text.count("tool error") == 1
    assert "pr_blast_radius_tool" in caplog.text
    assert "bad ref" in caplog.text


def test_control_characters_in_a_logged_ref_are_escaped(shape, caplog):
    with caplog.at_level(logging.WARNING, logger="code_review_graph.tools.pr_tools"):
        tool(shape, head="main", base="x\nFAKE LOG LINE")
    assert "\nFAKE LOG LINE" not in caplog.text
