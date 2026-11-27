"""The cached route functions answer like the originals, and the timing log reports cache use."""

from __future__ import annotations

import logging
import re

import pytest

from code_review_graph import feign_links as fl
from code_review_graph import main
from code_review_graph import query_cache as qc

ROUTES = [
    "/v1/users/{id}", "v1//users/{id:[0-9]{2}}?x=1", "/", "", "/A/b/{c}/", "/a#frag",
    "/v2/items/{}", "/Users/{id}/Orders",
]


@pytest.mark.parametrize("route", ROUTES)
def test_the_cached_functions_answer_like_the_originals(route):
    assert qc.normalize_route(route) == fl.normalize_route(route)
    shape = fl.normalize_route(route)
    assert qc.segments(shape) == fl._segments(shape)


@pytest.mark.parametrize("client", ["GET", "POST", "", "ANY", "get"])
@pytest.mark.parametrize("server", ["GET", "POST", "", "ANY"])
def test_the_cached_method_match_answers_like_the_original(client, server):
    assert qc.method_matches(client, server) == fl._method_matches(client, server)


def test_a_delta_counts_only_the_hits_between_two_snapshots():
    qc.normalize_route("/delta/one")
    before = qc.snapshot()
    qc.normalize_route("/delta/one")
    qc.normalize_route("/delta/two")
    used = qc.delta(before, qc.snapshot())
    hits, misses, size = used["normalize_route"]
    assert (hits, misses) == (1, 1)
    assert size >= 2
    assert "segments" not in used


def test_an_unused_cache_is_left_out_and_an_empty_delta_prints_a_dash():
    now = qc.snapshot()
    assert qc.delta(now, now) == {}
    assert qc.format_delta({}) == "-"
    assert qc.format_delta({"b": (2, 1, 3), "a": (0, 4, 4)}) == "a:0/4/4,b:2/1/3"


def test_the_existing_lru_caches_are_registered():
    names = set(qc.snapshot())
    assert {"normalize_route", "segments", "method_matches"} <= names
    assert any(name.endswith("path_segments") for name in names)


async def test_the_timing_line_names_the_caches_a_call_used(caplog):
    caplog.set_level(logging.INFO, logger=main.logger.name)

    def work() -> dict:
        qc.normalize_route("/timing/line/{x}")
        qc.normalize_route("/timing/line/{x}")
        return {"status": "ok"}

    await main._offload("demo_tool", work, None, provenance=False)
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    caches = dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))["caches"]
    assert re.search(r"normalize_route:\d+/\d+/\d+", caches)


async def test_a_call_that_used_no_cache_logs_a_dash(caplog):
    caplog.set_level(logging.INFO, logger=main.logger.name)
    await main._offload("demo_tool", lambda: {"status": "ok"}, None, provenance=False)
    (record,) = [r for r in caplog.records if "tool timing" in r.getMessage()]
    assert record.getMessage().endswith("caches=-")
