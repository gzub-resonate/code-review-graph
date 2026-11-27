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
    # Only exchanges with an edge in sub are listed, but the status rests on every repo.
    assert "audit.exchange" not in rows
    assert set(rows) == {"legacy.exchange", "orders.exchange"}


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


@pytest.fixture
def queue_world(tmp_path, registry):  # noqa: F811
    repo = _build_repo(tmp_path / "attr", exchange_edges=False)
    _add_edges(repo, [
        # Bare queue listeners (@RabbitListener(queues=...)): the name is the target itself.
        ("HANDLES", "attr.process-permutation", {"queue": "attr.process-permutation"}),
        ("HANDLES", "attr.refresh-vertical", {"queue": "attr.refresh-vertical"}),
        # A publish on the default exchange names the queue as its routing key.
        ("PUBLISHES", "attr.refresh-vertical",
         {"queue": "attr.refresh-vertical", "routing_key": "attr.refresh-vertical"}),
        # A real exchange binding: its queue is another name.
        ("HANDLES", "api.e.topic",
         {"binding_key": "a.*", "queue": "attr.q", "exchange": "api.e.topic"}),
    ])
    registry.register(str(repo), alias="attr")
    return repo


def test_queue_names_are_not_listed_as_exchanges(queue_world):
    reply = cross_repo_requests_func(origin="amqp")
    assert [r["exchange"] for r in reply["results"]] == ["api.e.topic"]
    assert reply["counts"] == {"unpublished": 1}
    queues = {q["queue"]: q for q in reply["queues"]}
    assert set(queues) == {"attr.process-permutation", "attr.refresh-vertical"}
    assert all(q["kind"] == "queue" for q in queues.values())
    assert queues["attr.process-permutation"]["listener_count"] == 1
    assert queues["attr.refresh-vertical"]["status"] == "matched"
    assert reply["queues_total"] == 2 and "queue name(s)" in reply["summary"]


def test_a_queue_edge_of_one_exchange_name_is_flagged_in_the_single_exchange_mode(queue_world):
    reply = cross_repo_requests_func(origin="amqp", service="attr.process-permutation")
    assert {r["kind"] for r in reply["results"]} == {"queue"}
    exchange = cross_repo_requests_func(origin="amqp", service="api.e.topic")
    assert all("kind" not in r for r in exchange["results"])


def test_scoped_list_computes_status_over_all_repos_and_shows_both_counts(world):
    reply = cross_repo_requests_func(origin="amqp", repos=["sub"])
    orders = _by_name(reply)["orders.exchange"]
    assert orders["status"] == "matched"
    assert (orders["publisher_count"], orders["listener_count"]) == (3, 7)
    assert (orders["scoped_publisher_count"], orders["scoped_listener_count"]) == (0, 1)
    assert orders["publisher_repos"] == ["pub"]
    assert reply["scoped_to"] == ["sub"] and reply["status_basis"] == "all_repos"
    assert reply["counts"] == {"unpublished": 1, "matched": 1}
    assert "status computed over all 3 repo(s)" in reply["summary"]
    # An unscoped call has no scoped counts.
    assert "scoped_publisher_count" not in _by_name(
        cross_repo_requests_func(origin="amqp"))["orders.exchange"]


def test_scoped_service_mode_reports_the_status_over_all_repos(world):
    reply = cross_repo_requests_func(origin="amqp", service="orders.exchange", repos=["sub"])
    assert {r["repo"] for r in reply["results"]} == {"sub"} and reply["total"] == 1
    assert reply["exchange_status"] == "matched"
    assert reply["exchange_counts"] == {"publisher": 3, "listener": 7}
    assert reply["exchange_status_confirmed"] is True and reply["scoped_to"] == ["sub"]


def test_a_spent_budget_is_in_the_summary_and_statuses_are_not_asserted(world, monkeypatch):
    from code_review_graph.tools import registry_tools

    monkeypatch.setenv("CRG_CROSS_REPO_BUDGET_SECONDS", "1")
    monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "1")
    # started, then the check before pub (in budget), sub and plain (spent).
    clock = iter([0.0, 0.5, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(registry_tools, "_now", lambda: next(clock))
    reply = cross_repo_requests_func(origin="amqp")
    assert "searched 1 of 3 repos, 2 skipped by the time budget" in reply["summary"]
    assert "across 3 repo(s)" not in reply["summary"]
    assert reply["skipped_repos"] == ["sub", "plain"] and reply["status_basis"] == "partial"
    assert "2 of 3" in reply["truncated_reason"]
    rows = _by_name(reply)
    assert rows["audit.exchange"]["status_confirmed"] is False
    assert "status_confirmed" not in rows["orders.exchange"]


def test_a_service_query_reads_the_repos_sharing_its_words_first():
    from code_review_graph.tools.registry_tools import _amqp_hint_order

    repos = [{"path": "/x/services-sink-s3"}, {"path": "/x/services-api-attribute"},
             {"path": "/x/geoflux"}, {"path": "/x/services-api"}]
    names = [r["path"].rsplit("/", 1)[1] for r in
             _amqp_hint_order(repos, "api.e.topic.attribute")]
    # "attribute" is shared ("api" is too generic to count); the rest keep their order.
    assert names == ["services-api-attribute", "services-sink-s3", "geoflux", "services-api"]
    assert _amqp_hint_order(repos, None) is repos


def test_a_spent_budget_marks_the_service_status_unconfirmed(world, monkeypatch):
    from code_review_graph.tools import registry_tools

    monkeypatch.setenv("CRG_CROSS_REPO_BUDGET_SECONDS", "1")
    monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "1")
    clock = iter([0.0, 0.5, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(registry_tools, "_now", lambda: next(clock))
    reply = cross_repo_requests_func(origin="amqp", service="audit.exchange")
    assert reply["exchange_status"] == "unlistened"
    assert reply["exchange_status_confirmed"] is False
    assert "searched 1 of 3 repos, 2 skipped by the time budget" in reply["summary"]


@pytest.fixture
def bound_queue_world(tmp_path, registry):  # noqa: F811
    """Imitates services-api-attribute: queues bound by ``@QueueBinding`` to ``api.e.topic.vertical``
    and services-api: ``api.q.projectadmin.account.created`` bound to ``api.e.topic.account``."""
    repo = _build_repo(tmp_path / "attr", exchange_edges=False)
    _add_edges(repo, [
        ("HANDLES", "svc.refresh", {"queue": "svc.refresh"}),
        ("HANDLES", "svc.permutation", {"queue": "svc.permutation"}),
        ("HANDLES", "svc.lonely", {"queue": "svc.lonely"}),
        ("HANDLES", "svc.wild", {"queue": "svc.wild"}),
        ("HANDLES", "svc.future", {"queue": "svc.future"}),
        ("PUBLISHES", "api.e.topic.vertical", {"routing_key": "verticals.isdirty"}),
        ("PUBLISHES", "api.e.topic.vertical", {"routing_key": "verticals.permutation.work"}),
        ("HANDLES", "api.e.topic.vertical",
         {"binding_key": "verticals.isdirty", "queue": "svc.refresh",
          "exchange": "api.e.topic.vertical"}),
        ("HANDLES", "api.e.topic.vertical",
         {"binding_key": "verticals.permutation.work", "queue": "svc.permutation",
          "exchange": "api.e.topic.vertical"}),
        # Bound to a key nobody publishes: stays unpublished.
        ("HANDLES", "api.e.topic.vertical",
         {"binding_key": "verticals.never", "queue": "svc.lonely",
          "exchange": "api.e.topic.vertical"}),
        # A wildcard binding reached by a published key.
        ("HANDLES", "api.e.topic.vertical",
         {"binding_key": "verticals.#", "queue": "svc.wild", "exchange": "api.e.topic.vertical"}),
        # Exchange with no publisher at all.
        ("HANDLES", "api.e.topic.quiet",
         {"binding_key": "q.x", "queue": "svc.future", "exchange": "api.e.topic.quiet"}),
    ])
    registry.register(str(repo), alias="attr")
    return repo


def test_a_queue_gets_the_publishers_of_the_exchange_it_is_bound_to(bound_queue_world):
    reply = cross_repo_requests_func(origin="amqp")
    queues = {q["queue"]: q for q in reply["queues"]}
    refresh = queues["svc.refresh"]
    assert refresh["status"] == "matched"
    assert refresh["publisher_count"] == 1
    assert refresh["publisher_repos"] == ["attr"]
    assert refresh["bound_publisher_count"] == 1
    assert refresh["bound_to"] == [{
        "exchange": "api.e.topic.vertical", "binding_key": "verticals.isdirty",
        "publisher_count": 1, "matched_via": "exact_key",
    }]
    assert queues["svc.permutation"]["status"] == "matched"


def test_a_wildcard_binding_counts_every_publisher_it_reaches(bound_queue_world):
    wild = {q["queue"]: q for q in cross_repo_requests_func(origin="amqp")["queues"]}["svc.wild"]
    assert wild["status"] == "matched"
    assert wild["publisher_count"] == 2
    assert wild["bound_to"][0]["matched_via"] == "topic_pattern"


def test_a_queue_bound_to_a_key_nobody_publishes_stays_unpublished(bound_queue_world):
    lonely = {q["queue"]: q for q in cross_repo_requests_func(origin="amqp")["queues"]}[
        "svc.lonely"]
    assert lonely["status"] == "unpublished"
    assert lonely["publisher_count"] == 0
    assert "bound_publisher_count" not in lonely
    assert lonely["bound_to"][0]["publisher_count"] == 0


def test_a_queue_bound_to_an_unpublished_exchange_stays_unpublished(bound_queue_world):
    future = {q["queue"]: q for q in cross_repo_requests_func(origin="amqp")["queues"]}[
        "svc.future"]
    assert future["status"] == "unpublished"
    assert future["bound_to"][0]["exchange"] == "api.e.topic.quiet"
