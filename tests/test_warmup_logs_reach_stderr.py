"""Warm-up INFO lines reach stderr under ``serve``, which never configures root logging."""

from __future__ import annotations

import logging

from code_review_graph import cross_repo_warmup, main  # noqa: F401  (main installs the handler)


def test_warmup_info_is_written_when_the_process_never_configured_logging(capsys, monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    cross_repo_warmup.logger.info("Cross-repo warm-up: origin=%s %.1fs %s", "demo", 0.5, "ok")
    captured = capsys.readouterr()
    assert "INFO: Cross-repo warm-up: origin=demo 0.5s ok" in captured.err
    assert "warm-up" not in captured.out


def test_other_loggers_keep_their_level():
    assert logging.getLogger("code_review_graph.graph").level == logging.NOTSET
