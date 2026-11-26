"""Concurrent identical cold builds share one computation (``repo_cache.SingleFlight``)."""

from __future__ import annotations

import threading
import time

import pytest

from code_review_graph import infra_join
from code_review_graph.repo_cache import MEMO, ResultCache, SingleFlight
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import registry as registry  # noqa: F401
from .test_cross_repo_speedups import touch_graph
from .test_cross_repo_speedups import world as world  # noqa: F401
from .test_service_aliases_infra_join import infra_world as infra_world  # noqa: F401

THREADS = 8


def run_together(fn, count=THREADS):
    """Call *fn* on *count* threads released at the same moment; return results or errors."""
    gate = threading.Barrier(count)
    out: list = [None] * count

    def worker(i):
        gate.wait()
        try:
            out[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            out[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


class TestSingleFlight:
    def test_simultaneous_identical_calls_compute_once(self):
        flight = SingleFlight()
        calls: list[int] = []

        def compute():
            calls.append(1)
            time.sleep(0.3)
            return "value"

        assert run_together(lambda: flight.do("k", compute)) == ["value"] * THREADS
        assert len(calls) == 1

    def test_different_keys_do_not_share(self):
        flight = SingleFlight()
        calls: list[str] = []
        flight.do("a", lambda: calls.append("a"))
        flight.do("b", lambda: calls.append("b"))
        assert calls == ["a", "b"]

    def test_waiters_get_the_leaders_exception(self):
        flight = SingleFlight()
        calls: list[int] = []

        def compute():
            calls.append(1)
            time.sleep(0.3)
            raise RuntimeError("boom")

        results = run_together(lambda: flight.do("k", compute))
        assert len(calls) == 1
        assert all(isinstance(r, RuntimeError) for r in results)
        # Nothing stays registered: the next call computes again.
        assert flight.do("k", lambda: "fresh") == "fresh"

    def test_a_waiter_computes_itself_when_the_leader_is_stuck(self):
        flight = SingleFlight()
        release = threading.Event()
        started = threading.Event()

        def stuck():
            started.set()
            release.wait(10)
            return "late"

        leader = threading.Thread(target=lambda: flight.do("k", stuck))
        leader.start()
        assert started.wait(5)
        begun = time.monotonic()
        assert flight.do("k", lambda: "mine", timeout=0.2) == "mine"
        assert time.monotonic() - begun < 5
        release.set()
        leader.join(5)


class TestMemoSharesColdReads:
    def test_simultaneous_cold_reads_of_a_repo_compute_once(self, world):  # noqa: F811
        entry = {"path": str(world["machines"])}
        calls: list[int] = []

        def compute():
            calls.append(1)
            time.sleep(0.3)
            return ["facts"]

        assert run_together(lambda: MEMO.get("ns", entry, compute)) == [["facts"]] * THREADS
        assert len(calls) == 1
        assert MEMO.get("ns", entry, compute) == ["facts"] and len(calls) == 1

    def test_a_changed_stamp_recomputes(self, world):  # noqa: F811
        entry = {"path": str(world["machines"])}
        calls: list[int] = []
        MEMO.get("ns", entry, lambda: calls.append(1))
        touch_graph(world["machines"])
        MEMO.get("ns", entry, lambda: calls.append(1))
        assert len(calls) == 2


class TestResultCache:
    def test_is_bounded_and_keyed(self, monkeypatch):
        cache = ResultCache(entries=2)
        for key in "abc":
            cache.get(key, lambda key=key: key)
        assert len(cache) == 2
        calls: list[int] = []
        cache.get("a", lambda: calls.append(1) or "a")
        assert calls == [1]  # "a" was evicted

    def test_an_uncacheable_result_is_not_kept(self):
        cache = ResultCache()
        calls: list[int] = []
        for _ in range(2):
            cache.get("k", lambda: calls.append(1) or {"truncated": True},
                      cacheable=lambda v: not v["truncated"])
        assert len(calls) == 2


class TestInfrastructureBlockIsShared:
    @pytest.fixture
    def counted(self, monkeypatch):
        calls: list[int] = []
        real = infra_join.build_infrastructure

        def slow(*args, **kwargs):
            calls.append(1)
            time.sleep(0.3)
            return real(*args, **kwargs)

        monkeypatch.setattr(infra_join, "build_infrastructure", slow)
        return calls

    @staticmethod
    def call():
        return cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=100,
        )

    def test_simultaneous_identical_calls_build_the_block_once(
        self, infra_world, counted,  # noqa: F811
    ):
        results = run_together(self.call)
        assert len(counted) == 1
        assert all(isinstance(r, dict) and "infrastructure" in r for r in results)
        blocks = [r["infrastructure"] for r in results]
        assert all(b == blocks[0] for b in blocks)
        # Each reply owns its copy: changing one leaves the others alone.
        blocks[0]["repos"].clear()
        assert blocks[1] == results[2]["infrastructure"]

    def test_a_repeat_reuses_it_and_a_changed_stamp_rebuilds(
        self, infra_world, counted, monkeypatch,  # noqa: F811
    ):
        self.call()
        self.call()
        assert len(counted) == 1
        from code_review_graph import repo_cache

        real = repo_cache.graph_stamp
        monkeypatch.setattr(
            repo_cache, "graph_stamp",
            lambda entry: (*(real(entry) or ()), (1, 2, 3)),
        )
        self.call()
        assert len(counted) == 2

    def test_a_truncated_block_is_not_kept(self, infra_world, counted, monkeypatch):  # noqa: F811
        monkeypatch.setattr(infra_join, "MAX_REPOS", 0)
        self.call()
        self.call()
        assert len(counted) == 2
