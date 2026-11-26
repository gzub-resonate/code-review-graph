"""Join outbound Spring Feign calls to server endpoints across registered repos.

The pure rules live in ``code_review_graph.feign_links``; the registry-facing
tool is ``cross_repo_requests_func``.  Repository and service names below are
invented.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from code_review_graph import feign_links as fl
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/orders", "/orders"),
        ("/orders/", "/orders"),
        ("orders", "/orders"),
        ("/", "/"),
        ("", "/"),
        ("/orders?active=true", "/orders"),
        ("/orders/?a=1#frag", "/orders"),
        ("/orders/{id}", "/orders/{}"),
        ("/orders/{orderId}/items/{item}", "/orders/{}/items/{}"),
        ("/orders/{id:[0-9]{2}}", "/orders/{}"),
        ("//orders///{id}//", "/orders/{}"),
        ("/Orders", "/Orders"),
        ("/files/{name}.json", "/files/{}.json"),
        ("/orders/{id", "/orders/{id"),
    ],
)
def test_normalize_route(raw, expected):
    assert fl.normalize_route(raw) == expected


def test_route_case_is_kept():
    assert fl.normalize_route("/Orders") != fl.normalize_route("/orders")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Orders_Service", "orders-service"),
        ("orders--service", "orders-service"),
        ("orders_-_service", "orders-service"),
        (" orders-service ", "orders-service"),
    ],
)
def test_normalize_service_name(raw, expected):
    assert fl.normalize_service_name(raw) == expected


def test_normalization_does_not_strip_words():
    assert fl.normalize_service_name("orders") != fl.normalize_service_name("orders-service")


def _call(**kw):
    base = {
        "repo": "web", "file": "A.java", "line": 3, "http_method": "GET",
        "route": "/orders", "service": "orders-service",
    }
    base.update(kw)
    return fl.ClientCall(**base)


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"service": None}, "service_unresolved"),
        ({"service_unresolved": True}, "service_unresolved"),
        ({"url_from_argument": True}, "url_from_argument"),
        ({"url_literal": True}, "url_literal"),
        ({"route_dynamic": True}, "route_dynamic"),
        ({"route": "/a/${base}/b"}, "placeholder_in_route"),
        ({"route": "/a/<unresolved>/b"}, "placeholder_in_route"),
        ({}, None),
    ],
)
def test_unmatchable_reason(kw, reason):
    assert fl.unmatchable_reason(_call(**kw)) == reason


def test_unmatchable_reason_has_one_precedence():
    both = _call(service_unresolved=True, url_literal=True, route_dynamic=True)
    assert fl.unmatchable_reason(both) == "service_unresolved"
    assert fl.unmatchable_reason(_call(url_literal=True, route_dynamic=True)) == "url_literal"


def _server(route, method="GET", line=1, file="C.java", repo="orders"):
    return fl.ServerEndpoint(repo, file, line, method, route)


def test_link_matched_ambiguous_and_none():
    one = fl.link_call(_call(route="/orders/{id}"), [_server("/orders/{orderId}")])
    assert one.status == fl.MATCHED and len(one.candidates) == 1
    many = fl.link_call(
        _call(), [_server("/orders", line=1), _server("/orders/", line=9)],
    )
    assert many.status == fl.AMBIGUOUS and len(many.candidates) == 2
    none = fl.link_call(_call(), [_server("/other")])
    assert none.status == fl.NO_ROUTE_MATCH and none.candidates == ()


def test_link_requires_same_method_but_any_matches_every_method():
    assert fl.link_call(_call(), [_server("/orders", "POST")]).status == fl.NO_ROUTE_MATCH
    assert fl.link_call(_call(), [_server("/orders", "ANY")]).status == fl.MATCHED
    assert fl.link_call(_call(http_method="get"), [_server("/orders", "GET")]).status == fl.MATCHED


def test_link_variable_does_not_match_a_literal_segment():
    # A variable on the client is not evidence for a different, fixed route.
    assert fl.link_call(_call(route="/orders/{id}"), [_server("/orders/new")]).status == (
        fl.NO_ROUTE_MATCH
    )


def test_link_counts_one_location_once():
    twins = [_server("/orders", line=5), _server("/orders", line=5)]
    assert fl.link_call(_call(), twins).status == fl.MATCHED


def test_resolve_service_exact_then_normalized():
    names = {"orders-service": (0,), "billing_api": (1,)}

    def lookup(name):
        return names.get(name, ())

    folded = {"orders-service": {"orders-service"}, "billing-api": {"billing_api"}}
    assert fl.resolve_service("orders-service", lookup, folded) == fl.TargetResolution(
        (0,), how="exact",
    )
    assert fl.resolve_service("Billing-API", lookup, folded) == fl.TargetResolution(
        (1,), how="normalized",
    )
    missing = fl.resolve_service("shipping", lookup, folded)
    assert missing.indexes == () and missing.reason == "service_not_registered"


def test_resolve_service_normalized_fold_reaching_two_repos_is_refused():
    names = {"a-b": (0,), "a_b": (1,)}
    folded = {"a-b": {"a-b", "a_b"}}
    got = fl.resolve_service("A-B", lambda n: names.get(n, ()), folded)
    assert got.indexes == () and got.reason == "service_name_ambiguous"


def test_resolve_service_exact_may_select_sibling_checkouts():
    got = fl.resolve_service("x", lambda n: (0, 3) if n == "x" else (), {})
    assert got.indexes == (0, 3) and got.how == "exact"


# ---------------------------------------------------------------------------
# The tool, over real graph databases and a temp registry
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def _client(root, line, method, route, **extra):
    meta = {
        "role": "client", "client": "feign", "http_method": method, "route": route,
        "config_keys": [], "route_dynamic": False, "url_literal": False,
    }
    meta.update(extra)
    return NodeInfo(
        kind="Endpoint", name=f"call{line}@GetMapping[0:0] {method} {route}",
        file_path=str(root / "Client.java"), line_start=line, line_end=line,
        language="java", parent_name="Client", extra=meta,
    )


def _server_node(root, line, method, route):
    return NodeInfo(
        kind="Endpoint", name=f"h{line}@GetMapping[0:0] {method} {route}",
        file_path=str(root / "Controller.java"), line_start=line, line_end=line,
        language="java", parent_name="Controller",
        extra={"http_method": method, "route": route, "handler": f"h{line}"},
    )


def make_repo(tmp_path, registry, name, *, alias=None, nodes=()):
    root = (tmp_path / name).resolve()
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "Client.java"),
            [n for n in nodes if n.file_path.endswith("Client.java")], [],
        )
        store.store_file_nodes_edges(
            str(root / "Controller.java"),
            [n for n in nodes if n.file_path.endswith("Controller.java")], [],
        )
    registry.register(str(root), alias=alias)
    return root


def by_route(result):
    return {(r["http_method"], r["route"]): r for r in result["results"]}


@pytest.fixture
def world(tmp_path, registry):
    """A ``web`` repo whose calls land (or fail to) in ``orders-service``."""
    api = tmp_path / "orders-service"
    make_repo(tmp_path, registry, "orders-service", nodes=[
        _server_node(api.resolve(), 10, "GET", "/orders"),
        _server_node(api.resolve(), 20, "GET", "/orders/{orderId}"),
        _server_node(api.resolve(), 30, "POST", "/orders"),
        _server_node(api.resolve(), 40, "GET", "/dup"),
        _server_node(api.resolve(), 50, "GET", "/dup/"),
    ])
    web = tmp_path / "web"
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web.resolve(), 3, "GET", "/orders", service="orders-service"),
        _client(web.resolve(), 4, "GET", "/orders/{id}?full=1", service="orders-service"),
        _client(web.resolve(), 5, "DELETE", "/orders", service="orders-service"),
        _client(web.resolve(), 6, "GET", "/dup", service="orders-service"),
        _client(web.resolve(), 7, "GET", "/x", service="unknown-service"),
        _client(web.resolve(), 8, "GET", "/y", service_unresolved=True),
        _client(web.resolve(), 9, "GET", "/z", service="orders-service", url_literal=True),
        _client(web.resolve(), 11, "GET", "/z", service="orders-service", url_from_argument=True),
        _client(web.resolve(), 12, "GET", "/a/<unresolved>", service="orders-service"),
        _client(web.resolve(), 13, "GET", "/b", service="orders-service", route_dynamic=True),
        _client(web.resolve(), 14, "GET", "/Orders", service="Orders_Service"),
    ])


def test_every_status_and_reason(world):
    result = cross_repo_requests_func(repos=["web"])
    assert result["status"] == "ok"
    rows = {(r["client_line"]): r for r in result["results"]}
    assert rows[3]["status"] == "matched"
    assert rows[3]["candidates"][0] == {
        "repo": "orders-service", "file": "Controller.java", "line": 10,
        "http_method": "GET", "route": "/orders",
    }
    assert rows[3]["target_resolution"] == "exact"
    assert rows[4]["status"] == "matched"
    assert rows[4]["candidates"][0]["route"] == "/orders/{orderId}"
    assert rows[5]["status"] == "no_route_match"  # wrong method: still no lead
    assert rows[6]["status"] == "ambiguous" and rows[6]["candidates_total"] == 2
    assert rows[7]["status"] == "no_target_repo"
    assert rows[7]["reason"] == "service_not_registered"
    assert rows[7]["no_target_reason"] == "repo_not_registered"
    assert result["counts_by_reason"] == {"repo_not_registered": 1}
    assert rows[8]["reason"] == "service_unresolved"
    assert rows[9]["reason"] == "url_literal"
    assert rows[11]["reason"] == "url_from_argument"
    assert rows[12]["reason"] == "placeholder_in_route"
    assert rows[13]["reason"] == "route_dynamic"
    assert rows[14]["status"] == "case_mismatch"  # "/Orders" against the "/orders" controller
    assert result["counts"] == {
        "matched": 2, "case_mismatch": 1, "no_route_match": 1, "ambiguous": 1,
        "no_target_repo": 1, "unmatchable": 5,
    }
    assert result["total"] == 11
    assert result["truncated"] is False
    assert result["repos_searched"] == ["web"]


def test_route_case_is_not_folded_so_wrong_case_never_matches(world):
    row = by_route(cross_repo_requests_func(repos=["web"], route="/Orders"))[("GET", "/Orders")]
    # The service name folds to the repo; the route differs only in case, so it
    # is a lead (``case_mismatch``) and never a match.
    assert row["target_resolution"] == "normalized"
    assert row["status"] == "case_mismatch"
    assert row["confidence"] == "low" and row["reason"] == "route_differs_by_case"
    assert row["candidates"][0]["route"] == "/orders"


def test_alias_and_alias_file_resolve_service_names(tmp_path, registry):
    root = (tmp_path / "orders-backend").resolve()
    make_repo(tmp_path, registry, "orders-backend", alias="orders", nodes=[
        _server_node(root, 1, "GET", "/orders"),
    ])
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, 1, "GET", "/orders", service="orders"),
        _client(web, 2, "GET", "/orders", service="legacy-orders"),
        _client(web, 3, "GET", "/orders", service="orders-backend"),
    ])
    (tmp_path / "home" / "repo-aliases.toml").write_text(
        '[aliases]\nlegacy-orders = "orders"\n', encoding="utf-8",
    )
    rows = {r["client_line"]: r for r in cross_repo_requests_func(repos=["web"])["results"]}
    assert {rows[1]["status"], rows[2]["status"], rows[3]["status"]} == {"matched"}
    assert all(r["target_resolution"] == "exact" for r in rows.values())
    # The candidate repo is reported by its alias.
    assert rows[2]["candidates"][0]["repo"] == "orders"


def test_normalized_name_reaching_two_repos_is_not_guessed(tmp_path, registry):
    for name in ("pay-api", "pay_api"):
        root = (tmp_path / name).resolve()
        make_repo(tmp_path, registry, name, nodes=[_server_node(root, 1, "GET", "/p")])
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[_client(web, 1, "GET", "/p", service="PAY-API")])
    row = cross_repo_requests_func(repos=["web"])["results"][0]
    assert row["status"] == "no_target_repo"
    assert row["reason"] == "service_name_ambiguous"
    assert "candidates" not in row


def test_filters(world):
    only = cross_repo_requests_func(repos=["web"], service="orders-service", route="/orders")
    assert {r["client_line"] for r in only["results"]} == {3, 4, 5}
    assert cross_repo_requests_func(repos=["web"], status="ambiguous")["total"] == 1
    assert cross_repo_requests_func(repos=["web"], service="nope")["total"] == 0
    root = cross_repo_requests_func(repos=["web"], route="/")
    assert root["total"] == 11
    with pytest.raises(ValueError, match="status"):
        cross_repo_requests_func(status="bogus")


def test_bounded_output_and_counts_cover_everything(tmp_path, registry):
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, i + 1, "GET", f"/r{i}", service="nobody") for i in range(30)
    ])
    other = (tmp_path / "other").resolve()
    make_repo(tmp_path, registry, "other", nodes=[
        _client(other, i + 1, "GET", f"/r{i}", service="nobody") for i in range(30)
    ])
    capped = cross_repo_requests_func(limit=5, max_results=7)
    assert len(capped["results"]) == 7
    assert capped["total"] == 60
    assert capped["counts"] == {"no_target_repo": 60}
    assert capped["truncated"] is True
    assert "showing 7 of 60" in capped["summary"]
    per_repo = cross_repo_requests_func(limit=5, max_results=100)
    assert len(per_repo["results"]) == 10
    assert per_repo["truncated"] is True
    assert len(cross_repo_requests_func(max_results=1000, limit=1000)["results"]) == 60
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            cross_repo_requests_func(limit=bad)
        with pytest.raises(ValueError):
            cross_repo_requests_func(max_results=bad)


def test_candidates_are_bounded(tmp_path, registry):
    api = (tmp_path / "api").resolve()
    make_repo(tmp_path, registry, "api", nodes=[
        _server_node(api, i + 1, "GET", "/same") for i in range(9)
    ])
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[_client(web, 1, "GET", "/same", service="api")])
    row = cross_repo_requests_func(repos=["web"])["results"][0]
    assert row["status"] == "ambiguous"
    assert row["candidates_total"] == 9 and len(row["candidates"]) == 5


def test_unknown_and_ambiguous_repo_names_are_reported(world, tmp_path, registry):
    result = cross_repo_requests_func(repos=["web", "nope"])
    assert result["unknown"] == ["nope"] and result["unknown_total"] == 1
    assert result["repos_searched"] == ["web"]
    assert "matched no repository" in result["summary"]
    none = cross_repo_requests_func(repos=["nope"])
    assert none["results"] == [] and none["repos_searched"] == []
    assert none["unknown"] == ["nope"]


def test_client_filter_does_not_hide_targets(world):
    # Only the caller is selected; the target repo still answers.
    result = cross_repo_requests_func(repos=["web"], status="matched")
    assert result["total"] == 2


def test_empty_registry(registry):
    result = cross_repo_requests_func()
    assert result["status"] == "ok" and result["results"] == [] and result["total"] == 0


def test_repo_without_a_database_is_reported_not_fatal(world, tmp_path, registry):
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    registry.register(str(bare))
    result = cross_repo_requests_func()
    assert result["status"] == "ok"
    assert result["unreadable"] == [{"repo": "bare", "role": "client", "reason": "no_database"}]
    assert "could not be read" in result["summary"]
    assert result["counts"]["matched"] == 2


def test_unreadable_database_is_reported_and_message_is_not_leaked(world, tmp_path, registry):
    junk = tmp_path / "junk"
    (junk / ".code-review-graph").mkdir(parents=True)
    (junk / ".code-review-graph" / "graph.db").write_bytes(b"this is not sqlite" * 100)
    registry.register(str(junk))
    result = cross_repo_requests_func()
    assert result["unreadable"] == [{"repo": "junk", "role": "client", "reason": "unreadable"}]
    assert str(tmp_path) not in repr(result["unreadable"])
    assert result["counts"]["matched"] == 2


def test_unreadable_target_is_not_reported_as_no_route_match(tmp_path, registry):
    junk = tmp_path / "ghost-service"
    (junk / ".code-review-graph").mkdir(parents=True)
    (junk / ".code-review-graph" / "graph.db").write_bytes(b"not sqlite" * 100)
    registry.register(str(junk))
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, 1, "GET", "/p", service="ghost-service"),
    ])
    result = cross_repo_requests_func(repos=["web"])
    row = result["results"][0]
    assert row["status"] == "no_target_repo" and row["reason"] == "target_unreadable"
    assert {"repo": "ghost-service", "role": "target", "reason": "unreadable"} in (
        result["unreadable"]
    )


def test_sibling_checkouts_sharing_a_folder_name_are_both_searched(tmp_path, registry):
    for parent in ("one", "two"):
        root = (tmp_path / parent / "orders-service").resolve()
        make_repo(tmp_path, registry, f"{parent}/orders-service", nodes=[
            _server_node(root, 1, "GET", "/orders"),
        ])
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, 1, "GET", "/orders", service="orders-service"),
    ])
    row = cross_repo_requests_func(repos=["web"])["results"][0]
    assert row["status"] == "ambiguous" and row["candidates_total"] == 2


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_databases_are_not_modified(world, tmp_path):
    dbs = sorted(tmp_path.glob("*/.code-review-graph/graph.db"))
    assert len(dbs) == 2
    # Fold the write-ahead log into the file, as a finished build leaves it.
    for db in dbs:
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    before = {db: _digest(db) for db in dbs}
    listing = sorted(p.name for p in tmp_path.glob("*/.code-review-graph/*"))
    cross_repo_requests_func()
    cross_repo_requests_func(repos=["web"], status="matched")
    assert {db: _digest(db) for db in dbs} == before
    assert sorted(p.name for p in tmp_path.glob("*/.code-review-graph/*")) == listing


def test_read_only_connection_rejects_writes(world, tmp_path, monkeypatch):
    seen = []
    real = GraphStore.__init__

    def spy(self, *args, **kwargs):
        real(self, *args, **kwargs)
        seen.append(self.read_only)

    monkeypatch.setattr(GraphStore, "__init__", spy)
    cross_repo_requests_func()
    assert seen and all(seen)


def test_files_outside_the_repo_are_not_echoed(tmp_path, registry):
    web = (tmp_path / "web").resolve()
    node = _client(web, 1, "GET", "/p", service="nobody")
    node.file_path = "/somewhere/else/Client.java"
    root = make_repo(tmp_path, registry, "web", nodes=[])
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.store_file_nodes_edges("/somewhere/else/Client.java", [node], [])
    row = cross_repo_requests_func()["results"][0]
    assert row["client_file"] is None
    assert "somewhere" not in repr(row)


def test_only_feign_clients_are_listed(tmp_path, registry):
    web = (tmp_path / "web").resolve()
    other = _client(web, 2, "GET", "/p", service="nobody", client="resttemplate")
    plain = _server_node(web, 3, "GET", "/p")
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, 1, "GET", "/p", service="nobody"), other, plain,
    ])
    result = cross_repo_requests_func()
    assert [r["client_line"] for r in result["results"]] == [1]


def test_case_only_difference_is_a_case_mismatch_lead():
    """A route that differs only in letter case is a low-confidence lead, not a match."""
    result = fl.link_call(_call(route="/Orders"), [_server("/orders")])
    assert result.status == fl.CASE_MISMATCH
    assert result.reason == fl.CASE_MISMATCH_REASON
    assert result.confidence == "low"
    assert [c.route for c in result.candidates] == ["/orders"]


def test_case_mismatch_keeps_method_and_catch_all_rules():
    call = _call(route="/Orders")
    assert fl.link_call(call, [_server("/orders", "POST")]).status == fl.NO_ROUTE_MATCH
    assert fl.link_call(call, [_server("/orders", "ANY")]).status == fl.CASE_MISMATCH
    wild = fl.ServerEndpoint("s", "A.java", 1, "GET", "/orders", catch_all=True)
    assert fl.link_call(call, [wild]).status == fl.NO_ROUTE_MATCH


def test_exact_match_wins_over_a_case_variant():
    result = fl.link_call(
        _call(route="/orders"), [_server("/Orders", line=1), _server("/orders", line=2)],
    )
    assert result.status == fl.MATCHED and result.confidence == "high"
    assert [c.route for c in result.candidates] == ["/orders"]


def test_case_mismatch_is_listed_among_the_statuses():
    assert fl.CASE_MISMATCH in fl.STATUSES


@pytest.mark.parametrize(
    ("service", "reason", "expected"),
    [
        ("Auth0", "service_not_registered", "external_vendor"),
        ("auth0api", "service_not_registered", "external_vendor"),
        ("salesforce", "service_not_registered", "external_vendor"),
        ("ship", "service_not_registered", "repo_not_registered"),
        ("constellation-ai-agents", "service_not_registered", "repo_not_registered"),
        ("salesforce", "target_unreadable", "target_unreadable"),
        (None, "service_not_registered", "repo_not_registered"),
    ],
)
def test_no_target_reason_rules(service, reason, expected):
    from code_review_graph.external_hosts import no_target_reason

    assert no_target_reason(service, reason) == expected


def test_no_target_reason_rows_counts_and_status_filter(tmp_path, registry):
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", nodes=[
        _client(web, 1, "POST", "/oauth/token", service="Auth0"),
        _client(web, 2, "POST", "/services/oauth2/token", service="salesforce"),
        _client(web, 3, "GET", "/v1/x", service="internal-thing"),
    ])
    result = cross_repo_requests_func(repos=["web"])
    rows = {r["client_line"]: r for r in result["results"]}
    assert rows[1]["no_target_reason"] == "external_vendor"
    assert rows[2]["no_target_reason"] == "external_vendor"
    assert rows[3]["no_target_reason"] == "repo_not_registered"
    assert all(r["status"] == "no_target_repo" for r in rows.values())
    assert result["counts"] == {"no_target_repo": 3}
    assert result["counts_by_reason"] == {"external_vendor": 2, "repo_not_registered": 1}

    vendors = cross_repo_requests_func(repos=["web"], status="external_vendor")
    assert vendors["total"] == 2
    assert vendors["counts_by_reason"] == {"external_vendor": 2}
    assert {r["client_line"] for r in vendors["results"]} == {1, 2}
    internal = cross_repo_requests_func(repos=["web"], status="repo_not_registered")
    assert [r["client_line"] for r in internal["results"]] == [3]
    assert cross_repo_requests_func(repos=["web"], status="no_target_repo")["total"] == 3


def test_matched_rows_have_no_no_target_reason(world):
    result = cross_repo_requests_func(repos=["web"])
    for row in result["results"]:
        if row["status"] != "no_target_repo":
            assert "no_target_reason" not in row
