"""``method_not_served`` and ``prefix_mismatch``: leads for a JS call with no exact route."""

from __future__ import annotations

import pytest

from code_review_graph import feign_links as fl
from code_review_graph import js_route_leads as jl
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_js_api_calls import make_repo, registry  # noqa: F401


def _call(method: str, route: str) -> fl.ClientCall:
    return fl.ClientCall(repo="c", file=None, line=1, http_method=method, route=route, origin="js")


def _srv(line: int, method: str, route: str, repo: str = "s") -> fl.ServerEndpoint:
    return fl.ServerEndpoint(repo, f"{repo}.java", line, method, route, origin=repo)


def _refine(call, servers, own=None):
    index = fl.ServerIndex(servers)
    return jl.refine_no_route_match(call, fl.link_js_call(call, index), index, own)


def test_other_method_gives_method_not_served_with_served_methods():
    result = _refine(_call("DELETE", "/v1/tags/{id}"), [
        _srv(1, "GET", "/v1/tags/{id}"), _srv(2, "PUT", "/v1/tags/{id}"),
    ])
    assert result.status == "method_not_served"
    assert jl.served_methods(result.candidates) == ["GET", "PUT"]
    assert {c.repo for c in result.candidates} == {"s"}


def test_other_prefix_is_a_low_confidence_lead_not_a_match():
    result = _refine(
        _call("GET", "/v1/admin/projects/{id}"), [_srv(1, "GET", "/admin/projects/{id}")],
    )
    assert (result.status, result.confidence) == ("prefix_mismatch", "low")
    assert result.candidates[0].route == "/admin/projects/{id}"


def test_prefix_lead_needs_the_method_and_a_literal_tail():
    servers = [_srv(1, "POST", "/admin/projects/{id}"), _srv(2, "GET", "/a/{id}/{id}")]
    assert _refine(_call("GET", "/v1/admin/projects/{id}"), servers).status == "no_route_match"
    assert _refine(_call("GET", "/x/{a}/{b}"), servers).status == "no_route_match"


def test_nothing_fitting_stays_no_route_match_and_real_matches_win():
    servers = [_srv(1, "GET", "/v1/tags")]
    assert _refine(_call("GET", "/v1/other"), servers).status == "no_route_match"
    assert _refine(_call("GET", "/v1/tags"), servers).status == "matched"


def test_method_lead_beats_prefix_lead_and_own_repo_is_first():
    servers = [_srv(1, "GET", "/v1/tags/{id}"), _srv(2, "POST", "/other/tags/{id}")]
    assert _refine(_call("POST", "/v1/tags/{id}"), servers).status == "method_not_served"
    own = fl.ServerIndex([_srv(9, "GET", "/v1/tags/{id}", repo="own")])
    call = _call("POST", "/v1/tags/{id}")
    index = fl.ServerIndex([_srv(1, "PUT", "/v1/tags/{id}")])
    result = jl.refine_no_route_match(call, fl.LinkResult(fl.NO_ROUTE_MATCH), index, own)
    assert result.candidates[0].repo == "own"


def test_too_open_prefix_lead_is_dropped():
    servers = [_srv(n, "GET", f"/p{n}/admin/projects") for n in range(11)]
    assert _refine(_call("GET", "/v1/admin/projects"), servers).status == "no_route_match"


def test_all_statuses_place_the_leads_before_no_route_match():
    assert jl.ALL_STATUSES.index("method_not_served") < jl.ALL_STATUSES.index("no_route_match")
    assert set(fl.STATUSES) <= set(jl.ALL_STATUSES)


def test_through_the_tool_rows_filter_and_summary(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "svc", {
        "app/v1/tags/[id]/route.ts": "export async function GET() {}\n",
        "app/admin/projects/[id]/route.ts": "export async function GET() {}\n",
    })
    make_repo(tmp_path, registry, "web", {
        "a.ts": (
            "import axios from 'axios';\n"
            "export const a = (id: string) => axios.delete(`/v1/tags/${id}`);\n"
            "export const b = (id: string) => axios.get(`/v1/admin/projects/${id}`);\n"
            "export const c = () => axios.get('/nothing/here/at/all');\n"
        ),
    })
    reply = cross_repo_requests_func(repos=["web"])
    by_line = {r["client_line"]: r for r in reply["results"]}
    method = by_line[2]
    assert method["status"] == "method_not_served" and method["served_methods"] == ["GET"]
    assert method["candidates"][0]["repo"] == "svc"
    assert method["candidates"][0]["file"] == "app/v1/tags/[id]/route.ts"
    lead = by_line[3]
    assert lead["status"] == "prefix_mismatch" and lead["confidence"] == "low"
    assert lead["candidates"][0]["route"] == "/admin/projects/{id}"
    assert by_line[4]["status"] == "no_route_match"
    assert "1 method_not_served, 1 prefix_mismatch, 1 no_route_match" in reply["summary"]
    assert reply["counts"] == {
        "method_not_served": 1, "prefix_mismatch": 1, "no_route_match": 1,
    }
    only = cross_repo_requests_func(repos=["web"], status="method_not_served")
    assert [r["client_line"] for r in only["results"]] == [2]
    assert cross_repo_requests_func(repos=["web"], status="prefix_mismatch")["total"] == 1
    with pytest.raises(ValueError):
        cross_repo_requests_func(repos=["web"], status="bogus")
