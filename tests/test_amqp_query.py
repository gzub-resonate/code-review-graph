"""AMQP publishers and listeners of one exchange: topic matching, the zero note, the join.

The graphs are built by hand with the edge shapes the parsers write, so these tests do not
depend on any parser: a publisher edge is ``PUBLISHES`` to ``external:rabbitmq:<exchange>``
with ``routing_key``; a listener edge is ``HANDLES`` (and its ``CONSUMES`` twin) to the same
target with ``binding_key`` and ``queue``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.amqp_join import (
    MATCH_EXACT,
    MATCH_PATTERN,
    MATCH_UNKNOWN,
    has_wildcard,
    link_kind,
    topic_matches,
)
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools import registry_tools
from code_review_graph.tools.query import query_graph
from code_review_graph.tools.registry_tools import cross_repo_requests_func

EXCHANGE = "external:rabbitmq:orders.exchange"
SOURCE = (
    "def publish_created(): pass\n"
    "def publish_created_us(): pass\n"
    "def publish_cancelled(): pass\n"
    "def on_created(): pass\n"
    "def on_any_order(): pass\n"
    "def on_everything(): pass\n"
    "def on_nested(): pass\n"
    "def on_no_key(): pass\n"
    "def on_star_in_word(): pass\n"
)

PUBLISHERS = {
    "publish_created": "orders.created",
    "publish_created_us": "orders.created.us",
    "publish_cancelled": "orders.cancelled",
}
LISTENERS = {
    "on_created": "orders.created",
    "on_any_order": "orders.*",
    "on_everything": "#",
    "on_nested": "orders.#",
    "on_no_key": None,
    "on_star_in_word": "orders.cr*",
}


@pytest.mark.parametrize(
    ("binding", "routing", "expected"),
    [
        ("orders.created", "orders.created", True),
        ("orders.created", "orders.cancelled", False),
        ("orders.*", "orders.created", True),
        ("orders.#", "orders.created", True),
        ("#", "orders.created", True),
        ("#", "orders", True),
        ("orders.*", "orders.created.us", False),
        ("orders.#", "orders.created.us", True),
        ("orders.*", "orders", False),
        ("orders.#", "orders", True),
        ("#.created", "orders.created", True),
        ("#.created", "created", True),
        ("*.created", "created", False),
        ("*.*.us", "orders.created.us", True),
        ("orders.#.us", "orders.created.eu.us", True),
        ("orders.#.us", "orders.us", True),
        ("orders.#.us", "orders.created.eu", False),
        ("#.#", "a.b.c", True),
        # A wildcard is a whole word: inside a word it is plain text.
        ("orders.cr*", "orders.created", False),
        ("orders.cr*", "orders.cr*", True),
        # Without a wildcard the keys compare exactly, and a prefix is not a match.
        ("orders.created", "orders.created.us", False),
        ("orders", "orders.created", False),
        # Empty words are words.
        ("orders.*", "orders.", True),
    ],
)
def test_topic_matches_table(binding: str, routing: str, expected: bool) -> None:
    assert topic_matches(binding, routing) is expected


def test_topic_matches_stays_bounded_on_huge_keys() -> None:
    huge = ".".join(["a"] * 500)
    # Past the word cap the keys are plain text: no pattern work, equality only.
    assert topic_matches("#", huge) is False
    assert topic_matches(huge, huge) is True
    assert topic_matches("#." * 40 + "x", "a." * 200 + "y") is False


def test_has_wildcard_counts_whole_words_only() -> None:
    assert has_wildcard("orders.*") and has_wildcard("#") and has_wildcard("a.#.b")
    assert not has_wildcard("orders.cr*") and not has_wildcard("orders.created")


def test_link_kind_says_how_the_keys_met() -> None:
    assert link_kind("orders.created", "orders.created") == MATCH_EXACT
    assert link_kind("orders.*", "orders.created") == MATCH_PATTERN
    assert link_kind("orders.cancelled", "orders.created") is None
    assert link_kind(None, "orders.created") == MATCH_UNKNOWN
    assert link_kind("orders.*", None) == MATCH_UNKNOWN


def _build_repo(
    root: Path, exchange_edges: bool, extra_source: str = "", publisher_kind: str = "PUBLISHES",
) -> Path:
    """A repo with one module whose functions carry hand-made AMQP edges (or none)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    module = root / "svc.py"
    module.write_text(SOURCE + extra_source, encoding="utf-8")
    nodes, edges = CodeParser(root).parse_file(module)
    if exchange_edges:
        path = module.as_posix()
        for line, (name, key) in enumerate(PUBLISHERS.items(), start=1):
            edges.append(EdgeInfo(
                kind=publisher_kind, source=f"{path}::{name}", target=EXCHANGE,
                file_path=path, line=line, extra={"routing_key": key},
            ))
        for line, (name, key) in enumerate(LISTENERS.items(), start=10):
            extra: dict = {"resolution": "rabbit_listener_binding", "queue": f"q.{name}"}
            if key is not None:
                extra["binding_key"] = key
            for kind in ("CONSUMES", "HANDLES"):
                edges.append(EdgeInfo(
                    kind=kind, source=f"{path}::{name}", target=EXCHANGE,
                    file_path=path, line=line, extra=dict(extra),
                ))
    directory = root / ".code-review-graph"
    directory.mkdir(exist_ok=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(str(module), nodes, edges)
    return root


def _names(reply: dict) -> dict[str, str | None]:
    return {row["name"]: row.get("matched_via") for row in reply["results"]}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _build_repo(tmp_path / "repo", exchange_edges=True)


class TestListenersOf:
    def test_without_a_key_every_listener_is_listed_with_its_keys(self, repo):
        reply = query_graph("listeners_of", EXCHANGE, repo_root=str(repo))
        assert reply["result_count"] == len(LISTENERS)
        rows = {row["name"]: row for row in reply["results"]}
        assert rows["on_any_order"]["binding_key"] == "orders.*"
        assert rows["on_any_order"]["queue"] == "q.on_any_order"
        assert "binding_key" not in rows["on_no_key"]
        assert all("matched_via" not in row for row in rows.values())

    def test_a_routing_key_keeps_the_listeners_whose_binding_matches(self, repo):
        reply = query_graph(
            "listeners_of", EXCHANGE, repo_root=str(repo), routing_key="orders.created",
        )
        assert _names(reply) == {
            "on_created": MATCH_EXACT,
            "on_any_order": MATCH_PATTERN,
            "on_everything": MATCH_PATTERN,
            "on_nested": MATCH_PATTERN,
            "on_no_key": MATCH_UNKNOWN,
        }
        assert "orders.created" in reply["summary"]

    def test_one_word_wildcard_does_not_span_two_words(self, repo):
        reply = query_graph(
            "listeners_of", EXCHANGE, repo_root=str(repo), routing_key="orders.created.us",
        )
        assert _names(reply) == {
            "on_everything": MATCH_PATTERN,
            "on_nested": MATCH_PATTERN,
            "on_no_key": MATCH_UNKNOWN,
        }

    def test_minimal_detail_keeps_the_match_marker(self, repo):
        reply = query_graph(
            "listeners_of", EXCHANGE, repo_root=str(repo), routing_key="orders.created",
            detail_level="minimal", max_results=10,
        )
        assert reply["results"]
        assert {row["matched_via"] for row in reply["results"]} <= {
            MATCH_EXACT, MATCH_PATTERN, MATCH_UNKNOWN,
        }

    def test_repository_strings_are_sanitised(self, tmp_path):
        root = _build_repo(tmp_path / "evil", exchange_edges=False)
        module = root / "svc.py"
        nodes, edges = CodeParser(root).parse_file(module)
        path = module.as_posix()
        edges.append(EdgeInfo(
            kind="HANDLES", source=f"{path}::on_created", target=EXCHANGE, file_path=path,
            line=3, extra={"binding_key": "orders.\x07#\x1b[2J", "queue": "q\x00" + "x" * 400},
        ))
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            store.store_file_nodes_edges(str(module), nodes, edges)
        reply = query_graph("listeners_of", EXCHANGE, repo_root=str(root))
        row = reply["results"][0]
        assert "\x07" not in row["binding_key"] and "\x1b" not in row["binding_key"]
        assert "\x00" not in row["queue"] and len(row["queue"]) <= 256


class TestPublishersOf:
    def test_without_a_key_every_publisher_is_listed_with_its_routing_key(self, repo):
        reply = query_graph("publishers_of", EXCHANGE, repo_root=str(repo))
        assert reply["status"] == "ok"
        rows = {row["name"]: row for row in reply["results"]}
        assert set(rows) == set(PUBLISHERS)
        assert rows["publish_created"]["routing_key"] == "orders.created"

    def test_a_binding_pattern_keeps_the_publishers_it_covers(self, repo):
        reply = query_graph(
            "publishers_of", EXCHANGE, repo_root=str(repo), routing_key="orders.*",
        )
        assert _names(reply) == {
            "publish_created": MATCH_PATTERN, "publish_cancelled": MATCH_PATTERN,
        }
        reply = query_graph(
            "publishers_of", EXCHANGE, repo_root=str(repo), routing_key="orders.#",
        )
        assert set(_names(reply)) == set(PUBLISHERS)

    def test_a_key_without_a_wildcard_is_exact(self, repo):
        reply = query_graph(
            "publishers_of", EXCHANGE, repo_root=str(repo), routing_key="orders.created",
        )
        assert _names(reply) == {"publish_created": MATCH_EXACT}


def test_routing_key_is_refused_where_it_means_nothing(repo):
    with pytest.raises(ValueError, match="routing_key"):
        query_graph("callers_of", "publish_created", repo_root=str(repo), routing_key="x")
    with pytest.raises(ValueError, match="routing_key"):
        query_graph("listeners_of", "event::demo.Evt", repo_root=str(repo), routing_key="x")


class TestZeroNote:
    def test_a_graph_with_no_amqp_edges_says_the_zero_is_about_the_graph(self, tmp_path):
        root = _build_repo(tmp_path / "plain", exchange_edges=False)
        for pattern in ("listeners_of", "publishers_of"):
            reply = query_graph(pattern, EXCHANGE, repo_root=str(root))
            assert reply["result_count"] == 0
            note = reply["confidence"]
            assert "no AMQP bindings are indexed" in note
            assert "nothing about the brokers" in note
            assert len(note) <= 300

    def test_a_graph_with_amqp_edges_keeps_the_usual_note(self, repo):
        reply = query_graph(
            "listeners_of", "external:rabbitmq:other.exchange", repo_root=str(repo),
        )
        assert reply["result_count"] == 0
        assert "no AMQP bindings are indexed" not in reply.get("confidence", "")

    def test_a_key_that_matches_nothing_keeps_the_usual_note(self, repo):
        reply = query_graph(
            "listeners_of", EXCHANGE, repo_root=str(repo), routing_key="billing.paid",
        )
        assert _names(reply) == {"on_everything": MATCH_PATTERN, "on_no_key": MATCH_UNKNOWN}
        reply = query_graph(
            "publishers_of", EXCHANGE, repo_root=str(repo), routing_key="billing.paid",
        )
        assert reply["result_count"] == 0
        assert "no AMQP bindings are indexed" not in reply.get("confidence", "")

    def test_another_transport_keeps_its_own_note(self, tmp_path):
        root = _build_repo(tmp_path / "plain", exchange_edges=False)
        reply = query_graph("listeners_of", "external:aws-sqs:orders", repo_root=str(root))
        assert "no AMQP bindings" not in reply.get("confidence", "")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_CROSS_REPO_BUDGET_SECONDS", raising=False)
    return Registry()


@pytest.fixture
def two_repos(tmp_path, registry):
    """One repo that only publishes, one that only listens, one with no AMQP edges."""
    publisher = _build_repo(tmp_path / "pub", exchange_edges=True)
    listener = _build_repo(tmp_path / "sub", exchange_edges=True)
    plain = _build_repo(tmp_path / "plain", exchange_edges=False)
    # Make each side own one role only, by dropping the other role's edges.
    for root, drop in ((publisher, "HANDLES"), (listener, "PUBLISHES")):
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            store._conn.execute("DELETE FROM edges WHERE kind IN (?, 'CONSUMES')", (drop,))
            store._conn.commit()
    registry.register(str(publisher), alias="pub")
    registry.register(str(listener), alias="sub")
    registry.register(str(plain), alias="plain")
    return tmp_path


class TestCrossRepoAmqp:
    def test_lists_both_sides_across_repos(self, two_repos):
        reply = cross_repo_requests_func(origin="amqp", service="orders.exchange")
        assert reply["status"] == "ok" and reply["origin"] == "amqp"
        roles = {(row["repo"], row["role"]) for row in reply["results"]}
        assert roles == {("pub", "publisher"), ("sub", "listener")}
        assert reply["counts"] == {"publisher": 3, "listener": len(LISTENERS)}
        assert reply["total"] == 3 + len(LISTENERS)
        assert reply["repos_without_amqp"] == ["plain"]
        assert all(row["exchange"] == "orders.exchange" for row in reply["results"])
        assert all("matched_via" not in row for row in reply["results"])

    def test_the_prefixed_exchange_name_is_accepted(self, two_repos):
        reply = cross_repo_requests_func(origin="amqp", service=EXCHANGE)
        assert reply["total"] == 3 + len(LISTENERS)

    def test_a_key_links_publishers_to_listener_patterns(self, two_repos):
        reply = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", route="orders.created",
        )
        by_role: dict[str, dict[str, str]] = {"publisher": {}, "listener": {}}
        for row in reply["results"]:
            by_role[row["role"]][row["source"]] = row["matched_via"]
        assert by_role["listener"] == {
            "on_created": MATCH_EXACT, "on_any_order": MATCH_PATTERN,
            "on_everything": MATCH_PATTERN, "on_nested": MATCH_PATTERN,
            "on_no_key": MATCH_UNKNOWN,
        }
        assert by_role["publisher"] == {"publish_created": MATCH_EXACT}

    def test_status_keeps_one_role(self, two_repos):
        reply = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", status="listener",
        )
        assert {row["role"] for row in reply["results"]} == {"listener"}
        assert reply["counts"]["publisher"] == 3
        with pytest.raises(ValueError, match="status"):
            cross_repo_requests_func(origin="amqp", service="orders.exchange", status="x")

    def test_without_an_exchange_every_exchange_is_listed(self, two_repos):
        reply = cross_repo_requests_func(origin="amqp")
        assert reply["status"] == "ok"
        assert [r["exchange"] for r in reply["results"]] == ["orders.exchange"]

    def test_unknown_repos_are_reported_and_results_are_bounded(self, two_repos):
        reply = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", repos=["pub", "nope"], max_results=2,
        )
        assert reply["unknown"] == ["nope"]
        assert reply["repos_searched"] == 1
        assert len(reply["results"]) == 2 and reply["truncated"] is True
        assert reply["total"] == 3

    def test_repos_left_after_the_budget_are_named(self, two_repos, monkeypatch):
        monkeypatch.setenv("CRG_CROSS_REPO_BUDGET_SECONDS", "1")
        clock = iter([0.0, 0.5, 5.0, 5.0, 5.0])
        monkeypatch.setattr(registry_tools, "_now", lambda: next(clock))
        reply = cross_repo_requests_func(origin="amqp", service="orders.exchange")
        assert reply["skipped_repos"] == ["sub", "plain"]
        assert reply["truncated"] is True and reply["repos_searched"] == 1

    def test_the_join_writes_nothing(self, two_repos):
        db = two_repos / "pub" / ".code-review-graph" / "graph.db"
        with GraphStore(db, read_only=True) as store:
            before = store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        cross_repo_requests_func(origin="amqp", service="orders.exchange")
        with GraphStore(db, read_only=True) as store:
            assert store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before


class TestProducesPublishers:
    """The messaging parser writes a publisher as ``PRODUCES``; it must read as a publisher."""

    @pytest.fixture
    def produces_repo(self, tmp_path: Path) -> Path:
        return _build_repo(tmp_path / "repo", exchange_edges=True, publisher_kind="PRODUCES")

    def test_publishers_of_lists_produces_edges_with_their_keys(self, produces_repo):
        reply = query_graph("publishers_of", EXCHANGE, repo_root=str(produces_repo))
        assert reply["result_count"] == len(PUBLISHERS)
        rows = {row["name"]: row.get("routing_key") for row in reply["results"]}
        assert rows == PUBLISHERS

    def test_a_binding_key_keeps_the_matching_produces_publishers(self, produces_repo):
        reply = query_graph(
            "publishers_of", EXCHANGE, repo_root=str(produces_repo), routing_key="orders.*",
        )
        assert reply["result_count"] >= 1
        assert all(row["matched_via"] for row in reply["results"])

    def test_an_exchange_with_only_listeners_says_what_does_end_there(self, tmp_path: Path):
        repo = _build_repo(tmp_path / "repo", exchange_edges=True)
        with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
            store._conn.execute("DELETE FROM edges WHERE kind IN ('PUBLISHES', 'PRODUCES')")
            store._conn.commit()
        reply = query_graph("publishers_of", EXCHANGE, repo_root=str(repo))
        assert reply["result_count"] == 0
        assert "other edges do" in reply["confidence"]
        assert "not indexed" not in reply["confidence"]

    def test_external_targets_prints_publisher_and_listener_keys(self, produces_repo):
        reply = query_graph("external_targets", "external:rabbitmq", repo_root=str(produces_repo))
        row = next(r for r in reply["results"] if r["target"] == EXCHANGE)
        assert sorted(row["routing_keys"]) == sorted(set(PUBLISHERS.values()))
        assert "orders.*" in row["binding_keys"]

    def test_an_external_target_with_no_edges_says_so(self, produces_repo):
        reply = query_graph(
            "publishers_of", "external:rabbitmq:nowhere", repo_root=str(produces_repo),
        )
        assert reply["result_count"] == 0
        assert reply["confidence"].startswith("no edges for this target")

    def test_the_cross_repo_join_lists_produces_publishers_with_keys(self, produces_repo):
        with GraphStore(produces_repo / ".code-review-graph" / "graph.db") as store:
            from code_review_graph.amqp_join import read_channel_edges
            rows = [r for r in read_channel_edges(store, EXCHANGE) if r["role"] == "publisher"]
        assert {r["routing_key"] for r in rows} == set(PUBLISHERS.values())

    def test_a_bare_exchange_name_is_read_as_the_exchange(self, produces_repo):
        bare = EXCHANGE.removeprefix("external:rabbitmq:")
        full = query_graph("publishers_of", EXCHANGE, repo_root=str(produces_repo))
        reply = query_graph("publishers_of", bare, repo_root=str(produces_repo))
        assert reply["status"] == "ok"
        assert _names(reply) == _names(full)
        listeners = query_graph("listeners_of", bare, repo_root=str(produces_repo))
        assert listeners["result_count"] == len(LISTENERS)

    def test_a_routing_key_works_with_a_bare_exchange_name(self, produces_repo):
        bare = EXCHANGE.removeprefix("external:rabbitmq:")
        reply = query_graph(
            "publishers_of", bare, repo_root=str(produces_repo), routing_key="orders.*",
        )
        assert reply["status"] == "ok" and reply["result_count"] >= 1

    def test_an_unknown_bare_name_is_still_not_found(self, produces_repo):
        reply = query_graph("publishers_of", "no.such.exchange", repo_root=str(produces_repo))
        assert reply["status"] == "not_found"

    def test_the_no_edges_note_keeps_the_whole_target(self, produces_repo):
        reply = query_graph(
            "publishers_of", "external:rabbitmq:nowhere", repo_root=str(produces_repo),
        )
        assert "external:rabbitmq:nowhere" in reply["confidence"]


class TestAmqpPages:
    """The ``amqp`` origin pages like the other cross-repo origins."""

    @staticmethod
    def read_all(page_size: int, **query):
        rows: list = []
        cursor = None
        for _ in range(100):
            reply = cross_repo_requests_func(
                origin="amqp", service="orders.exchange", max_results=page_size,
                cursor=cursor, **query,
            )
            rows.extend(reply["results"])
            cursor = reply.get("next_cursor")
            if cursor is None:
                return rows, reply
        raise AssertionError("paging did not end")

    def test_a_cut_list_carries_a_cursor_and_the_pages_add_up(self, two_repos):
        everything = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", max_results=100,
        )
        assert everything["total"] > 3 and "next_cursor" not in everything
        first = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", max_results=3,
        )
        assert first["truncated"] is True and first["next_cursor"]
        assert first["results"] == everything["results"][:3]
        rows, last = self.read_all(3)
        assert rows == everything["results"]
        assert last["truncated"] is False and last["offset"] > 0

    def test_a_cursor_for_another_exchange_or_key_is_refused(self, two_repos):
        from code_review_graph.paging import CursorError

        first = cross_repo_requests_func(
            origin="amqp", service="orders.exchange", max_results=3,
        )
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="amqp", service="orders.exchange", route="orders.created",
                max_results=3, cursor=first["next_cursor"],
            )
        with pytest.raises(CursorError):
            cross_repo_requests_func(
                origin="amqp", service="other.exchange", max_results=3,
                cursor=first["next_cursor"],
            )
