"""Every tool call logs one ``tool timing`` line, and a call over the slow limit logs a warning.

The line splits the wait for a worker slot (``queue_ms``) from the tool body (``work_ms``) and the
provenance stamp (``provenance_ms``), so a slow call can be told apart from a queued one.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from code_review_graph import main


def fields(record: logging.LogRecord) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))


async def run(work, **kwargs):
    return await main._offload("demo_tool", work, None, provenance=False, **kwargs)


async def test_a_call_logs_its_stages(caplog):
    caplog.set_level(logging.INFO, logger=main.logger.name)
    await run(lambda: (time.sleep(0.05), {"status": "ok"})[1])
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    seen = fields(record)
    assert seen["tool"] == "demo_tool"
    assert seen["status"] == "ok"
    assert int(seen["work_ms"]) >= 50
    assert int(seen["total_ms"]) >= int(seen["work_ms"])
    assert record.levelno == logging.INFO


async def test_an_error_reply_is_logged_as_an_error(caplog):
    caplog.set_level(logging.INFO, logger=main.logger.name)
    await run(lambda: {"status": "error", "error": "boom"})
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    assert fields(record)["status"] == "error"


async def test_a_call_that_raises_is_still_timed(caplog):
    caplog.set_level(logging.INFO, logger=main.logger.name)

    def fail() -> dict:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await run(fail)
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    assert fields(record)["status"] == "raised"


async def test_a_call_over_the_slow_limit_is_a_warning(caplog, monkeypatch):
    monkeypatch.setenv("CRG_SLOW_TOOL_SECONDS", "1")
    caplog.set_level(logging.INFO, logger=main.logger.name)
    await run(lambda: (time.sleep(1.05), {"status": "ok"})[1])
    (record,) = [r for r in caplog.records if "slow tool" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert int(fields(record)["total_ms"]) >= 1000


async def test_a_timed_out_call_is_logged_as_a_timeout(caplog, monkeypatch):
    monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
    caplog.set_level(logging.INFO, logger=main.logger.name)
    reply = await run(lambda: (time.sleep(1.5), {"status": "ok"})[1])
    assert reply["status"] == "error"
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    assert fields(record)["status"] == "timeout"
