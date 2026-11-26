"""The cross-repo joins keep what they read about a repo, read repos on threads and skip work.

* ``repo_cache.MEMO`` keeps a per-repo result while that repo's graph file is unchanged.
* ``parallel_map`` and ``parallel_map_budget`` give the results of a serial loop, in order.
* ``cross_repo_requests_tool`` with ``origin="stepfunction"`` or ``"awssdk"`` reads each repo's
  definitions and facts once across calls, and a ``service`` or ``route`` filter leaves the
  rows an unfiltered call has, without the owner lookup of the rows it drops.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from code_review_graph import service_aliases as sa
from code_review_graph import stepfunction_links as sl
from code_review_graph.repo_cache import (
    MEMO,
    SKIPPED,
    _reset_shared_pool,
    graph_stamp,
    memo_entries,
    parallel_map,
    parallel_map_budget,
)
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import LAMBDAS, MACHINES, OTHER, PLATFORM, make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401
from .test_service_aliases_infra_join import infra_world as infra_world  # noqa: F401


def graph_file(root: Path) -> Path:
    return root / ".code-review-graph" / "graph.db"


_REBUILDS = iter(range(1, 10**6))


def touch_graph(root: Path) -> None:
    """Make the repo's graph really change: it was rebuilt (a newer build time is recorded)."""
    from code_review_graph.graph import GraphStore

    with GraphStore(graph_file(root)) as store:
        store.set_metadata("last_updated", "2099-01-01T00:00:%06d" % next(_REBUILDS))


@pytest.fixture
def world(tmp_path, registry):  # noqa: F811
    roots = {
        "machines": make_repo(tmp_path, registry, "machines", MACHINES),
        "lambdas": make_repo(tmp_path, registry, "lambdas", LAMBDAS),
        "platform": make_repo(tmp_path, registry, "platform", PLATFORM),
        "other": make_repo(tmp_path, registry, "other", OTHER),
    }
    return roots


class TestRepoMemo:
    def test_a_value_is_reused_until_the_graph_file_changes(self, world):
        entry = {"path": str(world["machines"])}
        calls: list[int] = []

        def compute() -> str:
            calls.append(1)
            return "read"

        assert MEMO.get("ns", entry, compute) == "read"
        assert MEMO.get("ns", entry, compute) == "read"
        assert len(calls) == 1
        touch_graph(world["machines"])
        MEMO.get("ns", entry, compute)
        assert len(calls) == 2
        # Another namespace of the same repo is its own value.
        MEMO.get("other-ns", entry, compute)
        assert len(calls) == 3

    def test_the_stamp_follows_the_file_and_is_none_without_a_graph(self, world, tmp_path):
        entry = {"path": str(world["lambdas"])}
        first = graph_stamp(entry)
        assert first is not None
        touch_graph(world["lambdas"])
        assert graph_stamp(entry) != first
        assert graph_stamp({"path": str(tmp_path / "nowhere")}) is None

    def test_a_value_the_caller_rejects_or_a_repo_without_a_graph_is_not_kept(
        self, world, tmp_path,
    ):
        entry = {"path": str(world["machines"])}
        calls: list[int] = []

        def failed() -> None:
            calls.append(1)

        MEMO.get("ns", entry, failed, cacheable=lambda v: v is not None)
        MEMO.get("ns", entry, failed, cacheable=lambda v: v is not None)
        assert len(calls) == 2
        missing = {"path": str(tmp_path / "nowhere")}
        MEMO.get("ns", missing, failed)
        MEMO.get("ns", missing, failed)
        assert len(calls) == 4
        assert len(MEMO) == 0

    def test_the_cache_is_bounded_and_can_be_turned_off(self, world, monkeypatch):
        machines = {"path": str(world["machines"])}
        lambdas = {"path": str(world["lambdas"])}
        calls: list[str] = []

        def compute(name: str):
            def run() -> str:
                calls.append(name)
                return name
            return run

        monkeypatch.setenv("CRG_REPO_MEMO_ENTRIES", "1")
        MEMO.get("ns", machines, compute("m"))
        MEMO.get("ns", lambdas, compute("l"))
        assert len(MEMO) == 1
        MEMO.get("ns", machines, compute("m"))  # evicted, read again
        assert calls == ["m", "l", "m"]
        monkeypatch.setenv("CRG_REPO_MEMO_ENTRIES", "0")
        MEMO.get("ns", machines, compute("m"))
        MEMO.get("ns", machines, compute("m"))
        assert calls == ["m", "l", "m", "m", "m"]

    def test_a_bad_setting_falls_back_to_the_default(self, monkeypatch):
        for bad in ("many", "-3", "  "):
            monkeypatch.setenv("CRG_REPO_MEMO_ENTRIES", bad)
            assert memo_entries() == 4096

    def test_threads_share_it_safely(self, world):
        entry = {"path": str(world["machines"])}
        seen: list[str] = []

        def worker() -> None:
            seen.append(MEMO.get("ns", entry, lambda: "same"))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert seen == ["same"] * 16
        assert len(MEMO) == 1


@pytest.fixture
def pool_of(monkeypatch):
    """Make the shared pool of a given size for the test, and drop it afterwards."""

    def make(size: int) -> None:
        _reset_shared_pool()
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", str(size))

    yield make
    _reset_shared_pool()


class TestParallelHelpers:
    @pytest.mark.parametrize("workers", [1, 2, 8])
    def test_results_come_back_in_input_order(self, workers):
        assert parallel_map(lambda n: n * n, range(20), workers) == [n * n for n in range(20)]
        assert parallel_map(lambda n: n, [], workers) == []

    def test_work_overlaps_on_threads(self, pool_of):
        pool_of(4)
        names: set[str] = set()
        barrier = threading.Barrier(3, timeout=5)

        def fn(_n: int) -> int:
            names.add(threading.current_thread().name)
            barrier.wait()  # only passes when three run at the same time
            return _n

        assert parallel_map(fn, [1, 2, 3], 3) == [1, 2, 3]
        assert len(names) == 3

    def test_a_batch_that_starts_after_the_budget_is_skipped(self):
        spent = {"now": False}

        def fn(n: int) -> int:
            if n == 2:
                spent["now"] = True
            return n

        got = parallel_map_budget(fn, [0, 1, 2, 3, 4], 1, lambda: spent["now"])
        # One worker checks before each item, as a serial loop does.
        assert got[:3] == [0, 1, 2] and got[3:] == [SKIPPED, SKIPPED]
        wide = parallel_map_budget(lambda n: n, [0, 1, 2, 3], 2, lambda: False)
        assert wide == [0, 1, 2, 3]
        none_left = parallel_map_budget(lambda n: n, [0, 1, 2], 2, lambda: True)
        assert none_left == [SKIPPED] * 3
        assert parallel_map_budget(lambda n: n, [], 4, lambda: False) == []


class TestSharedPool:
    def test_concurrent_calls_share_one_bounded_pool(self, pool_of):
        pool_of(3)
        lock = threading.Lock()
        running = {"now": 0, "max": 0}
        pool_threads: set[int] = set()

        def fn(n: int) -> int:
            if threading.current_thread().name.startswith("crg-xrepo"):
                pool_threads.add(threading.get_ident())
            with lock:
                running["now"] += 1
                running["max"] = max(running["max"], running["now"])
            time.sleep(0.01)
            with lock:
                running["now"] -= 1
            return n * 2

        callers = 8
        outcomes: dict[int, list[int]] = {}

        def call(i: int) -> None:
            outcomes[i] = parallel_map(fn, range(12), 4)

        threads = [threading.Thread(target=call, args=(i,)) for i in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(got == [n * 2 for n in range(12)] for got in outcomes.values())
        assert len(outcomes) == callers
        # Eight calls of four workers used to start 32 threads; now at most the pool's three
        # run besides the callers themselves.
        assert len(pool_threads) <= 3
        assert running["max"] <= 3 + callers

    def test_a_task_that_maps_again_does_not_deadlock(self, pool_of):
        pool_of(2)

        def inner(n: int) -> int:
            return n + 1

        def outer(n: int) -> int:
            return sum(parallel_map(inner, range(4), 3)) + n

        done: list[list[int]] = []
        worker = threading.Thread(
            target=lambda: done.append(parallel_map(outer, range(6), 3)), daemon=True,
        )
        worker.start()
        worker.join(timeout=20)
        assert done == [[10 + n for n in range(6)]]

    def test_the_first_error_is_raised(self, pool_of):
        pool_of(2)

        def fn(n: int) -> int:
            if n == 3:
                raise ValueError("three")
            return n

        with pytest.raises(ValueError, match="three"):
            parallel_map(fn, range(8), 3)
        assert parallel_map(lambda n: n, range(4), 3) == [0, 1, 2, 3]

    def test_the_pool_size_follows_the_setting(self, monkeypatch):
        from code_review_graph.repo_cache import shared_pool_size

        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "2")
        assert shared_pool_size() == 2
        monkeypatch.delenv("CRG_CROSS_REPO_WORKERS")
        assert 1 <= shared_pool_size() <= 4


def count_calls(monkeypatch, owner, name: str) -> list[int]:
    """Wrap ``owner.name`` so each call is counted, and return the counter."""
    calls: list[int] = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


class TestSelectionDoesNotChangeOwners:
    """Any registered repo may own a target, so a selection must not narrow the owner lookup."""

    def test_rows_of_a_selected_repo_match_the_all_repo_call(self, world):
        everyone = cross_repo_requests_func(origin="stepfunction")["results"]
        alone = cross_repo_requests_func(origin="stepfunction", repos=["machines"])["results"]
        assert alone and {r["repo"] for r in alone} == {"machines"}
        assert alone == [r for r in everyone if r["repo"] == "machines"]
        # The owners are in repos that were not selected, and still resolve.
        assert any(r["status"] == "matched" and r["target_repo"] != "machines" for r in alone)

    def test_every_repo_is_asked_for_its_definitions_whatever_the_selection(
        self, world, monkeypatch,
    ):
        reads = count_calls(monkeypatch, sl.Definitions, "_read")
        cross_repo_requests_func(origin="stepfunction", repos=["machines"])
        assert len(reads) == len(world)


class TestStepFunctionJoinIsKept:
    def test_each_repo_is_read_once_across_calls(self, world, monkeypatch):
        definitions = count_calls(monkeypatch, sl.Definitions, "_read")
        facts = count_calls(monkeypatch, sa, "read_repo_facts")
        first = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
        assert len(definitions) == 4 and len(facts) == 4
        second = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
        assert len(definitions) == 4 and len(facts) == 4
        assert second == first
        # A rebuilt graph is read again, and only that one.
        touch_graph(world["lambdas"])
        third = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
        assert len(definitions) == 5 and len(facts) == 5
        assert third == first

    def test_a_threaded_call_answers_as_a_serial_one(self, world, monkeypatch):
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "1")
        serial = cross_repo_requests_func(origin="stepfunction")
        MEMO.clear()
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "8")
        threaded = cross_repo_requests_func(origin="stepfunction")
        assert threaded == serial

    FILTERS = [
        {"service": "validateOrder"},
        {"service": "alerts"},
        {"route": "lambda"},
        {"route": "lambda/audit-trail"},
        {"route": "sns/alerts"},
        {"route": "/sns/"},
        {"service": "validateOrder", "route": "sns"},
        {"service": "nothing-has-this-name"},
    ]

    @staticmethod
    def expected(rows, service, route):
        out = []
        for row in rows:
            if service and row["target_name"] != service:
                continue
            wanted = route.strip("/") if route else ""
            if wanted and not (row["target"] == wanted or row["target"].startswith(wanted + "/")):
                continue
            out.append(row)
        return out

    @pytest.mark.parametrize(
        "flt", FILTERS, ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()),
    )
    def test_a_filter_leaves_the_rows_an_unfiltered_call_has(self, world, flt):
        everything = cross_repo_requests_func(origin="stepfunction", max_results=100)
        filtered = cross_repo_requests_func(origin="stepfunction", max_results=100, **flt)
        keep = self.expected(everything["results"], flt.get("service"), flt.get("route"))
        assert filtered["results"] == keep
        wanted_counts: dict[str, int] = {}
        for row in keep:
            wanted_counts[row["status"]] = wanted_counts.get(row["status"], 0) + 1
        assert filtered["counts"] == wanted_counts
        assert filtered["total"] == len(keep)

    def test_an_edge_the_filter_drops_gets_no_owner_lookup(self, world, monkeypatch):
        lookups = count_calls(monkeypatch, sl.Definitions, "how")
        cross_repo_requests_func(origin="stepfunction", max_results=100)
        unfiltered = len(lookups)
        lookups.clear()
        cross_repo_requests_func(origin="stepfunction", service="validateOrder")
        assert 0 < len(lookups) < unfiltered
        lookups.clear()
        cross_repo_requests_func(origin="stepfunction", service="nothing-has-this-name")
        assert lookups == []


class TestAwsSdkJoin:
    def test_filters_leave_the_rows_an_unfiltered_call_has(self, infra_world):  # noqa: F811
        everything = cross_repo_requests_func(
            origin="awssdk", repos=["inventory-svc"], max_results=100,
        )
        assert everything["results"]
        names = {row["target_name"] for row in everything["results"]}
        kinds = {row["target_kind"] for row in everything["results"]}
        for name in sorted(names)[:4]:
            filtered = cross_repo_requests_func(
                origin="awssdk", repos=["inventory-svc"], service=name, max_results=100,
            )
            keep = [r for r in everything["results"] if r["target_name"] == name]
            assert filtered["results"] == keep
        for kind in sorted(kinds):
            filtered = cross_repo_requests_func(
                origin="awssdk", repos=["inventory-svc"], route=kind, max_results=100,
            )
            keep = [
                r for r in everything["results"]
                if r["target"] == kind or r["target"].startswith(kind + "/")
            ]
            assert filtered["results"] == keep

    def test_a_cached_call_answers_as_the_first_did(self, infra_world, monkeypatch):  # noqa: F811
        first = cross_repo_requests_func(origin="awssdk", repos=["inventory-svc"])
        facts = count_calls(monkeypatch, sa, "read_repo_facts")
        second = cross_repo_requests_func(origin="awssdk", repos=["inventory-svc"])
        assert second == first
        assert facts == []


class TestGraphStampIgnoresFileNoise:
    """The stamp follows the graph, not the mtime or the ``-wal`` of its database file."""

    def test_a_file_time_change_alone_keeps_the_stamp(self, world):
        entry = {"path": str(world["machines"])}
        first = graph_stamp(entry)
        later = graph_file(world["machines"]).stat().st_mtime_ns + 7_000_000_000
        os.utime(graph_file(world["machines"]), ns=(later, later))
        assert graph_stamp(entry) == first

    def test_a_wal_only_change_keeps_the_stamp(self, world):
        import code_review_graph.repo_cache as rc
        from code_review_graph.graph import GraphStore

        entry = {"path": str(world["machines"])}
        path = graph_file(world["machines"])
        first = graph_stamp(entry)
        before = rc._file_signature(path)
        # A writer that changes nothing the graph holds still moves the WAL.
        with GraphStore(path) as store:
            store._conn.execute("INSERT INTO metadata (key, value) VALUES ('scratch', '1')")
            store._conn.execute("DELETE FROM metadata WHERE key = 'scratch'")
            assert rc._file_signature(path) != before
            assert graph_stamp(entry) == first
        assert graph_stamp(entry) == first

    def test_a_real_graph_change_gives_a_new_stamp(self, world):
        from code_review_graph.graph import GraphStore
        from code_review_graph.parser import NodeInfo

        entry = {"path": str(world["machines"])}
        first = graph_stamp(entry)
        touch_graph(world["machines"])
        second = graph_stamp(entry)
        assert second != first
        with GraphStore(graph_file(world["machines"])) as store:
            store.store_file_nodes_edges(
                "/x/new.py",
                [NodeInfo(kind="Function", name="fresh", file_path="/x/new.py",
                          line_start=1, line_end=1, language="python")],
                [],
            )
        assert graph_stamp(entry) not in (first, second)

    def test_a_replaced_database_file_gives_a_new_stamp(self, world):
        entry = {"path": str(world["machines"])}
        first = graph_stamp(entry)
        path = graph_file(world["machines"])
        copy = path.with_name("graph.db.copy")
        copy.write_bytes(path.read_bytes())
        copy.replace(path)
        assert graph_stamp(entry) != first

    def test_a_unchanged_file_costs_no_database_open(self, world, monkeypatch):
        import code_review_graph.repo_cache as rc

        entry = {"path": str(world["machines"])}
        graph_stamp(entry)
        monkeypatch.setattr(rc, "_graph_marker", lambda p: pytest.fail("reopened"))
        graph_stamp(entry)
