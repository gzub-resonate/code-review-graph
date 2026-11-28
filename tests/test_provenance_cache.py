"""The ``_graph`` stamp is cached per graph file state and HEAD state, not rebuilt per call."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import code_review_graph.tools._common as common
from code_review_graph import provenance_cache
from code_review_graph.graph import GraphStore


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    graph_dir = root / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    store.set_metadata("last_updated", "2026-07-17T12:00:00+00:00")
    store.set_metadata("git_head_sha", "abc")
    store.commit()
    store.close()
    provenance_cache.clear()
    return root


@pytest.fixture
def counters(monkeypatch):
    seen = {"sqlite": 0, "git": 0}
    real_connect, real_run = sqlite3.connect, subprocess.run

    def connect(*a, **k):
        if k.get("uri"):  # the provenance read opens ``file:...?mode=ro``; GraphStore does not
            seen["sqlite"] += 1
        return real_connect(*a, **k)

    def run(*a, **k):
        if a and a[0][:1] == ["git"] and "rev-parse" in a[0]:
            seen["git"] += 1
        return real_run(*a, **k)

    monkeypatch.setattr(common.sqlite3, "connect", connect)
    monkeypatch.setattr(common.subprocess, "run", run)
    return seen


def test_second_call_touches_neither_sqlite_nor_git(repo, counters):
    first = provenance_cache.cached_graph_provenance(str(repo))
    assert first and "head_sha" in first
    assert counters == {"sqlite": 1, "git": 1}
    second = provenance_cache.cached_graph_provenance(str(repo))
    assert counters == {"sqlite": 1, "git": 1}
    assert second["head_sha"] == first["head_sha"]
    assert second["updated_at"] == first["updated_at"]


def test_age_is_recomputed_on_a_hit(repo, counters):
    provenance_cache.cached_graph_provenance(str(repo))
    again = provenance_cache.cached_graph_provenance(str(repo))
    assert counters["sqlite"] == 1
    assert again["age_seconds"] > 0


def test_a_new_commit_invalidates(repo, counters):
    before = provenance_cache.cached_graph_provenance(str(repo))
    (repo / "a.txt").write_text("b")
    _git(repo, "commit", "-q", "-am", "two")
    after = provenance_cache.cached_graph_provenance(str(repo))
    assert counters == {"sqlite": 2, "git": 2}
    assert after["head_sha"] != before["head_sha"]


def test_a_checkout_invalidates(repo, counters):
    provenance_cache.cached_graph_provenance(str(repo))
    _git(repo, "checkout", "-q", "-b", "other")
    provenance_cache.cached_graph_provenance(str(repo))
    assert counters["git"] == 2


def test_a_changed_graph_file_invalidates(repo, counters):
    provenance_cache.cached_graph_provenance(str(repo))
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    store.set_metadata("git_head_sha", "def")
    store.commit()
    store.close()
    db = repo / ".code-review-graph" / "graph.db"
    os.utime(db, ns=(1, db.stat().st_mtime_ns + 5_000_000_000))
    changed = provenance_cache.cached_graph_provenance(str(repo))
    assert counters["sqlite"] == 2
    assert changed["built_at_sha"] == "def"


def test_a_failed_read_is_not_cached(repo, counters, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(common.sqlite3, "connect", boom)
    assert provenance_cache.cached_graph_provenance(str(repo)) is None
    monkeypatch.undo()
    recovered = provenance_cache.cached_graph_provenance(str(repo))
    assert recovered and "head_sha" in recovered


def test_a_failed_git_read_is_not_cached(repo, counters, monkeypatch):
    real_run = subprocess.run

    def broken(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(common.subprocess, "run", broken)
    partial = provenance_cache.cached_graph_provenance(str(repo))
    assert partial and "head_sha" not in partial
    monkeypatch.setattr(common.subprocess, "run", real_run)
    full = provenance_cache.cached_graph_provenance(str(repo))
    assert "head_sha" in full


def test_with_provenance_stamps_once_and_keeps_existing_graph_key(repo):
    assert "_graph" in provenance_cache.with_provenance({"status": "ok"}, str(repo))
    keep = {"_graph": {"x": 1}}
    assert provenance_cache.with_provenance(keep, str(repo)) == {"_graph": {"x": 1}}


def test_main_resolves_the_cached_stamp():
    import code_review_graph.main as main

    main.__dict__.pop("with_provenance", None)
    assert main.__getattr__("with_provenance") is provenance_cache.with_provenance
