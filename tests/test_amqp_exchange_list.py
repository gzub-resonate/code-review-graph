"""``origin="amqp"`` with no exchange: every exchange, its publishers, listeners and status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_amqp_query import _build_repo, registry  # noqa: F401


def _add_edges(root: Path, spec: list[tuple[str, str, dict]]) -> None:
    """Add ``(kind, exchange, extra)`` edges from the repo's first function."""
    path = (root / "svc.py").as_posix()
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        for n, (kind, exchange, extra) in enumerate(spec, start=40):
            store._conn.execute(
                "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line,"
                " extra, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (kind, f"{path}::publish_created", f"external:rabbitmq:{exchange}", path, n,
                 json.dumps(extra)),
            )
        store._conn.commit()


@pytest.fixture
def world(tmp_path, registry):  # noqa: F811
    pub = _build_repo(tmp_path / "pub", exchange_edges=True)
    sub = _build_repo(tmp_path / "sub", exchange_edges=False)
    plain = _build_repo(tmp_path / "plain", exchange_edges=False)
    # pub: orders.exchange (full fixture: 3 publishers, 6 listeners) plus a publish-only one.
    _add_edges(pub, [("PUBLISHES", "audit.exchange", {"routing_key": "audit.written"})])
    # sub: listens on audit? no: only an exchange nobody publishes to, and orders.exchange.
    _add_edges(sub, [
        ("HANDLES", "legacy.exchange", {"binding_key": "legacy.*", "queue": "q.legacy"}),
        ("HANDLES", "orders.exchange", {"binding_key": "orders.created", "queue": "q.o"}),
    ])
    for name, root in (("pub", pub), ("sub", sub), ("plain", plain)):
        registry.register(str(root), alias=name)
    return tmp_path


def _by_name(reply):
    return {r["exchange"]: r for r in reply["results"]}


def test_every_exchange_is_listed_with_roles_repos_and_keys(world):
    reply = cross_repo_requests_func(origin="amqp")
    rows = _by_name(reply)
    assert list(rows) == ["audit.exchange", "legacy.exchange", "orders.exchange"]
    audit, legacy, orders = rows.values()
    assert (audit["status"], audit["publisher_count"], audit["listener_count"]) == (
        "unlistened", 1, 0)
    assert audit["publisher_repos"] == ["pub"] and audit["listener_repos"] == []
    assert audit["routing_keys"] == ["audit.written"]
    assert (legacy["status"], legacy["publisher_count"], legacy["listener_count"]) == (
        "unpublished", 0, 1)
    assert legacy["binding_keys"] == ["legacy.*"]
    assert orders["status"] == "matched"
    assert orders["publisher_count"] == 3 and orders["listener_count"] == 6 + 1
    assert orders["publisher_repos"] == ["pub"] and orders["listener_repos"] == ["pub", "sub"]
    assert "orders.created" in orders["routing_keys"]
    assert reply["counts"] == {"unlistened": 1, "unpublished": 1, "matched": 1}
    assert reply["repos_without_amqp"] == ["plain"]
    assert reply["total"] == 3


def test_status_filter_keeps_counts_over_everything(world):
    reply = cross_repo_requests_func(origin="amqp", status="unlistened")
    assert [r["exchange"] for r in reply["results"]] == ["audit.exchange"]
    assert reply["total"] == 1 and reply["counts"]["matched"] == 1
    assert cross_repo_requests_func(origin="amqp", status="unpublished")["results"][0][
        "exchange"] == "legacy.exchange"


def test_role_statuses_are_refused_without_an_exchange(world):
    with pytest.raises(ValueError, match="unlistened"):
        cross_repo_requests_func(origin="amqp", status="publisher")


def test_route_is_a_key_substring_and_repos_limit_the_roles(world):
    reply = cross_repo_requests_func(origin="amqp", route="audit")
    assert [r["exchange"] for r in reply["results"]] == ["audit.exchange"]
    only_sub = cross_repo_requests_func(origin="amqp", repos=["sub"])
    rows = _by_name(only_sub)
    # Seen from sub alone, orders.exchange has no publisher.
    assert rows["orders.exchange"]["status"] == "unpublished"
    assert "audit.exchange" not in rows


def test_rows_page_and_cursor_is_scoped(world):
    first = cross_repo_requests_func(origin="amqp", max_results=2)
    assert len(first["results"]) == 2 and first["next_cursor"]
    second = cross_repo_requests_func(origin="amqp", max_results=2, cursor=first["next_cursor"])
    assert [r["exchange"] for r in second["results"]] == ["orders.exchange"]
    with pytest.raises(ValueError):
        cross_repo_requests_func(
            origin="amqp", max_results=2, status="matched", cursor=first["next_cursor"],
        )


def test_exchange_given_behaviour_is_unchanged(world):
    reply = cross_repo_requests_func(origin="amqp", service="orders.exchange")
    assert reply["summary"].startswith("10 AMQP publisher/listener row(s) for exchange")
    assert {r["role"] for r in reply["results"]} == {"publisher", "listener"}
