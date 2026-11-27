"""The Git-state warm-up: run the change-discovery Git commands once per repo after a start.

A fresh task has a cold disk cache, and the first ``git diff`` against the base on a large repo
took 17.7 s (0.05 s the next time), longer than the discovery timeout, so a client's first
change-tool call failed with "git timed out". The warm-up pays that cost before a client asks.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from code_review_graph import cross_repo_warmup as warm
from code_review_graph.registry import Registry


def _repo(base: Path, name: str, *, git: bool = True) -> str:
    root = base / name
    root.mkdir()
    if git:
        (root / ".git").mkdir()
    return str(root)


class _FakeRun:
    """Records each call; optionally raises for a path."""

    def __init__(self, clock=None, step: float = 0.0, raises: dict | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], str, dict]] = []
        self._clock, self._step = clock, step
        self._raises = raises or {}

    def __call__(self, args, **kwargs):
        self.calls.append((tuple(args), kwargs["cwd"], kwargs))
        if self._clock is not None:
            self._clock[0] += self._step
        error = self._raises.get(kwargs["cwd"])
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, 0, b"", b"")


def test_each_git_repo_gets_both_discovery_commands_and_other_paths_are_skipped(tmp_path):
    a, plain, c = _repo(tmp_path, "a"), _repo(tmp_path, "plain", git=False), _repo(tmp_path, "c")
    run = _FakeRun()
    report = warm.warm_git_state([a, plain, c], max_seconds=60, run=run)
    assert [(path, outcome) for path, _s, outcome in report] == [(a, "ok"), (c, "ok")]
    assert [(args, cwd) for args, cwd, _k in run.calls] == [
        (command, repo) for repo in (a, c) for command in warm.GIT_STATE_COMMANDS
    ]
    assert all(kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0" for _a, _c, kwargs in run.calls)
    assert all(kwargs["stdin"] == subprocess.DEVNULL for _a, _c, kwargs in run.calls)


def test_the_commands_are_the_ones_change_discovery_runs():
    diff, untracked = warm.GIT_STATE_COMMANDS
    assert diff[:5] == ("git", "diff", "--relative", "--name-status", "-z")
    assert untracked[:3] == ("git", "ls-files", "--others")


def test_a_timeout_or_a_missing_git_ends_that_repo_and_the_next_is_still_tried(tmp_path):
    a, b, c = (_repo(tmp_path, name) for name in "abc")
    run = _FakeRun(raises={
        a: subprocess.TimeoutExpired("git", 1), b: FileNotFoundError("git"),
    })
    report = warm.warm_git_state([a, b, c], max_seconds=60, run=run)
    assert [outcome for _p, _s, outcome in report] == ["timeout", "failed: FileNotFoundError", "ok"]
    # a failed first command ends the repo: one call for a and b, both for c
    assert len(run.calls) == 1 + 1 + len(warm.GIT_STATE_COMMANDS)


def test_the_cap_stops_starting_repos(tmp_path):
    repos = [_repo(tmp_path, name) for name in "abcd"]
    now = [0.0]
    run = _FakeRun(clock=now, step=10.0)
    report = warm.warm_git_state(
        repos, max_seconds=15, run=run, clock=lambda: now[0],
    )
    # the first repo takes 20 s of fake time, so the cap is spent before the second starts
    assert [path for path, _s, _o in report] == [repos[0]]


def test_a_set_stop_event_starts_nothing(tmp_path):
    stop = threading.Event()
    stop.set()
    run = _FakeRun()
    assert warm.warm_git_state([_repo(tmp_path, "a")], max_seconds=60, stop=stop, run=run) == []
    assert run.calls == []


def test_against_a_real_repository_it_succeeds_and_changes_nothing(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, env=env, capture_output=True)

    git("init", "-q")
    (root / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    (root / "a.txt").write_text("two\n")
    git("commit", "-q", "-am", "second")
    (root / "new.txt").write_text("untracked\n")
    index_before = (root / ".git" / "index").read_bytes()
    report = warm.warm_git_state([str(root)], max_seconds=60)
    assert [outcome for _p, _s, outcome in report] == ["ok"]
    assert (root / ".git" / "index").read_bytes() == index_before
    assert not (root / ".git" / "index.lock").exists()


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    registry = Registry()
    paths = []
    for name in ("one", "two"):
        path = _repo(tmp_path, name)
        registry.register(path, alias=name)
        paths.append(str(Path(path).resolve()))
    return paths


def test_it_does_nothing_unless_switched_on(registered, monkeypatch):
    monkeypatch.delenv(warm.GIT_WARMUP_ENV, raising=False)
    assert warm.start_git_warmup() is None


def test_switched_on_it_warms_every_registered_repo_in_a_background_thread(
    registered, monkeypatch,
):
    seen: list[list[str]] = []
    monkeypatch.setattr(warm, "warm_git_state", lambda paths, **_k: seen.append(list(paths)) or [])
    monkeypatch.setenv(warm.GIT_WARMUP_ENV, "1")
    thread = warm.start_git_warmup()
    assert thread is not None and thread.daemon and thread.name == "crg-git-warmup"
    thread.join(5)
    assert seen == [registered]


def test_an_error_inside_the_warm_up_never_reaches_the_server(registered, monkeypatch):
    def boom(paths, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(warm, "warm_git_state", boom)
    monkeypatch.setenv(warm.GIT_WARMUP_ENV, "1")
    thread = warm.start_git_warmup()
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


def test_an_empty_registry_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(warm.GIT_WARMUP_ENV, "1")
    assert warm.start_git_warmup() is None
