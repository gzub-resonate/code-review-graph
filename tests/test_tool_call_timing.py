"""Every tool call logs one ``tool timing`` line, and a call over the slow limit logs a warning.

The line splits the wait for a worker slot (``queue_ms``) from the tool body (``work_ms``) and the
provenance stamp (``provenance_ms``), so a slow call can be told apart from a queued one.

The lines are read from stderr, not from pytest's log capture: ``serve`` starts without the CLI's
``logging.basicConfig``, so in production nothing but the timing logger's own handler writes them,
and a test that reads captured records would pass while the container log stayed empty.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from code_review_graph import main


def stderr_lines(capsys, marker):
    return [line for line in capsys.readouterr().err.splitlines() if marker in line]


def fields(line: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", line))


async def run(work, **kwargs):
    return await main._offload("demo_tool", work, None, provenance=False, **kwargs)


async def test_a_call_logs_its_stages(capsys):
    await run(lambda: (time.sleep(0.05), {"status": "ok"})[1])
    (line,) = stderr_lines(capsys, "tool timing")
    seen = fields(line)
    assert seen["tool"] == "demo_tool"
    assert seen["status"] == "ok"
    assert int(seen["work_ms"]) >= 50
    assert int(seen["total_ms"]) >= int(seen["work_ms"])
    assert line.startswith("INFO: ")


async def test_the_line_is_written_when_the_process_never_configured_logging(capsys, monkeypatch):
    # What `serve` looks like: no root handler and the root left at WARNING.
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    await run(lambda: {"status": "ok"})
    assert len(stderr_lines(capsys, "tool timing")) == 1


async def test_the_line_goes_to_stderr_and_not_to_stdout(capsys):
    await run(lambda: {"status": "ok"})
    captured = capsys.readouterr()
    assert "tool timing" in captured.err
    assert "tool timing" not in captured.out


async def test_an_error_reply_is_logged_as_an_error(capsys):
    await run(lambda: {"status": "error", "error": "boom"})
    (line,) = stderr_lines(capsys, "tool timing")
    assert fields(line)["status"] == "error"


async def test_a_call_that_raises_is_still_timed(capsys):
    def fail() -> dict:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await run(fail)
    (line,) = stderr_lines(capsys, "tool timing")
    assert fields(line)["status"] == "raised"


async def test_a_call_over_the_slow_limit_is_a_warning(capsys, monkeypatch):
    monkeypatch.setenv("CRG_SLOW_TOOL_SECONDS", "1")
    await run(lambda: (time.sleep(1.05), {"status": "ok"})[1])
    (line,) = stderr_lines(capsys, "slow tool")
    assert line.startswith("WARNING: ")
    assert int(fields(line)["total_ms"]) >= 1000


async def test_a_timed_out_call_is_logged_as_a_timeout(capsys, monkeypatch):
    monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
    reply = await run(lambda: (time.sleep(1.5), {"status": "ok"})[1])
    assert reply["status"] == "error"
    (line,) = stderr_lines(capsys, "tool timing")
    assert fields(line)["status"] == "timeout"


def test_the_timing_logger_does_not_depend_on_the_root_logger():
    assert main.timing_logger.handlers
    assert main.timing_logger.propagate is False
    assert main.timing_logger.level == logging.INFO


# ---- argument summary -------------------------------------------------------------------------


async def _fake_tool(pattern: str, target: str, cursor=None, api_token=None, limit=10, files=None):
    return await main._offload("fake_tool", lambda: {"status": "ok"}, None, provenance=False)


async def test_the_line_summarises_the_tool_arguments_and_hides_secrets(capsys):
    await _fake_tool("callers_of", "a" * 100, cursor="opaque-secret-cursor", api_token="hunter2",
                     files=["x", "y"])
    (line,) = stderr_lines(capsys, "tool timing")
    assert "hunter2" not in line and "opaque-secret-cursor" not in line
    summary = line.split(" args=", 1)[1]
    assert " " not in summary
    assert "pattern=callers_of" in summary
    assert "target=" + "a" * 40 + "," in summary  # cut at 40 characters
    assert "cursor=<set>" in summary and "api_token=<redacted>" in summary
    assert "limit=10" in summary and "files=[2]" in summary


def test_summary_is_capped_and_new_fields_come_last():
    assert len(main.summarize_args({f"k{i}": "v" * 30 for i in range(30)})) == 200
    assert main.summarize_args({}) == "-"


async def test_existing_fields_keep_their_order(capsys):
    await run(lambda: {"status": "ok"})
    (line,) = stderr_lines(capsys, "tool timing")
    names = re.findall(r"(\w+)=", line)
    assert names[:8] == [
        "tool", "repo_root", "status", "total_ms", "queue_ms", "work_ms", "provenance_ms",
        "caches",
    ]
    assert names[-1] == "args"
