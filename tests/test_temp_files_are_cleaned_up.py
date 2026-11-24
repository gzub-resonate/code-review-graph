"""A write that fails part-way leaves no temporary file behind."""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from code_review_graph import install_manifest
from code_review_graph.incremental import _WatchSupervisor


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


def test_a_manifest_write_that_fails_in_the_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "install-manifest.json"
    with pytest.raises(UnicodeEncodeError):
        install_manifest._replace(target, "\ud800 cannot be encoded")
    assert leftovers(tmp_path) == []
    assert not target.exists()


def test_a_manifest_write_whose_replace_fails_leaves_no_temp_file(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        install_manifest._replace(tmp_path / "install-manifest.json", "{}")
    assert leftovers(tmp_path) == []


def test_a_manifest_write_still_replaces_the_file(tmp_path):
    target = tmp_path / "install-manifest.json"
    install_manifest._replace(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"
    assert leftovers(tmp_path) == []


def test_a_watcher_health_write_whose_replace_fails_leaves_no_temp_file(tmp_path, monkeypatch):
    health = tmp_path / "health.json"
    supervisor = _WatchSupervisor(types.SimpleNamespace(), tmp_path, [], health_path=health)

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    supervisor.report_health(observer_alive=True, force=True)
    assert leftovers(tmp_path) == []
    assert not health.exists()
