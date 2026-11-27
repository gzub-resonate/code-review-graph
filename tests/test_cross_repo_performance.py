"""Speed-ups of the Feign/JS join change no answer: the inverted route index, the all-repo server
index with one repo left out, the kept endpoint reads and the resumed skipped-repos list."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from code_review_graph import constants, repo_cache
from code_review_graph import feign_links as fl
from code_review_graph import js_route_leads as jl
from code_review_graph import server_route_index as sri
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools import registry_tools
from code_review_graph.tools.registry_tools import cross_repo_requests_func

METHODS = ["GET", "POST", "PUT"]
WORDS = ["users", "orders", "items", "{id}", "export", "v1", "v2", "admin", "{x}"]


def routes(rnd: random.Random, count: int) -> list[str]:
    return ["/" + "/".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 5)))
            for _ in range(count)]


def servers(rnd: random.Random, repo: str, count: int) -> list[fl.ServerEndpoint]:
    return [
        fl.ServerEndpoint(
            repo=repo, file="A.java", line=i, http_method=rnd.choice(METHODS), route=route,
            origin=f"/r/{repo}", catch_all=rnd.random() < 0.05,
        )
        for i, route in enumerate(routes(rnd, count))
    ]


@pytest.fixture
def world():
    rnd = random.Random(7)
    repos = [f"r{i}" for i in range(6)]
    every = [s for repo in repos for s in servers(rnd, repo, 80)]
    calls = [
        fl.ClientCall(repo="r0", file="c.ts", line=i, http_method=rnd.choice(METHODS),
                      route=route, origin="js")
        for i, route in enumerate(routes(rnd, 400))
    ]
    return repos, every, calls


def test_variable_hits_match_the_scan_over_every_route(world):
    _repos, every, calls = world
    index = fl.ServerIndex(every)
    for call in calls:
        assert sri.variable_hits(call, index) == fl._variable_hits(call, index)


def test_an_excluding_index_answers_as_an_index_without_that_repo(world):
    repos, every, calls = world
    everywhere = fl.ServerIndex(every)
    for repo in repos[:3]:
        others = fl.ServerIndex([s for s in every if s.origin != f"/r/{repo}"])
        view = sri.ExcludingIndex(everywhere, f"/r/{repo}")
        own = fl.ServerIndex([s for s in every if s.origin == f"/r/{repo}"])
        for call in calls:
            expected = fl.link_js_call(call, others, own)
            assert sri.link_js_call(call, view, own) == expected
            assert sri.refine_no_route_match(call, expected, view, own) == (
                jl.refine_no_route_match(call, expected, others, own)
            )


def test_a_variable_with_no_fixed_segment_keeps_the_old_pool():
    index = fl.ServerIndex([])
    assert list(sri.variable_pool(index, ("{}", "{}"))) == []
    assert list(sri.variable_pool(index, ("a", "{}"))) == []


# --- the join through the tool ---------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv(constants.CROSS_REPO_BUDGET_ENV, raising=False)
    repo_cache.MEMO.clear()
    repo_cache.SERVER_INDEXES.clear()
    return Registry()


def endpoint_repo(tmp_path, registry, name, *, clients=(), servers_=()):
    from code_review_graph.endpoint_index import build_endpoint_index

    root = (tmp_path / name).resolve()
    (root / ".git").mkdir(parents=True)
    path = str(root / "A.java")
    nodes = [NodeInfo(kind="File", name=path, file_path=path, line_start=1, line_end=1)]
    for i, route in enumerate(servers_):
        nodes.append(NodeInfo(
            kind="Endpoint", name=f"s{i}", file_path=path, line_start=i + 1, line_end=i + 1,
            extra={"role": "server", "http_method": "GET", "route": route}))
    for i, (service, route) in enumerate(clients):
        nodes.append(NodeInfo(
            kind="Endpoint", name=f"c{i}", file_path=path, line_start=50 + i, line_end=50 + i,
            extra={"role": "client", "client": "feign", "service": service,
                   "http_method": "GET", "route": route}))
    with GraphStore(get_db_path(root)) as store:
        for node in nodes:
            store.upsert_node(node)
        store.commit()
        build_endpoint_index(store, root)
    registry.register(str(root), alias=name)
    return root


def test_endpoint_reads_are_kept_until_the_graph_changes(tmp_path, registry, monkeypatch):
    endpoint_repo(tmp_path, registry, "api", servers_=["/v1/things"])
    root = endpoint_repo(tmp_path, registry, "web", clients=[("api", "/v1/things")])
    reads: list[str] = []
    real = registry_tools._load_endpoints_uncached
    monkeypatch.setattr(
        registry_tools, "_load_endpoints_uncached",
        lambda entry: (reads.append(entry["path"]), real(entry))[1],
    )
    first = cross_repo_requests_func(origin="feign")
    assert first["counts"] == {"matched": 1}
    assert len(reads) == 2
    again = cross_repo_requests_func(origin="feign")
    assert again["results"] == first["results"]
    assert len(reads) == 2
    with GraphStore(get_db_path(root)) as store:
        store.upsert_node(NodeInfo(
            kind="Endpoint", name="extra", file_path=str(root / "A.java"), line_start=90,
            line_end=90, extra={"role": "client", "client": "feign", "service": "api",
                                "http_method": "GET", "route": "/v1/other"}))
        store.commit()
    third = cross_repo_requests_func(origin="feign")
    assert len(reads) == 3
    assert third["total"] in (1, 2)


class Clock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_a_repeat_after_a_budget_skip_starts_with_the_skipped_repos(
    tmp_path, registry, monkeypatch,
):
    endpoint_repo(tmp_path, registry, "api", servers_=["/v1/a", "/v1/b"])
    for name in ("web1", "web2", "web3"):
        endpoint_repo(tmp_path, registry, name, clients=[("api", "/v1/a")])
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    first = cross_repo_requests_func(origin="feign", max_results=100)
    skipped = first["skipped_repos"]
    assert skipped
    assert first["resumes_skipped_on_repeat"] is True
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    second = cross_repo_requests_func(origin="feign", max_results=100)
    # the skipped repos come first: the first of them is among the repos the repeat searched
    assert skipped[0] in second["repos_searched"]


def test_a_complete_call_does_not_claim_a_resume(tmp_path, registry):
    endpoint_repo(tmp_path, registry, "api", servers_=["/v1/a"])
    endpoint_repo(tmp_path, registry, "web", clients=[("api", "/v1/a")])
    reply = cross_repo_requests_func(origin="feign")
    assert reply["resumes_skipped_on_repeat"] is False
    assert Path(reply["results"][0]["client_file"]).name == "A.java"
