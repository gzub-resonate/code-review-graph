"""``list_repos_tool`` pages, and the AMQP ``queues`` list pages with the exchanges.

Both follow the cursor contract of the other list tools: ``limit`` / ``max_results`` bounds a
page, ``next_cursor`` reads the next one, ``total`` counts everything, and a cursor from a
different query or from a list that has since changed is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.paging import CursorError
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func, list_repos_func

from . import test_amqp_query
from .test_amqp_exchange_list import _add_edges
from .test_amqp_query import _build_repo

#: The shared registry fixture (``CRG_HOME`` in a temporary directory), bound once here.
registry = test_amqp_query.registry


def _register(registry: Registry, base: Path, count: int, start: int = 0) -> None:
    for n in range(start, start + count):
        root = base / f"repo-{n:03d}"
        (root / ".git").mkdir(parents=True)
        registry.register(str(root), alias=f"r{n:03d}")


def _read_all(call, key: str, max_pages: int = 20):
    """Follow ``next_cursor`` to the end; return every row under *key* and the pages."""
    pages, rows, cursor = [], [], None
    for _ in range(max_pages):
        reply = call(cursor)
        pages.append(reply)
        rows.extend(reply[key])
        cursor = reply.get("next_cursor")
        if not cursor:
            return rows, pages
    raise AssertionError("cursor never ended")


# --- list_repos_tool ---------------------------------------------------------------------


def test_a_small_registry_is_one_page_with_the_old_keys_and_no_cursor(tmp_path, registry):
    _register(registry, tmp_path, 3)
    reply = list_repos_func()
    assert len(reply["repos"]) == 3
    assert (reply["total"], reply["count"], reply["truncated"]) == (3, 3, False)
    assert "next_cursor" not in reply and "offset" not in reply
    assert reply["summary"] == "3 registered repository(ies)"


def test_cursors_read_every_repo_once_in_registry_order(tmp_path, registry):
    _register(registry, tmp_path, 120)
    first = list_repos_func()
    assert len(first["repos"]) == 50 and first["total"] == 120 and first["truncated"]
    assert first["next_cursor"] and "showing 50 of 120" in first["summary"]
    rows, pages = _read_all(lambda c: list_repos_func(cursor=c), "repos")
    assert rows == registry.list_repos()
    assert [len(p["repos"]) for p in pages] == [50, 50, 20]
    assert pages[1]["offset"] == 50 and "offset" not in pages[0]
    assert "next_cursor" not in pages[-1] and pages[-1]["truncated"] is False


def test_limit_sets_the_page_and_is_capped_at_the_ceiling(tmp_path, registry):
    _register(registry, tmp_path, 120)
    assert len(list_repos_func(limit=7)["repos"]) == 7
    assert len(list_repos_func(limit=10_000)["repos"]) == 100
    with pytest.raises(ValueError):
        list_repos_func(limit=0)


def test_a_page_size_change_between_pages_is_allowed(tmp_path, registry):
    _register(registry, tmp_path, 120)
    first = list_repos_func(limit=10)
    second = list_repos_func(limit=40, cursor=first["next_cursor"])
    assert [r["path"] for r in first["repos"] + second["repos"]] == [
        r["path"] for r in registry.list_repos()[:50]
    ]


def test_a_cursor_is_refused_after_the_registry_changes(tmp_path, registry):
    _register(registry, tmp_path, 60)
    cursor = list_repos_func()["next_cursor"]
    _register(registry, tmp_path, 1, start=60)
    with pytest.raises(CursorError, match="start over"):
        list_repos_func(cursor=cursor)


def test_a_made_up_cursor_is_refused(tmp_path, registry):
    _register(registry, tmp_path, 60)
    with pytest.raises(CursorError):
        list_repos_func(cursor="not-a-cursor")


# --- AMQP queues -------------------------------------------------------------------------


@pytest.fixture
def many_queues(tmp_path, registry):  # noqa: F811
    repo = _build_repo(tmp_path / "attr", exchange_edges=False)
    edges = [("PUBLISHES", f"ex.{i}", {"routing_key": f"k{i}"}) for i in range(2)]
    edges += [("HANDLES", f"q.{i:02d}", {"queue": f"q.{i:02d}"}) for i in range(7)]
    _add_edges(repo, edges)
    registry.register(str(repo), alias="attr")
    return repo


def test_default_page_still_carries_every_queue_when_they_fit(many_queues):
    reply = cross_repo_requests_func(origin="amqp")
    assert len(reply["queues"]) == 7 and reply["queues_total"] == 7
    assert reply["queues_truncated"] is False
    assert "next_cursor" not in reply


def test_max_results_bounds_the_queues_and_the_cursor_reads_the_rest(many_queues):
    whole = cross_repo_requests_func(origin="amqp")
    first = cross_repo_requests_func(origin="amqp", max_results=3)
    assert len(first["queues"]) == 3 and first["queues_total"] == 7
    assert first["queues_truncated"] is True and first["next_cursor"]

    def call(cursor):
        return cross_repo_requests_func(origin="amqp", max_results=3, cursor=cursor)

    queues, pages = _read_all(call, "queues")
    assert queues == whole["queues"]
    exchanges = [row for page in pages for row in page["results"]]
    assert exchanges == whole["results"]
    assert all(p["queues_total"] == 7 for p in pages)
    assert all(len(p["queues"]) <= 3 for p in pages)


def test_a_status_filter_leaves_the_queues_out_as_before(many_queues):
    reply = cross_repo_requests_func(origin="amqp", status="unlistened")
    assert "queues" not in reply and "queues_total" not in reply


def test_a_queue_cursor_belongs_to_its_query(many_queues):
    cursor = cross_repo_requests_func(origin="amqp", max_results=3)["next_cursor"]
    with pytest.raises(ValueError, match="different query"):
        cross_repo_requests_func(
            origin="amqp", max_results=3, route="ex", cursor=cursor,
        )
