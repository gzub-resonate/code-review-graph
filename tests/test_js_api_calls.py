# ruff: noqa: E501
"""HTTP API calls and served routes in JavaScript and TypeScript.

Cases build files in a temp directory and read the nodes and edges back.
Repository, service and route names are invented.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.parser import CodeParser

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
