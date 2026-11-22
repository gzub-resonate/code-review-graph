# ruff: noqa: E501
"""HTTP routes and API calls in JavaScript and TypeScript, and their cross-repo join.

Parse-time cases build files in a temp directory and read the nodes and edges
back; join cases index real files into graph databases registered in a temp
registry.  Repository, service and route names are invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import feign_links as fl
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse(root: Path, rel: str, text: str | None = None):
    path = write(root, rel, text) if text is not None else root / rel
    return CodeParser(root).parse_file(path)


def clients(nodes):
    return [n for n in nodes if n.kind == "Endpoint" and n.extra.get("role") == "client"]


def servers(nodes):
    return [n for n in nodes if n.kind == "Endpoint" and n.extra.get("role") != "client"]


def sigs(nodes):
    return sorted((n.extra["http_method"], n.extra["route"]) for n in nodes)


def edges_of(edges, kind):
    return [e for e in edges if e.kind == kind]


# ---------------------------------------------------------------------------
# Client calls
# ---------------------------------------------------------------------------


def test_axios_instance_with_literal_base_url_prefixes_the_route(tmp_path):
    nodes, edges = parse(tmp_path, "src/api.ts", """
import axios from 'axios';
const apiClient = axios.create({ baseURL: '/api/v2/' });
export async function loadTag(id: string) {
  return apiClient.get(`/tags/${id}`);
}
export function saveTag(body: unknown) {
  return apiClient.post('tags', body);
}
""")
    found = clients(nodes)
    assert sigs(found) == [("GET", "/api/v2/tags/{param}"), ("POST", "/api/v2/tags")]
    assert all(n.extra["client"] == "js_http" and n.extra["origin"] == "js" for n in found)
    assert {n.extra["base_url"] for n in found} == {"/api/v2/"}
    requests = edges_of(edges, "REQUESTS")
    assert {e.source.split("::")[-1] for e in requests} == {"loadTag", "saveTag"}
    # The edge target is the endpoint node, as for Feign.
    names = {f"{n.file_path}::{n.name}" for n in found}
    assert {e.target for e in requests} == names


def test_axios_default_verbs_and_config_form(tmp_path):
    nodes, _ = parse(tmp_path, "src/a.js", """
import axios from 'axios';
export const a = () => axios.get('/one');
export const b = () => axios.put('/two', {});
export const c = () => axios.patch('/three', {});
export const d = () => axios.delete('/four');
export const e = () => axios({ method: 'post', url: '/five' });
export const f = () => axios.request({ url: '/six' });
""")
    assert sigs(clients(nodes)) == [
        ("DELETE", "/four"), ("GET", "/one"), ("GET", "/six"), ("PATCH", "/three"),
        ("POST", "/five"), ("PUT", "/two"),
    ]


def test_base_url_that_is_not_a_literal_is_flagged_not_applied(tmp_path):
    nodes, _ = parse(tmp_path, "src/a.ts", """
import axios from 'axios';
const http = axios.create({ baseURL: process.env.API });
export const a = () => http.get('/x');
""")
    (node,) = clients(nodes)
    assert node.extra["route"] == "/x" and node.extra["base_url_dynamic"] is True
    assert "base_url" not in node.extra


def test_fetch_with_method_and_default_get(tmp_path):
    nodes, _ = parse(tmp_path, "src/f.ts", """
export async function create() {
  return fetch('/api/items', { method: 'POST', body: '{}' });
}
export async function list() { return fetch('/api/items'); }
export async function remove(id: string) {
  return fetch(`/api/items/${id}`, { method: "DELETE" });
}
""")
    assert sigs(clients(nodes)) == [
        ("DELETE", "/api/items/{param}"), ("GET", "/api/items"), ("POST", "/api/items"),
    ]


def test_fetch_with_unknown_method_is_unmatchable(tmp_path):
    nodes, _ = parse(tmp_path, "src/f.ts", """
export function call(m: string, opts: RequestInit) {
  fetch('/a', { method: m });
  fetch('/b', opts);
  fetch('/c', { ...opts });
}
""")
    found = clients(nodes)
    assert len(found) == 3
    assert all(n.extra["method_dynamic"] and n.extra["http_method"] == "UNKNOWN" for n in found)


def test_template_literal_and_concatenation_become_placeholders(tmp_path):
    nodes, _ = parse(tmp_path, "src/t.ts", """
import axios from 'axios';
const PREFIX = '/v3';
export function a(id: string, kid: string) {
  return axios.get(`/v3/tags/${id}/keys/${kid}?full=true`);
}
export function b(id: string) { return axios.get('/v3/tags/' + id + '/children'); }
export function c(id: string) { return axios.get(PREFIX + '/tags/' + id); }
export function d(id: string) { return axios.get(`${PREFIX}/tags/${id}`); }
""")
    assert sigs(clients(nodes)) == [
        ("GET", "/v3/tags/{param}"), ("GET", "/v3/tags/{param}"),
        ("GET", "/v3/tags/{param}/children"), ("GET", "/v3/tags/{param}/keys/{param}"),
    ]


def test_unknown_variable_url_is_not_guessed(tmp_path):
    nodes, _ = parse(tmp_path, "src/u.ts", """
import axios from 'axios';
export function a(url: string) { return axios.get(url); }
export function b(base: string) { return axios.get(`${base}/x`); }
export function c(base: string) { return axios.get(base + '/x'); }
export function d(fn: () => string) { return fetch(fn()); }
""")
    found = clients(nodes)
    assert len(found) == 4
    for node in found:
        assert node.extra["route"] == "<unresolved>" and node.extra["route_dynamic"] is True
        call = fl.ClientCall(
            repo="r", file=None, line=1, http_method="GET", route=node.extra["route"],
            origin="js", route_dynamic=True,
        )
        assert fl.unmatchable_reason(call) == "non_literal_url"


def test_absolute_urls_are_flagged(tmp_path):
    nodes, _ = parse(tmp_path, "src/abs.ts", """
import axios from 'axios';
const other = axios.create({ baseURL: 'https://host.example/api' });
export const a = () => axios.get('https://host.example/v1/x');
export const b = () => other.get('/y');
""")
    found = clients(nodes)
    assert len(found) == 2 and all(n.extra["absolute_url"] for n in found)


def test_jquery_calls(tmp_path):
    nodes, _ = parse(tmp_path, "src/j.js", """
export function a() { return $.ajax({ url: '/api/a', type: 'POST' }); }
export function b() { return $.ajax('/api/b'); }
export function c() { return $.getJSON('/api/c'); }
export function d() { return jQuery.post('/api/d', {}); }
export function e(u) { return $.ajax({ url: u }); }
""")
    found = clients(nodes)
    assert [(n.extra["http_method"], n.extra["route"]) for n in found[:4]] == [
        ("POST", "/api/a"), ("GET", "/api/b"), ("GET", "/api/c"), ("POST", "/api/d"),
    ]
    assert found[4].extra["route"] == "<unresolved>"
    assert {n.extra["client_kind"] for n in found} == {"jquery"}


def test_ember_ajax_forms(tmp_path):
    nodes, _ = parse(tmp_path, "app/adapters/thing.js", """
import ApplicationAdapter from './application';
export default ApplicationAdapter.extend({
  a() { return this.ajax('/v1/a', 'PUT', { data: {} }); },
  b() { return this.ajax('/v1/b'); },
  c() { return this.ajax.request('/v1/c', { method: 'POST' }); },
  d(url) { return this.ajax(url, 'GET'); }
});
""")
    found = [n for n in clients(nodes) if n.extra["client_kind"] == "ember-ajax"]
    assert [(n.extra["http_method"], n.extra["route"]) for n in found] == [
        ("PUT", "/v1/a"), ("GET", "/v1/b"), ("POST", "/v1/c"), ("GET", "<unresolved>"),
    ]


def test_imported_instance_is_inferred_and_needs_a_literal(tmp_path):
    nodes, edges = parse(tmp_path, "src/c.ts", """
import { apiClient } from '@/lib/client';
import { cache } from '@/lib/cache';
export const a = (id: string) => apiClient.get(`/v3/tags/${id}`);
export const b = (u: string) => apiClient.get(u);
export const c = () => cache.get('/key');
""")
    (node,) = clients(nodes)
    assert node.extra["route"] == "/v3/tags/{param}" and node.extra["receiver_inferred"]
    (edge,) = edges_of(edges, "REQUESTS")
    assert edge.extra["confidence_tier"] == "INFERRED"


def test_calls_outside_a_function_hang_off_the_file(tmp_path):
    nodes, edges = parse(tmp_path, "src/top.ts", "import axios from 'axios';\naxios.get('/boot');\n")
    (edge,) = edges_of(edges, "REQUESTS")
    assert edge.source == [n for n in nodes if n.kind == "File"][0].name


def test_test_files_are_skipped(tmp_path):
    nodes, _ = parse(tmp_path, "src/__tests__/a.test.ts", """
import axios from 'axios';
it('x', () => { axios.get('/x'); });
""")
    assert clients(nodes) == []


def test_unrelated_get_calls_are_not_requests(tmp_path):
    nodes, _ = parse(tmp_path, "src/m.ts", """
const m = new Map<string, string>();
export const a = () => m.get('/x');
export const b = (q: any) => q.get('/y');
""")
    assert clients(nodes) == [] and servers(nodes) == []



def test_a_query_string_glued_to_the_path_is_not_a_path_parameter(tmp_path):
    """``/feeds${query}`` where ``query`` is ``cond ? `?...` : ''`` is the feeds route."""
    nodes, _ = parse(tmp_path, "lib/feeds.ts", """
import apiClient from './apiClient';
export async function a(id?: string) {
  const query = id ? `?${new URLSearchParams({ id })}` : '';
  return apiClient.get(`/v1/admin/feeds${query}`);
}
export async function b(waveId?: string) {
  const params = waveId ? `?wave_id=${encodeURIComponent(waveId)}` : '';
  return apiClient.get(`/v1/cleanroom/waves/diff${params}`);
}
export async function c(qs: string) {
  const queryPart = qs ? `?${qs}` : '';
  const url = `/v1/admin/users/all${queryPart}`;
  return apiClient.get(url);
}
export async function d(qs: string) {
  return apiClient.get(`/v1/inline${qs ? `?${qs}` : ''}`);
}
export async function e() {
  const q = '?x=1';
  return apiClient.get('/v1/concat' + q);
}
""")
    assert {r for _, r in sigs(clients(nodes))} >= {
        "/v1/admin/feeds", "/v1/cleanroom/waves/diff", "/v1/inline", "/v1/concat",
    }
    assert not [n for n in clients(nodes) if "{param}" in n.extra["route"] and "/v1/admin/feeds" in n.extra["route"]]


def test_only_a_name_that_always_holds_a_query_string_is_a_suffix(tmp_path):
    nodes, _ = parse(tmp_path, "lib/guard.ts", """
import apiClient from './apiClient';
export async function one(id: string) {
  const suffix = id ? `/${id}` : '';
  return apiClient.get(`/v1/items${suffix}`);
}
export async function two(suffix: string) {
  return apiClient.get(`/v1/things${suffix}`);
}
const query = cond ? '?a=1' : '';
export async function three(query: string) {
  return apiClient.get(`/v1/shadow${query}`);
}
export async function four() {
  let q = '?a=1';
  q = other();
  return apiClient.get(`/v1/reassigned${q}`);
}
""")
    assert sigs(clients(nodes)) == [
        ("GET", "/v1/items{param}"), ("GET", "/v1/reassigned{param}"),
        ("GET", "/v1/shadow{param}"), ("GET", "/v1/things{param}"),
    ]


# ---------------------------------------------------------------------------
# Ember Data
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Server routes
# ---------------------------------------------------------------------------

_NEXT = "export async function {m}(req: Request) {{ return new Response('x'); }}\n"


def test_next_app_router_route_params_groups_and_methods(tmp_path):
    nodes, edges = parse(tmp_path, "src/app/(main)/api/tags/[id]/route.ts", _NEXT.format(m="GET") + _NEXT.format(m="DELETE") + "export async function helper() {}\n")
    assert sigs(servers(nodes)) == [("DELETE", "/api/tags/{id}"), ("GET", "/api/tags/{id}")]
    handles = edges_of(edges, "HANDLES")
    assert {e.source.split("::")[-1] for e in handles} == {"GET", "DELETE"}
    assert all(n.extra["framework"] == "nextjs-app" for n in servers(nodes))


def test_next_app_router_const_export_and_clause(tmp_path):
    nodes, _ = parse(tmp_path, "app/api/x/route.js", """
const handler = async () => new Response('x');
export const POST = handler;
async function put() {}
export { put as PUT };
""")
    assert sigs(servers(nodes)) == [("POST", "/api/x"), ("PUT", "/api/x")]


def test_next_catch_all_route_is_marked(tmp_path):
    nodes, _ = parse(tmp_path, "app/api/proxy/[...path]/route.ts", _NEXT.format(m="GET") + _NEXT.format(m="POST"))
    found = servers(nodes)
    assert {n.extra["route"] for n in found} == {"/api/proxy/{...path}"}
    assert all(n.extra["catch_all"] for n in found)
    nodes, _ = parse(tmp_path, "app/docs/[[...slug]]/route.ts", _NEXT.format(m="GET"))
    assert servers(nodes)[0].extra["optional_catch_all"] is True


def test_route_file_in_an_ember_pods_app_is_not_a_next_route(tmp_path):
    nodes, _ = parse(tmp_path, "app/posts/route.js", "import Route from '@ember/routing/route';\nexport default Route.extend({});\n")
    assert servers(nodes) == []


def test_next_pages_router(tmp_path):
    nodes, edges = parse(tmp_path, "pages/api/users/[id].ts", """
export default function handler(req, res) {
  if (req.method === 'PUT') { return res.json({}); }
  switch (req.method) { case 'DELETE': return res.end(); }
}
""")
    assert sigs(servers(nodes)) == [("DELETE", "/api/users/{id}"), ("PUT", "/api/users/{id}")]
    assert {e.source.split("::")[-1] for e in edges_of(edges, "HANDLES")} == {"handler"}
    nodes, _ = parse(tmp_path, "pages/api/index.ts", "export default function handler(req, res) { res.end(); }\n")
    assert sigs(servers(nodes)) == [("ANY", "/api")]


def test_express_routes(tmp_path):
    nodes, edges = parse(tmp_path, "server/routes.js", """
const express = require('express');
const app = express();
const router = express.Router();
function listUsers(req, res) { res.json([]); }
app.get('/users', listUsers);
router.post('/users/:userId/keys', (req, res) => res.sendStatus(201));
app.all('/ping', (req, res) => res.end());
app.get('/files/*', (req, res) => res.end());
router.route('/orders/:id').get(listUsers).delete(listUsers);
app.get('/not-a-route');
app.get(dynamicPath, listUsers);
""")
    found = {(n.extra["http_method"], n.extra["route"]): n for n in servers(nodes)}
    assert set(found) == {
        ("GET", "/users"), ("POST", "/users/{userId}/keys"), ("ANY", "/ping"),
        ("GET", "/files/{...rest}"), ("GET", "/orders/{id}"), ("DELETE", "/orders/{id}"),
    }
    assert found[("GET", "/files/{...rest}")].extra["catch_all"] is True
    handles = {e.target.split("::")[-1].split("@")[0]: e.source.split("::")[-1] for e in edges_of(edges, "HANDLES")}
    assert handles["listUsers"] == "listUsers"


def test_inline_handlers_each_have_their_own_identity(tmp_path):
    nodes, edges = parse(tmp_path, "src/b.ts", """
import express from 'express';
const app = express();
app.get('/a', () => first()); app.get('/b', () => second());
app.post('/c', function (req, res) {
  save(req.body);
  res.end();
});
function first() {}
function second() {}
function save(x) {}
""")
    handlers = {n.name for n in nodes if n.extra.get("inline_handler")}
    assert len(handlers) == 3  # two on one line are told apart by column
    by_route = {n.extra["route"]: n.extra["handler"] for n in servers(nodes)}
    assert set(by_route) == {"/a", "/b", "/c"} and set(by_route.values()) == handlers
    handles = {e.target.split("::")[-1].split(" ", 1)[-1]: e.source.split("::")[-1]
               for e in edges_of(edges, "HANDLES")}
    assert handles == {f"{'POST' if r == '/c' else 'GET'} {r}": h for r, h in by_route.items()}
    # the registering code still uses each handler, so none is reported as dead code
    referenced = {e.target.split("::")[-1] for e in edges_of(edges, "REFERENCES")}
    assert handlers <= referenced


def test_inline_handler_body_calls_belong_to_the_handler(tmp_path):
    nodes, edges = parse(tmp_path, "src/c.ts", """
import express from 'express';
const app = express();
app.post('/c', function (req, res) {
  save(req.body);
  res.end();
});
setup();
function save(x) {}
function setup() {}
""")
    (handler,) = [n.name for n in nodes if n.extra.get("inline_handler")]
    calls = {(e.source.split("::")[-1], e.target.split("::")[-1]) for e in edges_of(edges, "CALLS")}
    assert (handler, "save") in calls and (handler, "end") in calls
    # the registering call and the call after the handler stay with the module
    modules = {t for s, t in calls if s.endswith("src/c.ts")}
    assert {"post", "setup"} <= modules


def test_one_handler_for_several_methods_is_one_function(tmp_path):
    nodes, edges = parse(tmp_path, "src/f.ts", """
import Fastify from 'fastify';
const fastify = Fastify();
fastify.route({ method: ['GET', 'HEAD'], url: '/health', handler: async () => 'ok' });
""")
    assert len([n for n in nodes if n.extra.get("inline_handler")]) == 1
    assert len({e.source for e in edges_of(edges, "HANDLES")}) == 1
    assert len(edges_of(edges, "HANDLES")) == 2


def test_fastify_object_route_and_koa_router(tmp_path):
    nodes, _ = parse(tmp_path, "src/f.ts", """
import Fastify from 'fastify';
const fastify = Fastify();
fastify.route({ method: ['GET', 'HEAD'], url: '/health', handler: async () => 'ok' });
fastify.get('/v1/things/:id', async () => ({}));
""")
    assert sigs(servers(nodes)) == [("GET", "/health"), ("GET", "/v1/things/{id}"), ("HEAD", "/health")]


def test_server_calls_need_framework_evidence(tmp_path):
    nodes, _ = parse(tmp_path, "src/x.ts", "const app = new Thing();\napp.get('/x', () => 1);\n")
    assert servers(nodes) == []


def test_axios_calls_are_never_server_routes(tmp_path):
    nodes, _ = parse(tmp_path, "src/x.ts", """
import axios from 'axios';
import express from 'express';
export const a = (cb) => axios.get('/x', cb);
""")
    assert servers(nodes) == [] and len(clients(nodes)) == 1


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def make_repo(tmp_path, registry, name, files, *, java=()):
    """Write *files*, index them with the real parser into a registered repo."""
    root = (tmp_path / name).resolve()
    for rel, text in files.items():
        write(root, rel, text)
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True, exist_ok=True)
    parser = CodeParser(root)
    with GraphStore(directory / "graph.db") as store:
        for rel in files:
            nodes, edges = parser.parse_file(root / rel)
            store.store_file_nodes_edges(str(root / rel), nodes, edges)
        for rel, extra_nodes in java:
            store.store_file_nodes_edges(str(root / rel), extra_nodes, [])
    registry.register(str(root), alias=name)
    return root


def rows(result):
    return {(r["http_method"], r["route"], r["client_line"]): r for r in result["results"]}


CLIENT_FILES = {
    "src/api.ts": """
import axios from 'axios';
const api = axios.create({ baseURL: '/v3' });
export const getTag = (id: string) => api.get(`/tags/${id}`);
export const missing = () => api.get('/nothing');
export const dupe = () => axios.get('/dup');
export const dynamic = (u: string) => axios.get(u);
export const proxied = () => axios.get('/api/proxy/v9/thing');
export const post = () => fetch('/v3/tags', { method: 'POST' });
""",
}

TAG_API = {
    "src/main/Tags.ts": "",
    "app/v3/tags/[id]/route.ts": "export async function GET() { return new Response('x'); }\n",
    "app/v3/tags/route.ts": "export async function POST() { return new Response('x'); }\n",
    "app/dup/route.ts": "export async function GET() { return new Response('x'); }\n",
}


@pytest.fixture
def world(tmp_path, registry):
    make_repo(tmp_path, registry, "tag-api", {k: v for k, v in TAG_API.items() if v})
    make_repo(tmp_path, registry, "other-api", {
        "server.js": "const express = require('express');\nconst app = express();\napp.get('/api/proxy/other', (q, r) => r.end());\napp.get('/dup', (q, r) => r.end());\n",
    })
    make_repo(tmp_path, registry, "gateway", {
        "app/api/proxy/[...path]/route.ts": "export async function GET() { return new Response('x'); }\n",
    })
    make_repo(tmp_path, registry, "web", CLIENT_FILES)
    return tmp_path


def test_join_matched_ambiguous_no_route_match_and_unmatchable(world):
    result = cross_repo_requests_func(repos=["web"])
    by_line = {r["client_line"]: r for r in result["results"]}
    assert result["status"] == "ok"
    tag = by_line[4]
    assert tag["status"] == "matched" and tag["origin"] == "js" and tag["client_kind"] == "axios"
    assert tag["candidates"] == [{
        "repo": "tag-api", "file": "app/v3/tags/[id]/route.ts", "line": 1,
        "http_method": "GET", "route": "/v3/tags/{id}",
    }]
    assert "confidence" not in tag
    assert by_line[5]["status"] == "no_route_match"
    dupe = by_line[6]
    assert dupe["status"] == "ambiguous" and dupe["candidates_total"] == 2
    assert {c["repo"] for c in dupe["candidates"]} == {"tag-api", "other-api"}
    assert by_line[7]["status"] == "unmatchable" and by_line[7]["reason"] == "non_literal_url"
    assert by_line[9]["status"] == "matched" and by_line[9]["candidates"][0]["route"] == "/v3/tags"


def test_catch_all_route_matches_only_by_prefix_with_low_confidence(world):
    row = {r["client_line"]: r for r in cross_repo_requests_func(repos=["web"])["results"]}[8]
    assert row["status"] == "matched" and row["reason"] == "catch_all_route"
    assert row["confidence"] == "low" and "catch_all_route" in row["confidence_notes"]
    cand = row["candidates"][0]
    assert cand["repo"] == "gateway" and cand["catch_all"] is True


def test_catch_all_never_matches_other_methods_or_unrelated_routes(world):
    # The gateway proxies GET only, so a POST under the prefix does not match it.
    idx = fl.ServerIndex([fl.ServerEndpoint(
        repo="g", file="r.ts", line=1, http_method="GET", route="/api/proxy/{...path}",
        origin="g", catch_all=True,
    )])
    call = lambda m, r: fl.ClientCall(repo="c", file=None, line=1, http_method=m, route=r, origin="js")  # noqa: E731
    assert fl.link_js_call(call("GET", "/api/proxy/a/b"), idx).status == fl.MATCHED
    assert fl.link_js_call(call("POST", "/api/proxy/a"), idx).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(call("GET", "/api/proxy"), idx).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(call("GET", "/api/other/a"), idx).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(call("GET", "/api/proxyx/a"), idx).status == fl.NO_ROUTE_MATCH
    optional = fl.ServerIndex([fl.ServerEndpoint(
        repo="g", file="r.ts", line=1, http_method="GET", route="/docs/{...slug}",
        origin="g", catch_all=True, optional_catch_all=True,
    )])
    assert fl.link_js_call(call("GET", "/docs"), optional).status == fl.MATCHED
    root_wild = fl.ServerIndex([fl.ServerEndpoint(
        repo="g", file="r.ts", line=1, http_method="GET", route="/{...all}",
        origin="g", catch_all=True,
    )])
    assert fl.link_js_call(call("GET", "/anything"), root_wild).status == fl.NO_ROUTE_MATCH


def test_exact_route_beats_a_catch_all(tmp_path, registry):
    make_repo(tmp_path, registry, "gw", {"app/api/[...p]/route.ts": "export async function GET() {}\n"})
    make_repo(tmp_path, registry, "svc", {"app/api/health/route.ts": "export async function GET() {}\n"})
    make_repo(tmp_path, registry, "web", {"a.ts": "import axios from 'axios';\nexport const h = () => axios.get('/api/health');\n"})
    (row,) = cross_repo_requests_func(repos=["web"])["results"]
    assert row["status"] == "matched" and row["candidates"][0]["repo"] == "svc"
    assert "confidence" not in row


def test_a_route_the_caller_serves_itself_is_its_own_target(tmp_path, registry):
    make_repo(tmp_path, registry, "solo", {
        "app/x/route.ts": "export async function GET() {}\n",
        "a.ts": "import axios from 'axios';\nexport const h = () => axios.get('/x');\n",
    })
    (row,) = cross_repo_requests_func(repos=["solo"])["results"]
    assert row["status"] == "matched"
    assert row["candidates"][0]["repo"] == "solo"


def test_own_route_beats_the_same_route_in_a_sibling_repo(tmp_path, registry):
    """A relative URL goes to the origin that served the page, not to a sibling frontend."""
    route = "export async function GET() {}\n"
    make_repo(tmp_path, registry, "sibling", {"app/api/config/client/route.ts": route})
    make_repo(tmp_path, registry, "web", {
        "app/api/config/client/route.ts": route,
        "a.ts": "export const c = () => fetch('/api/config/client');\n",
    })
    (row,) = cross_repo_requests_func(repos=["web"])["results"]
    assert row["status"] == "matched"
    assert [c["repo"] for c in row["candidates"]] == ["web"]


def test_own_catch_all_beats_a_sibling_catch_all(tmp_path, registry):
    route = "export async function GET() {}\n"
    make_repo(tmp_path, registry, "sibling", {"app/api/proxy/[...path]/route.ts": route})
    make_repo(tmp_path, registry, "web", {
        "app/api/proxy/[...path]/route.ts": route,
        "a.ts": "export const c = () => fetch('/api/proxy/agent/dsp');\n",
    })
    (row,) = cross_repo_requests_func(repos=["web"])["results"]
    assert [c["repo"] for c in row["candidates"]] == ["web"]
    assert row["reason"] == "catch_all_route" and row["confidence"] == "low"


def test_a_call_the_caller_does_not_serve_still_reaches_other_repos(tmp_path, registry):
    make_repo(tmp_path, registry, "svc", {"app/api/health/route.ts": "export async function GET() {}\n"})
    make_repo(tmp_path, registry, "web", {
        "app/api/other/route.ts": "export async function GET() {}\n",
        "a.ts": "export const c = () => fetch('/api/health');\n",
    })
    (row,) = cross_repo_requests_func(repos=["web"])["results"]
    assert row["status"] == "matched" and row["candidates"][0]["repo"] == "svc"


def _vcall(route, method="GET"):
    return fl.ClientCall(
        repo="c", file=None, line=1, http_method=method, route=route, origin="js",
    )


def _vserver(route, method="GET", repo="s", line=1):
    return fl.ServerEndpoint(repo, "A.py", line, method, route, origin=f"/r/{repo}")


def test_variable_matches_literal_segment_at_low_confidence():
    index = fl.ServerIndex([_vserver("/agents/planner/threads")])
    result = fl.link_js_call(_vcall("/agents/{name}/threads"), index)
    assert (result.status, result.reason, result.confidence) == (
        fl.MATCHED, fl.VARIABLE_LITERAL_REASON, "low",
    )
    assert [c.route for c in result.candidates] == ["/agents/planner/threads"]


def test_variable_literal_needs_equal_method_and_segment_count():
    index = fl.ServerIndex([_vserver("/agents/planner/threads", "POST")])
    assert fl.link_js_call(_vcall("/agents/{n}/threads"), index).status == fl.NO_ROUTE_MATCH
    index = fl.ServerIndex([_vserver("/agents/planner/threads/extra")])
    assert fl.link_js_call(_vcall("/agents/{n}/threads"), index).status == fl.NO_ROUTE_MATCH


def test_variable_literal_is_case_sensitive():
    index = fl.ServerIndex([_vserver("/agents/planner/Threads")])
    assert fl.link_js_call(_vcall("/agents/{n}/threads"), index).status != fl.MATCHED


def test_variable_literal_rejects_mostly_variable_calls():
    index = fl.ServerIndex([_vserver("/agents/planner/threads")])
    assert fl.link_js_call(_vcall("/{a}/{b}/threads"), index).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(_vcall("/{a}/{b}/{c}"), index).status == fl.NO_ROUTE_MATCH


def test_variable_literal_never_beats_an_exact_route():
    index = fl.ServerIndex([
        _vserver("/agents/{id}/threads", line=1),
        _vserver("/agents/planner/threads", line=2),
    ])
    result = fl.link_js_call(_vcall("/agents/{n}/threads"), index)
    assert result.reason != fl.VARIABLE_LITERAL_REASON
    assert [c.line for c in result.candidates] == [1]


def test_variable_literal_too_many_routes_is_no_match():
    servers = [_vserver(f"/agents/a{i}/threads", line=i) for i in range(11)]
    assert fl.link_js_call(_vcall("/agents/{n}/threads"), fl.ServerIndex(servers)).status == (
        fl.NO_ROUTE_MATCH
    )
    ok = fl.link_js_call(_vcall("/agents/{n}/threads"), fl.ServerIndex(servers[:10]))
    assert (ok.status, ok.reason) == (fl.MATCHED, fl.VARIABLE_LITERAL_REASON)


def test_variable_literal_several_repos_is_ambiguous_and_own_pool_first():
    a = _vserver("/agents/x/threads", repo="s")
    b = _vserver("/agents/y/threads", repo="t")
    result = fl.link_js_call(_vcall("/agents/{n}/threads"), fl.ServerIndex([a, b]))
    assert (result.status, result.confidence) == (fl.AMBIGUOUS, "low")
    own = fl.ServerIndex([b])
    result = fl.link_js_call(_vcall("/agents/{n}/threads"), fl.ServerIndex([a, b]), own=own)
    assert result.status == fl.MATCHED and result.candidates[0].repo == "t"
def test_origin_filter_and_summary(world):
    only_js = cross_repo_requests_func(repos=["web"], origin="js")
    assert only_js["total"] == 6 and "JS/TS" in only_js["summary"]
    assert cross_repo_requests_func(repos=["web"], origin="feign")["total"] == 0
    assert cross_repo_requests_func(repos=["web"], service="x")["total"] == 0
    with pytest.raises(ValueError):
        cross_repo_requests_func(origin="rust")


def test_feign_rows_are_unchanged_by_js_rows(tmp_path, registry):
    def endpoint(root, name, role_extra, line, java_class):
        return NodeInfo(
            kind="Endpoint", name=name, file_path=str(root / f"{java_class}.java"),
            line_start=line, line_end=line, language="java", parent_name=java_class,
            extra=role_extra,
        )

    api = (tmp_path / "orders-service").resolve()
    make_repo(tmp_path, registry, "edge", {"app/orders/route.ts": "export async function GET() {}\n"})
    make_repo(tmp_path, registry, "orders-service", {"README.txt": "x"}, java=[
        ("Controller.java", [endpoint(api, "h@GetMapping[0:0] GET /orders", {"http_method": "GET", "route": "/orders"}, 10, "Controller")]),
    ])
    web = (tmp_path / "web").resolve()
    make_repo(tmp_path, registry, "web", {"a.ts": "import axios from 'axios';\nexport const o = () => axios.get('/orders');\n"}, java=[
        ("Client.java", [endpoint(web, "c@GetMapping[0:0] GET /orders", {
            "role": "client", "client": "feign", "http_method": "GET", "route": "/orders",
            "service": "orders-service", "config_keys": [], "route_dynamic": False, "url_literal": False,
        }, 3, "Client")]),
    ])
    result = cross_repo_requests_func(repos=["web"])
    feign = [r for r in result["results"] if r["origin"] == "feign"]
    js = [r for r in result["results"] if r["origin"] == "js"]
    assert len(feign) == 1 and len(js) == 1
    assert feign[0] == {
        "client_repo": "web", "client_file": "Client.java", "client_line": 3,
        "http_method": "GET", "route": "/orders", "service": "orders-service",
        "status": "matched", "origin": "feign", "target_resolution": "exact",
        "candidates": [{
            "repo": "orders-service", "file": "Controller.java", "line": 10,
            "http_method": "GET", "route": "/orders",
        }],
        "candidates_total": 1,
    }
    # The JS call sees both the Spring controller and the route file.
    assert js[0]["status"] == "ambiguous"
    only_feign = cross_repo_requests_func(repos=["web"], origin="feign")
    assert only_feign["summary"].startswith("1 Feign call(s)")
    assert only_feign["results"] == feign


def test_link_js_call_exact_method_semantics():
    idx = fl.ServerIndex([
        fl.ServerEndpoint("s", "a.ts", 1, "GET", "/items/{id}", origin="s"),
        fl.ServerEndpoint("s", "b.ts", 2, "ANY", "/ping", origin="s"),
    ])
    mk = lambda m, r: fl.ClientCall(repo="c", file=None, line=1, http_method=m, route=r, origin="js")  # noqa: E731
    assert fl.link_js_call(mk("GET", "/items/{param}?x=1"), idx).status == fl.MATCHED
    assert fl.link_js_call(mk("POST", "/items/{param}"), idx).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(mk("DELETE", "/ping"), idx).status == fl.MATCHED
    assert fl.link_js_call(mk("GET", "/items/7"), idx).status == fl.NO_ROUTE_MATCH  # no value guessing


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"route_dynamic": True}, "non_literal_url"),
        ({"absolute_url": True}, "absolute_url"),
        ({"method_dynamic": True}, "method_dynamic"),
        ({"route": "<unresolved>/x"}, "placeholder_in_route"),
        ({}, None),
    ],
)
def test_js_unmatchable_reasons(kwargs, reason):
    fields = {"repo": "c", "file": None, "line": 1, "http_method": "GET", "route": "/x", "origin": "js"}
    fields.update(kwargs)
    assert fl.unmatchable_reason(fl.ClientCall(**fields)) == reason


def test_mock_and_story_directories_are_skipped(tmp_path):
    for rel in ("server/mocks/a.js", ".storybook/mocks/b.ts", "stories/c.ts"):
        nodes, _ = parse(tmp_path, rel, """
const express = require('express');
const app = express();
app.get('/x', (q, r) => r.end());
export const go = () => fetch('/y');
""")
        assert clients(nodes) == [] and servers(nodes) == [], rel


def test_awaited_call_with_type_arguments_is_read(tmp_path):
    nodes, _ = parse(tmp_path, "src/g.ts", """
import axios from 'axios';
export async function load(id: string) {
  const { data } = await axios.get<Tag>(`/v3/tags/${id}`);
  await axios.post<Tag>('/v3/tags', {});
  return data;
}
""")
    assert sigs(clients(nodes)) == [("GET", "/v3/tags/{param}"), ("POST", "/v3/tags")]


def test_link_js_call_case_mismatch_only_replaces_no_route_match():
    idx = fl.ServerIndex([
        fl.ServerEndpoint("s", "a.java", 1, "GET", "/v1/attributegroups", origin="s"),
        fl.ServerEndpoint("s", "a.java", 2, "POST", "/v1/attributegroups", origin="s"),
        fl.ServerEndpoint("s", "b.java", 3, "ANY", "/v1/Ping", origin="s"),
    ])
    mk = lambda m, r: fl.ClientCall(repo="c", file=None, line=1, http_method=m, route=r, origin="js")  # noqa: E731
    lead = fl.link_js_call(mk("GET", "/v1/attributeGroups"), idx)
    assert (lead.status, lead.reason, lead.confidence) == (
        fl.CASE_MISMATCH, "route_differs_by_case", "low",
    )
    assert [c.line for c in lead.candidates] == [1]
    assert fl.link_js_call(mk("DELETE", "/v1/attributeGroups"), idx).status == fl.NO_ROUTE_MATCH
    assert fl.link_js_call(mk("GET", "/v1/ping"), idx).status == fl.CASE_MISMATCH
    assert fl.link_js_call(mk("GET", "/v1/attributegroups"), idx).status == fl.MATCHED
    assert fl.link_js_call(mk("GET", "/v1/other"), idx).status == fl.NO_ROUTE_MATCH


def test_catch_all_still_beats_a_case_lead():
    idx = fl.ServerIndex([
        fl.ServerEndpoint("gw", "r.ts", 1, "GET", "/api/{...p}", origin="gw", catch_all=True),
        fl.ServerEndpoint("s", "a.java", 2, "GET", "/api/Health", origin="s"),
    ])
    call = fl.ClientCall(repo="c", file=None, line=1, http_method="GET", route="/api/health", origin="js")
    result = fl.link_js_call(call, idx)
    assert result.status == fl.MATCHED and result.reason == "catch_all_route"


def test_case_mismatch_row_and_status_filter(tmp_path, registry):
    make_repo(tmp_path, registry, "svc", {"app/v1/attributegroups/route.ts": "export async function GET() {}\n"})
    make_repo(tmp_path, registry, "web", {
        "a.ts": "import axios from 'axios';\nexport const h = () => axios.get('/v1/attributeGroups');\n",
    })
    result = cross_repo_requests_func(repos=["web"])
    (row,) = result["results"]
    assert row["status"] == "case_mismatch"
    assert row["reason"] == "route_differs_by_case"
    assert row["confidence"] == "low"
    assert row["candidates"][0]["repo"] == "svc"
    assert "1 case_mismatch" in result["summary"]
    only = cross_repo_requests_func(repos=["web"], status="case_mismatch")
    assert only["results"] == [row]
    assert cross_repo_requests_func(repos=["web"], status="no_route_match")["results"] == []


def test_link_js_call_several_handlers_in_one_repo_is_a_match():
    two = [
        fl.ServerEndpoint("s", "A.java", 49, "GET", "/v3/admin/tags", origin="/r/s"),
        fl.ServerEndpoint("s", "A.java", 64, "GET", "/v3/admin/tags", origin="/r/s"),
    ]
    mk = lambda r: fl.ClientCall(repo="c", file=None, line=1, http_method="GET", route=r, origin="js")  # noqa: E731
    result = fl.link_js_call(mk("/v3/admin/tags"), fl.ServerIndex(two))
    assert (result.status, result.reason, result.confidence) == (fl.MATCHED, "several_handlers_in_repo", "high")
    assert [c.line for c in result.candidates] == [49, 64]
    other = two + [fl.ServerEndpoint("t", "B.java", 5, "GET", "/v3/admin/tags", origin="/r/t")]
    assert fl.link_js_call(mk("/v3/admin/tags"), fl.ServerIndex(other)).status == fl.AMBIGUOUS
