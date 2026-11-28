"""``handlers_of`` on a route across repos: the cheap probe must not change any answer.

Loop 1 (perf): the call asked every registered repo with a full ``query_graph``, one after
another, and ran into the cross-repo time budget on a server whose graphs sit on a network
disk. A one-query probe now rules out repos that hold no Endpoint the route could name.
"""

from __future__ import annotations

import pytest

from code_review_graph import constants
from code_review_graph.graph import GraphStore, NodeInfo
from code_review_graph.incremental import get_db_path
from code_review_graph.registry import Registry
from code_review_graph.tools import registry_tools

ROUTES = {
    "api": ["/v1/items", "/v1/items/{id}", "/v2/Respondents/avmap", "/v1/user_items"],
    "web": ["/v1/other"],
    "ping": ["/health"],
}
TARGETS = [
    "GET /v1/items", "/v1/items", "POST /v1/items/{id}", "/items/{id}", "GET /v2/respondents/avmap",
    "/avmap", "GET /v1/user_items", "/user_items", "/v1/nothing", "GET /health", "/",
    "/v1/items/42", "DELETE /v1/{id}", "/v1/%", "/V1/ITEMS",
]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv(constants.CROSS_REPO_BUDGET_ENV, raising=False)
    reg = Registry()
    for name, routes in ROUTES.items():
        root = (tmp_path / name).resolve()
        (root / ".git").mkdir(parents=True)
        path = str(root / "A.java")
        with GraphStore(get_db_path(root)) as store:
            store.upsert_node(NodeInfo(
                kind="File", name=path, file_path=path, line_start=1, line_end=1))
            for i, route in enumerate(routes):
                store.upsert_node(NodeInfo(
                    kind="Endpoint", name=f"s{i}", file_path=path, line_start=i + 1,
                    line_end=i + 1,
                    extra={"role": "server", "http_method": "GET", "route": route}))
            store.commit()
        reg.register(str(root), alias=name)
    ghost = (tmp_path / "ghost").resolve()
    (ghost / ".git").mkdir(parents=True)
    reg.register(str(ghost), alias="ghost")  # registered, never built
    return reg


@pytest.mark.parametrize("target", TARGETS)
def test_probe_gives_the_answer_of_asking_every_repo(registry, monkeypatch, target):
    with_probe = registry_tools.handlers_of_route_across_repos(target)
    monkeypatch.setattr(
        registry_tools, "_probe_repos",
        lambda entries, pattern, names, deadline: [
            registry_tools._probe_repo(e, None, names, deadline) for e in entries
        ],
    )
    without_probe = registry_tools.handlers_of_route_across_repos(target)
    assert with_probe == without_probe
    assert with_probe["repos_without_graph"] == 1


def test_probe_skips_the_full_query_of_repos_that_cannot_match(registry, monkeypatch):
    asked: list[str] = []
    from code_review_graph.tools import query

    real = query.query_graph
    monkeypatch.setattr(
        query, "query_graph", lambda **kw: (asked.append(kw["repo_root"]), real(**kw))[1],
    )
    reply = registry_tools.handlers_of_route_across_repos("GET /v1/items")
    assert reply["repos_searched"] == 3
    assert [p.rsplit("/", 1)[-1] for p in asked] == ["api"]


def test_a_placeholder_last_segment_rules_nothing_out():
    assert registry_tools._route_probe_terms("GET /v1/items/{id}")[0] is None
    assert registry_tools._route_probe_terms("/v1/user_items")[0] == "%user\\_items%"
    assert registry_tools._route_probe_terms("not a route")[0] is None


def test_an_unreadable_graph_is_left_to_the_full_query(registry, tmp_path):
    entry = next(e for e in Registry().list_repos() if e["alias"] == "api")
    get_db_path(tmp_path / "api").write_bytes(b"not a database" * 100)
    assert registry_tools._probe_repo(entry, "%items%", ("x", "y"), None) == "maybe"
