"""The opt-in background warm-up of the heavy cross-repo origins."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from code_review_graph import cross_repo_warmup as cw
from code_review_graph.repo_cache import MEMO
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import LAMBDAS, MACHINES, OTHER, PLATFORM, make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401


def test_it_is_off_by_default_and_the_origins_default(monkeypatch):
    monkeypatch.delenv(cw.WARMUP_ENV, raising=False)
    monkeypatch.delenv(cw.ORIGINS_ENV, raising=False)
    assert cw.warmup_enabled() is False
    assert cw.start_warmup() is None
    assert cw.warmup_origins() == ["awssdk", "feign", "js"]


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_the_switch_values(monkeypatch, raw):
    monkeypatch.setenv(cw.WARMUP_ENV, raw)
    assert cw.warmup_enabled() is True


def test_the_origin_list_is_cleaned(monkeypatch, caplog):
    monkeypatch.setenv(cw.ORIGINS_ENV, " JS, awssdk,js,nonsense ,,feign")
    with caplog.at_level(logging.WARNING):
        assert cw.warmup_origins() == ["js", "awssdk", "feign"]
    assert "nonsense" in caplog.text


def test_a_pass_warms_one_origin_at_a_time_and_logs_each(caplog):
    running = {"now": 0, "peak": 0}
    seen: list[str] = []

    def call(origin):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        seen.append(origin)
        running["now"] -= 1
        return {"status": "ok"}

    warmer = cw.Warmer(origins=["awssdk", "feign", "js"], call=call, pause=0)
    with caplog.at_level(logging.INFO, logger=cw.logger.name):
        report = warmer.run_pass()
    assert seen == ["awssdk", "feign", "js"] and running["peak"] == 1
    assert [r[0] for r in report] == seen
    lines = [r.getMessage() for r in caplog.records if "origin=" in r.getMessage()]
    assert len(lines) == 3 and all("s ok" in line for line in lines)


def test_an_error_in_one_origin_is_logged_and_the_next_still_runs(caplog):
    def call(origin):
        if origin == "feign":
            raise RuntimeError("boom")
        return {"status": "ok", "truncated": origin == "js"}

    warmer = cw.Warmer(origins=["awssdk", "feign", "js"], call=call, pause=0)
    with caplog.at_level(logging.INFO, logger=cw.logger.name):
        report = warmer.run_pass()
    assert [r[2] for r in report] == [
        "ok", "failed: RuntimeError", "ok (truncated: not fully cached)",
    ]


def test_the_time_cap_stops_further_origins():
    ticks = iter(range(0, 1000, 100))
    warmer = cw.Warmer(
        origins=["awssdk", "feign", "js"], call=lambda o: {}, pause=0, max_seconds=150,
        clock=lambda: float(next(ticks)),
    )
    assert [r[0] for r in warmer.run_pass()] == ["awssdk"]


def test_a_stop_ends_a_pass_and_the_thread():
    gate = threading.Event()
    seen: list[str] = []

    def call(origin):
        seen.append(origin)
        gate.set()
        return {}

    warmer = cw.Warmer(
        origins=["awssdk", "feign"], call=call, signature=lambda: (("r", 1),), pause=30,
        interval=0.05, initial_delay=0,
    )
    thread = warmer.start()
    assert gate.wait(5)
    warmer.stop(timeout=5)
    assert not thread.is_alive()
    assert seen == ["awssdk"]


def test_it_runs_after_start_and_again_after_the_stamps_change_and_settle():
    state = {"sig": (("repo", 1),)}
    done = threading.Semaphore(0)
    calls: list[str] = []

    def call(origin):
        calls.append(origin)
        done.release()
        return {}

    warmer = cw.Warmer(
        origins=["awssdk"], call=call, signature=lambda: state["sig"], pause=0, interval=0.05,
        initial_delay=0,
    )
    warmer.start()
    try:
        assert done.acquire(timeout=5)
        time.sleep(0.3)
        assert calls == ["awssdk"], "an unchanged registry is not warmed again"
        state["sig"] = (("repo", 2),)  # the indexer published
        assert done.acquire(timeout=5)
        assert calls == ["awssdk", "awssdk"]
    finally:
        warmer.stop()


def test_a_registry_still_changing_is_not_warmed_until_it_holds_still():
    counter = {"n": 0}
    calls: list[str] = []
    warmer = cw.Warmer(
        origins=["awssdk"], call=lambda o: calls.append(o) or {},
        signature=lambda: (("repo", counter.__setitem__("n", counter["n"] + 1) or counter["n"]),),
        pause=0, interval=0.02, initial_delay=0,
    )
    warmer.start()
    time.sleep(0.4)
    warmer.stop()
    assert calls == []


def test_a_signature_error_never_escapes():
    def broken():
        raise RuntimeError("registry unreadable")

    warmer = cw.Warmer(origins=["awssdk"], call=lambda o: {}, signature=broken, interval=0.02,
                       initial_delay=0)
    thread = warmer.start()
    time.sleep(0.15)
    assert thread.is_alive()
    warmer.stop()
    assert not thread.is_alive()


def test_the_default_call_is_the_unscoped_default_page_call(monkeypatch):
    from code_review_graph.tools import registry_tools

    captured = {}
    monkeypatch.setattr(
        registry_tools, "cross_repo_requests_func",
        lambda **kw: captured.update(kw) or {"status": "ok"},
    )
    cw._default_call("feign")
    assert captured == {"origin": "feign"}


def test_a_real_signature_follows_the_registry_and_the_graphs(tmp_path, registry):  # noqa: F811
    root = make_repo(tmp_path, registry, "machines", MACHINES)
    first = cw.registry_signature()
    assert cw.registry_signature() == first
    from code_review_graph.graph import GraphStore

    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.set_metadata("last_updated", "2099-01-01T00:00:00")
    assert cw.registry_signature() != first


def test_a_warmed_origin_makes_the_first_real_request_a_cache_hit(  # noqa: F811
    tmp_path, registry, monkeypatch,
):
    for name, files in (
        ("machines", MACHINES), ("lambdas", LAMBDAS), ("platform", PLATFORM), ("other", OTHER),
    ):
        make_repo(tmp_path, registry, name, files)
    MEMO.clear()
    reads: list[int] = []
    original = MEMO.get

    def counting(namespace, entry, compute, cacheable=None):
        def counted():
            reads.append(1)
            return compute()

        return original(namespace, entry, counted, cacheable)

    monkeypatch.setattr(MEMO, "get", counting)
    warmer = cw.Warmer(origins=["stepfunction"], pause=0)
    assert [r[2] for r in warmer.run_pass()] == ["ok"]
    warmed = len(reads)
    assert warmed > 0
    cross_repo_requests_func(origin="stepfunction")
    assert len(reads) == warmed
