"""The cross-repo time budget is checked inside one repo's work, not only before each repo.

``CRG_CROSS_REPO_BUDGET_SECONDS`` used to be read before each client repo, so one big repo (the
all-repo index of the JavaScript join, a long join, the owner lookup of an infrastructure
origin, an in-flight search) could run past it, and behind a load balancer an overrun is a
dropped connection. These tests drive the budget with a clock they control: it reads as
"not yet" until a step they choose has run, then as "spent". A repeat of the call must finish
the work from what the first call kept and give the answer an unbudgeted call gives.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from code_review_graph import constants, repo_cache
from code_review_graph import feign_links as fl
from code_review_graph import not_found_reasons as nfr
from code_review_graph import service_aliases as sa
from code_review_graph import stepfunction_links as sl
from code_review_graph import xrepo_fast as xf
from code_review_graph.registry import Registry
from code_review_graph.repo_cache import _reset_shared_pool
from code_review_graph.tools import registry_tools
from code_review_graph.tools.registry_tools import (
    cross_repo_requests_func,
    cross_repo_search_func,
)
from code_review_graph.xrepo_budget import BudgetSpent, Deadline

from .test_asl_cross_repo_links import LAMBDAS, MACHINES, PLATFORM
from .test_asl_cross_repo_links import make_repo as make_asl_repo
from .test_cross_repo_ranking_budget import named_repo
from .test_cross_repo_requests_paging import SERVER, client_file, make_repo


class FlipClock:
    """Reads ``0`` until :meth:`spend` is called, then ``1000``: a budget spent on demand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def spend(self) -> None:
        self.now = 1000.0


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "100")
    repo_cache.MEMO.clear()
    repo_cache.SERVER_INDEXES.clear()
    registry_tools._JOINS_CUT.clear()
    return Registry()


@pytest.fixture
def clock(monkeypatch):
    fake = FlipClock()
    monkeypatch.setattr(registry_tools, "_now", fake)
    return fake


def rows_of(reply):
    return [(r["client_repo"], r["client_file"], r["client_line"], r["status"])
            for r in reply["results"]]


# --- the Feign / JS join ---------------------------------------------------------------------


def test_a_budget_spent_in_the_middle_of_a_repo_skips_that_repo_whole(
    tmp_path, registry, clock, monkeypatch,
):
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1", 12)})
    make_repo(tmp_path, registry, "ui2", {"lib/a.ts": client_file("ui2", 12)})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    expected = cross_repo_requests_func(origin="js", max_results=100)
    assert expected["skipped_repos_total"] == 0
    monkeypatch.setattr(registry_tools, "_JOIN_CHECK_EVERY", 1)
    seen = {"calls": 0}
    real = fl.unmatchable_reason

    def spend_on_the_fifth_call(call):
        seen["calls"] += 1
        if seen["calls"] == 5:
            clock.spend()
        return real(call)

    monkeypatch.setattr(fl, "unmatchable_reason", spend_on_the_fifth_call)
    cut = cross_repo_requests_func(origin="js", max_results=100)
    # The first repo's join was cut off: it is named, and none of its calls are counted.
    assert "ui1" in cut["skipped_repos"]
    assert cut["truncated"] is True
    assert cut["total"] == sum(cut["counts"].values())
    assert all(row[0] not in cut["skipped_repos"] for row in rows_of(cut))
    assert cut["resumes_skipped_on_repeat"] is True
    # The repeat (the clock reads "not yet" again) finishes the call the unbudgeted way.
    clock.now = 0.0
    monkeypatch.setattr(fl, "unmatchable_reason", real)
    again = cross_repo_requests_func(origin="js", max_results=100)
    assert again["skipped_repos_total"] == 0
    assert rows_of(again) == rows_of(expected)
    assert again["counts"] == expected["counts"]


def test_a_repo_whose_join_was_cut_is_not_cut_again(
    tmp_path, registry, clock, monkeypatch,
):
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1", 12)})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    monkeypatch.setattr(registry_tools, "_JOIN_CHECK_EVERY", 1)
    real = fl.unmatchable_reason

    def always_spent(call):
        clock.spend()
        return real(call)

    monkeypatch.setattr(fl, "unmatchable_reason", always_spent)
    first = cross_repo_requests_func(origin="js", max_results=100)
    assert "ui1" in first["skipped_repos"]
    # Still out of budget on the repeat: ui1 already lost one join, so it may finish.
    second = cross_repo_requests_func(origin="js", max_results=100)
    assert second["skipped_repos_total"] < first["skipped_repos_total"] or (
        "ui1" not in second["skipped_repos"]
    )
    assert second["total"] > 0


def test_the_all_repo_index_stops_at_the_budget_and_the_repeat_goes_on(
    tmp_path, registry, clock, monkeypatch,
):
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1", 6)})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    make_repo(tmp_path, registry, "api2", {"server.js": SERVER})
    expected = cross_repo_requests_func(origin="js", repos=["ui1"], max_results=100)
    assert expected["skipped_repos_total"] == 0
    repo_cache.MEMO.clear()
    repo_cache.SERVER_INDEXES.clear()
    reads: list[str] = []
    real = registry_tools._load_endpoints_uncached

    def counting(entry):
        reads.append(Path(entry["path"]).name)
        if len(reads) == 1:  # the client repo's own read: the budget is gone after it
            found = real(entry)
            clock.spend()
            return found
        return real(entry)

    monkeypatch.setattr(registry_tools, "_load_endpoints_uncached", counting)
    cut = cross_repo_requests_func(origin="js", repos=["ui1"], max_results=100)
    assert cut["skipped_repos"] == ["ui1"]
    assert reads == ["ui1"]  # no other repo was read once the budget was gone
    clock.now = 0.0
    again = cross_repo_requests_func(origin="js", repos=["ui1"], max_results=100)
    assert again["skipped_repos_total"] == 0
    assert rows_of(again) == rows_of(expected)
    assert reads.count("ui1") == 1  # kept from the first call


def test_a_wide_call_with_no_budget_is_not_limited(tmp_path, registry, clock, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1", 6)})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    clock.spend()
    reply = cross_repo_requests_func(origin="js", max_results=100)
    assert reply["skipped_repos_total"] == 0
    assert reply["total"] == 6


# --- an infrastructure origin ----------------------------------------------------------------


def test_the_owner_lookup_of_an_infrastructure_origin_stops_at_the_budget(
    tmp_path, registry, clock, monkeypatch,
):
    make_asl_repo(tmp_path, registry, "machines", MACHINES)
    make_asl_repo(tmp_path, registry, "lambdas", LAMBDAS)
    make_asl_repo(tmp_path, registry, "platform", PLATFORM)
    expected = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
    assert expected["results"]
    assert "skipped_repos" not in expected
    repo_cache.MEMO.clear()
    real = sl.read_target_edges

    def read_then_spend(store):
        found = real(store)
        clock.spend()
        return found

    monkeypatch.setattr(sl, "read_target_edges", read_then_spend)
    cut = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
    # No owner can be named without the other repos' definitions: nothing is answered half.
    assert cut["results"] == []
    assert cut["skipped_repos"] == ["machines"]
    assert cut["truncated"] is True
    clock.now = 0.0
    monkeypatch.setattr(sl, "read_target_edges", real)
    again = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
    assert "skipped_repos" not in again
    assert again["results"] == expected["results"]
    assert again["counts"] == expected["counts"]


# --- a search stopped inside a repo ----------------------------------------------------------


def test_a_search_the_budget_stops_mid_query_is_skipped_and_retried_first(
    tmp_path, registry, clock, monkeypatch,
):
    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, "1")
    _reset_shared_pool()
    names = ["getMetricsBatchV1", "getMetricsBatchV2", "getMetricsBatch"]
    for alias in ("a", "b", "c"):
        named_repo(tmp_path, registry, alias, names)
    expected = cross_repo_search_func(query="getMetricsBatch", max_results=100)
    assert expected["skipped_repos"] == []
    monkeypatch.setattr(registry_tools, "_PROGRESS_STEPS", 1)
    real = registry_tools._search_one_repo

    def spend_on_second(repo_index, entry, query, kind, limit, deadline=None):
        if deadline is not None:  # every repo but the first one gets the deadline
            clock.spend()
        return real(repo_index, entry, query, kind, limit, deadline)

    monkeypatch.setattr(registry_tools, "_search_one_repo", spend_on_second)
    cut = cross_repo_search_func(query="getMetricsBatch", max_results=100)
    assert cut["skipped_repos"] == ["b", "c"]
    assert cut["truncated"] is True
    assert {row["repo"] for row in cut["results"]} >= {"a"}
    clock.now = 0.0
    monkeypatch.setattr(registry_tools, "_search_one_repo", real)
    again = cross_repo_search_func(query="getMetricsBatch", max_results=100)
    assert again["skipped_repos"] == []
    assert [(r["repo"], r["qualified_name"]) for r in again["results"]] == [
        (r["repo"], r["qualified_name"]) for r in expected["results"]
    ]


# --- the pieces --------------------------------------------------------------------------------


def test_deadline_without_a_budget_never_runs_out():
    ticks = iter(range(10**6))
    deadline = Deadline(0, lambda: float(next(ticks)))
    assert deadline.spent() is False
    assert deadline.remaining() is None
    deadline.check()


def test_deadline_runs_out_on_the_given_clock():
    clock = FlipClock()
    deadline = Deadline(5, clock)
    assert deadline.remaining() == 5.0
    deadline.check()
    clock.spend()
    assert deadline.spent() is True
    assert deadline.remaining() == 0.0
    with pytest.raises(BudgetSpent):
        deadline.check()


def test_facts_prefetch_with_a_spent_budget_raises_and_keeps_what_it_read(
    tmp_path, registry,
):
    for name in ("one", "two", "three"):
        named_repo(tmp_path, registry, name, ["f"])
    entries = Registry().list_repos()
    facts = sa.FactsCache(entries)
    calls = {"n": 0}

    def after_first_batch():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(BudgetSpent):
        facts.prefetch(range(3), 1, after_first_batch)
    assert len(facts._facts) == 1
    facts.prefetch(range(3), 1, lambda: False)
    assert len(facts._facts) == 3


# --- the route-keyed join gives the old answers ------------------------------------------------


def _random_world(rnd: random.Random):
    parts = ["users", "Users", "orders", "{id}", "{orderId:[0-9]+}", "v1", "V1", "x", ""]
    methods = ["GET", "POST", "get", "*", "ANY", "PUT"]

    def route():
        segments = [rnd.choice(parts) for _ in range(rnd.randint(1, 3))]
        return rnd.choice(["/", "", "//"]) + "/".join(segments) + rnd.choice(["", "/", "?q=1"])

    def server(repo, i):
        return fl.ServerEndpoint(
            repo=repo, file=f"{repo}/C{i % 3}.java", line=i, http_method=rnd.choice(methods),
            route=route(), origin=f"/p/{repo}", catch_all=rnd.random() < 0.1,
        )

    pools = {
        name: [server(name, i) for i in range(rnd.randint(0, 25))] for name in ("a", "b", "c")
    }
    calls = [
        fl.ClientCall(
            repo="me", file="Me.java", line=i, http_method=rnd.choice(methods),
            route=route(), service="a", origin="feign", route_dynamic=rnd.random() < 0.1,
        )
        for i in range(60)
    ]
    return pools, calls


def _same(a: fl.LinkResult, b: fl.LinkResult) -> bool:
    return (a.status, a.reason, a.confidence, a.candidates) == (
        b.status, b.reason, b.confidence, b.candidates,
    )


@pytest.mark.parametrize("seed", range(8))
def test_the_indexed_join_equals_link_call(seed):
    rnd = random.Random(seed)
    pools, calls = _random_world(rnd)
    for targets in (["a"], ["b", "a"], ["a", "b", "c"], []):
        indexed = [xf.ShapeIndex(pools[t]) for t in targets]
        flat = [s for t in targets for s in pools[t]]
        for call in calls:
            assert _same(xf.link_call_indexed(call, indexed), fl.link_call(call, flat))


@pytest.mark.parametrize("seed", range(8))
def test_the_caller_serves_note_equals_the_old_one(seed):
    rnd = random.Random(100 + seed)
    pools, calls = _random_world(rnd)
    own = pools["a"]
    index = xf.ShapeIndex(own)
    for call in calls:
        result = fl.LinkResult(fl.NO_ROUTE_MATCH)
        old = nfr.cross_repo_row_reason(call, result, None, None, own)
        assert xf.feign_row_reason(call, result, index) == old
