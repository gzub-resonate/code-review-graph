"""A repo's endpoints are stored when its graph is built and read back by the cross-repo tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph import endpoint_index
from code_review_graph import feign_links as fl
from code_review_graph.endpoint_index import (
    METADATA_KEY,
    build_endpoint_index,
    endpoint_stamp,
    extract_endpoints,
    read_endpoint_index,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

CLIENT = (
    "import axios from 'axios';\n"
    "export const list = () => axios.get('/api/items');\n"
    "export const make = () => axios.post('/api/items');\n"
    "export const gone = () => axios.get('/api/gone');\n"
)
SERVER = (
    "const express = require('express');\nconst app = express();\n"
    "app.get('/api/items', (req, res) => res.end());\n"
    "app.post('/api/items', (req, res) => res.end());\n"
)
FASTAPI = (
    "from fastapi import APIRouter, FastAPI\n"
    "app = FastAPI()\n"
    "router = APIRouter(prefix='/v1')\n"
    "@router.get('/things')\n"
    "def things():\n    return []\n"
    "app.include_router(router)\n"
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def make_repo(tmp_path, registry, name, files, *, alias=None):
    root = (tmp_path / name).resolve()
    (root / ".git").mkdir(parents=True)
    for rel, text in files.items():
        write(root, rel, text)
    with GraphStore(get_db_path(root)) as store:
        full_build(root, store)
    registry.register(str(root), alias=alias or name)
    return root


@pytest.fixture
def repos(tmp_path, registry):
    ui = make_repo(tmp_path, registry, "ui", {"src/api.ts": CLIENT})
    api = make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    return ui, api


def stored(root: Path):
    with GraphStore(get_db_path(root, read_only=True), read_only=True) as store:
        return store.get_metadata(METADATA_KEY), read_endpoint_index(store)


def ask(**args):
    args.setdefault("repos", ["ui"])
    return cross_repo_requests_func(**args)


def same_answer(a, b):
    keys = ("results", "counts", "total", "truncated", "repos_searched")
    return {k: a[k] for k in keys} == {k: b[k] for k in keys}


def drop_index(root: Path) -> None:
    with GraphStore(get_db_path(root)) as store:
        store._conn.execute("DELETE FROM metadata WHERE key = ?", (METADATA_KEY,))
        store.commit()


def test_a_build_stores_the_endpoints_it_found(repos):
    _ui, api = repos
    raw, found = stored(api)
    assert raw is not None and found is not None
    clients, servers = found
    assert clients == []
    assert {(s.http_method, s.route, s.file) for s in servers} == {
        ("GET", "/api/items", "server.js"), ("POST", "/api/items", "server.js"),
    }
    assert all(s.repo == "" and s.origin == "" for s in servers)


def test_the_tool_reads_the_stored_index_and_says_so(repos):
    reply = ask()
    assert reply["endpoint_index"] == {"stored": 2, "computed": 0}
    assert {r["status"] for r in reply["results"]} == {"matched", "no_route_match"}


def test_the_answer_is_the_same_without_the_stored_index(repos):
    ui, api = repos
    with_index = ask()
    drop_index(ui)
    drop_index(api)
    without = ask()
    assert without["endpoint_index"] == {"stored": 0, "computed": 2}
    assert same_answer(with_index, without)


def test_stored_rows_equal_what_the_nodes_give(repos):
    _ui, api = repos
    with GraphStore(get_db_path(api, read_only=True), read_only=True) as store:
        computed = extract_endpoints(store.get_nodes_by_kind(["Endpoint"]), api)
        assert read_endpoint_index(store) == computed


def test_every_flag_of_a_call_survives_the_round_trip():
    call = fl.ClientCall(
        repo="", file="a.ts", line=3, http_method="GET", route="/x/{id}", origin="js",
        client_kind="fetch", route_dynamic=True, method_dynamic=True, absolute_url=True,
        notes=("receiver_inferred", "base_url_not_literal"), query_keys=("a", "b"),
    )
    feign = fl.ClientCall(
        repo="", file=None, line=None, http_method="POST", route="/y", service="orders",
        service_unresolved=True, url_literal=True, url_from_argument=True,
    )
    server = fl.ServerEndpoint(
        repo="", file="S.java", line=9, http_method="GET", route="/z/{...p}",
        catch_all=True, optional_catch_all=True, params=("a", "!b"), headers=("X-Id",),
    )
    assert endpoint_index._client(endpoint_index._row(call, ("repo",))) == call
    assert endpoint_index._client(endpoint_index._row(feign, ("repo",))) == feign
    assert endpoint_index._server(endpoint_index._row(server, ("repo", "origin"))) == server


def test_the_rows_do_not_carry_the_registry_name_or_path(repos):
    _ui, api = repos
    raw, _found = stored(api)
    data = json.loads(raw)
    assert set(data) == {"v", "stamp", "clients", "servers"}
    assert all("repo" not in row for row in data["clients"] + data["servers"])
    assert all("origin" not in row for row in data["servers"])


def test_the_alias_comes_from_the_registry_not_the_index(tmp_path, registry):
    make_repo(tmp_path, registry, "ui", {"src/api.ts": CLIENT})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER}, alias="backend")
    row = next(r for r in ask()["results"] if r["status"] == "matched")
    assert row["client_repo"] == "ui"
    assert {c["repo"] for c in row["candidates"]} == {"backend"}


def test_an_edit_after_the_build_is_not_hidden_by_the_stored_index(repos):
    """The stamp covers the rows, so an update that left the index alone is detected."""
    _ui, api = repos
    with GraphStore(get_db_path(api)) as store:
        before = endpoint_stamp(store)
        node = store.get_nodes_by_kind(["Endpoint"])[0]
        extra = dict(node.extra)
        extra["route"] = "/api/renamed"
        store._conn.execute(
            "UPDATE nodes SET extra = ?, updated_at = updated_at + 1 WHERE id = ?",
            (json.dumps(extra), node.id),
        )
        store.commit()
        assert endpoint_stamp(store) != before
        assert read_endpoint_index(store) is None
    reply = ask()
    assert reply["endpoint_index"] == {"stored": 1, "computed": 1}
    assert {r["route"]: r["status"] for r in reply["results"] if r["http_method"] == "GET"} == {
        # /api/items is still served, but only with another HTTP method.
        "/api/items": "method_not_served", "/api/gone": "no_route_match",
    }


def test_an_update_refreshes_the_index(repos):
    _ui, api = repos
    write(api, "server.js", SERVER.replace("/api/items", "/api/products"))
    with GraphStore(get_db_path(api)) as store:
        incremental_update(api, store, changed_files=["server.js"])
    _raw, found = stored(api)
    assert found is not None
    assert {s.route for s in found[1]} == {"/api/products"}
    reply = ask()
    assert reply["endpoint_index"] == {"stored": 2, "computed": 0}
    assert {r["status"] for r in reply["results"]} == {"no_route_match"}


def test_removing_every_endpoint_removes_the_stored_index(repos):
    _ui, api = repos
    write(api, "server.js", "const x = 1;\n")
    with GraphStore(get_db_path(api)) as store:
        incremental_update(api, store, changed_files=["server.js"])
    raw, found = stored(api)
    assert raw is None and found is None


def test_a_repository_without_endpoints_stores_nothing(tmp_path, registry):
    root = make_repo(tmp_path, registry, "plain", {"a.py": "def f():\n    return 1\n"})
    assert stored(root) == (None, None)


@pytest.mark.parametrize("damage", [
    lambda d: d.update(v=99),
    lambda d: d.update(stamp="stale"),
    lambda d: d.update(servers=[{"unknown_field": 1}]),
    lambda d: d.pop("servers"),
])
def test_a_row_that_cannot_be_used_falls_back_to_the_nodes(repos, damage):
    _ui, api = repos
    good = ask()
    with GraphStore(get_db_path(api)) as store:
        data = json.loads(store.get_metadata(METADATA_KEY))
        damage(data)
        store.set_metadata(METADATA_KEY, json.dumps(data))
    reply = ask()
    assert reply["endpoint_index"] == {"stored": 1, "computed": 1}
    assert same_answer(good, reply)


def test_a_row_that_is_not_json_falls_back_to_the_nodes(repos):
    _ui, api = repos
    good = ask()
    with GraphStore(get_db_path(api)) as store:
        store.set_metadata(METADATA_KEY, "{not json")
    reply = ask()
    assert reply["endpoint_index"] == {"stored": 1, "computed": 1}
    assert same_answer(good, reply)


def test_a_python_route_is_stored_with_its_router_prefix_joined(tmp_path, registry):
    root = make_repo(tmp_path, registry, "svc", {"app.py": FASTAPI})
    _raw, found = stored(root)
    assert found is not None
    assert {(s.http_method, s.route) for s in found[1]} == {("GET", "/v1/things")}


def test_a_failure_in_the_index_does_not_fail_the_build(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("index broke")

    monkeypatch.setattr(endpoint_index, "build_endpoint_index", boom)
    root = (tmp_path / "r").resolve()
    (root / ".git").mkdir(parents=True)
    write(root, "server.js", SERVER)
    with GraphStore(get_db_path(root)) as store:
        result = full_build(root, store)
        assert result["total_nodes"] > 0
    assert stored(root) == (None, None)


def test_building_twice_gives_the_same_index(repos):
    _ui, api = repos
    first, _ = stored(api)
    with GraphStore(get_db_path(api)) as store:
        build_endpoint_index(store, api)
    second, _ = stored(api)
    assert json.loads(first)["servers"] == json.loads(second)["servers"]


def test_a_call_inferred_from_an_ember_data_model_keeps_its_note(tmp_path):
    """The note says the route comes from the adapter convention, not from a literal URL."""
    from code_review_graph.graph import GraphNode
    from code_review_graph.js_api_extract import CLIENT_TAG

    node = GraphNode(
        id=1, kind="Endpoint", name="GET /api/projects", qualified_name="e::1",
        file_path=str(tmp_path / "app/models/project.js"), line_start=3, line_end=3,
        language="javascript", parent_name=None, params=None, return_type=None,
        is_test=False, file_hash=None,
        extra={
            "role": "client", "client": CLIENT_TAG, "client_kind": "ember-data",
            "route": "/api/projects", "http_method": "GET", "inferred": True,
        },
    )
    clients, _servers = extract_endpoints([node], tmp_path)
    assert [c.notes for c in clients] == [("ember_data_convention",)]
    assert endpoint_index._client(endpoint_index._row(clients[0], ("repo",))) == clients[0]
