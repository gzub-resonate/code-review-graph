"""Reasons for rows that had none: ``unresolved_reason`` on awssdk rows, ``reason`` on JS
``no_route_match`` and Feign ``no_route_match`` rows, mock-folder ``test_double`` and the
``counts_by_reason`` of ``origin=js`` / ``origin=feign``.

Shapes imitate real rows: ``s3/?`` calls with a bucket taken from a parameter (services-geode
S3Helper), mock-server S3 literals under ``lib/server/msw`` (constellation-ui), an Arbiter
axios call to a route nobody serves, and a Feign client named after one service whose route the
calling repo itself serves (services-api CollectionRestSvcClient).
"""

from __future__ import annotations

from types import SimpleNamespace

from code_review_graph import feign_links as fl
from code_review_graph import not_found_reasons as nfr
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_js_api_calls import make_repo, registry  # noqa: F401


def _row(**kw):
    base = {"status": "unresolved", "target_kind": "s3", "target_name": "?"}
    base.update(kw)
    return base


def test_unresolved_rows_get_a_reason_from_their_shape():
    rows = [
        _row(),
        _row(const_ref="DELIVERY_BUCKET"),
        _row(placeholder="app.bucket"),
        _row(placeholder="app.bucket", unresolved_reason="not_defined"),
        _row(client="jdbc", evidence="connection_url"),
        _row(env_placeholder="BUCKET"),
        _row(status="matched"),
    ]
    nfr.annotate_unresolved(rows)
    got = [r.get("unresolved_reason") for r in rows]
    assert got == [
        "name_not_literal", "constant_not_resolved", "config_key_not_resolved",
        "not_defined", "host_not_literal", "env_placeholder", None,
    ]


def test_counts_and_summary_split_not_found_and_unresolved():
    rows = [
        {"status": "unresolved", "unresolved_reason": "name_not_literal"},
        {"status": "unresolved", "unresolved_reason": "name_not_literal"},
        {"status": "not_found", "not_found_reason": "test_double"},
    ]
    by_reason = nfr.counts_by_reason(rows)
    assert by_reason == {"name_not_literal": 2, "test_double": 1}
    clause = nfr.summary_clause({"unresolved": 2, "not_found": 1}, by_reason)
    assert clause == "; 1 not found (1 test_double); 2 unresolved (2 name_not_literal)"


PY_APP = """\
import boto3

s3 = boto3.client("s3")


def exists(bucket, key):
    return s3.head_object(Bucket=bucket, Key=key)
"""


def test_awssdk_unresolved_row_says_why_and_filters(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "app", {"app.py": PY_APP})
    reply = cross_repo_requests_func(origin="awssdk", max_results=100)
    rows = [r for r in reply["results"] if r["status"] == "unresolved"]
    assert rows
    assert all(r["unresolved_reason"] for r in rows)
    assert reply["counts_by_reason"]["name_not_literal"] == len(rows)
    only = cross_repo_requests_func(origin="awssdk", status="name_not_literal")
    assert only["total"] == len(rows)


def test_mock_folder_literal_is_a_test_double(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "ui", {
        "lib/server/msw/handlers.ts": "export const u = 's3://initiatives-bucket/out/x.csv';\n",
    })
    make_repo(tmp_path, registry, "infra", {
        "b.tf": 'resource "aws_s3_bucket" "d" {\n  bucket = "${var.env}-data"\n}\n',
    })
    reply = cross_repo_requests_func(origin="awssdk", repos=["ui"], max_results=100)
    got = {r["target_name"]: r.get("not_found_reason") for r in reply["results"]}
    assert got["initiatives-bucket"] == "test_double"


def test_declared_name_dynamic_lists_the_repos_that_declare_it(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "app", {
        "app.py": (
            'import boto3\ns3 = boto3.client("s3")\n'
            's3.get_object(Bucket="plain-bucket", Key="k")\n'
        ),
    })
    make_repo(tmp_path, registry, "infra", {
        "b.tf": 'resource "aws_s3_bucket" "d" {\n  bucket = "${var.env}-data"\n}\n',
    })
    reply = cross_repo_requests_func(origin="awssdk", repos=["app"], max_results=100)
    row = next(r for r in reply["results"] if r["target_name"] == "plain-bucket")
    assert row["not_found_reason"] == "declared_name_dynamic"
    assert row["dynamic_declared_in"] == ["infra"]


def test_js_no_route_match_reasons(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "svc", {
        "app/v1/tags/[id]/route.ts": "export async function GET() {}\n",
    })
    make_repo(tmp_path, registry, "web", {
        "a.ts": (
            "import axios from 'axios';\n"
            "export const a = () => axios.get('/nothing/here/at/all');\n"
            "export const b = () => axios.get('/v1/other/thing');\n"
            "export const c = (x: string) => axios.get(`/v1/${x}/y/z`);\n"
            "export const d = (u: string) => axios.get(u);\n"
        ),
    })
    reply = cross_repo_requests_func(repos=["web"], origin="js")
    by_line = {r["client_line"]: r for r in reply["results"]}
    assert by_line[2]["status"] == "no_route_match"
    assert by_line[2]["reason"] == "no_endpoint_under_base_path"
    assert by_line[3]["reason"] == "route_missing_under_served_base"
    assert by_line[4]["reason"] == "variable_segments_unmatched"
    assert by_line[5]["status"] == "unmatchable"
    assert by_line[5]["reason"] == "non_literal_url"
    assert reply["counts_by_reason"] == {
        "no_endpoint_under_base_path": 1, "route_missing_under_served_base": 1,
        "variable_segments_unmatched": 1, "non_literal_url": 1,
    }
    assert reply["counts"] == {"no_route_match": 3, "unmatchable": 1}
    only = cross_repo_requests_func(repos=["web"], origin="js", status="non_literal_url")
    assert [r["client_line"] for r in only["results"]] == [5]


def test_js_no_servers_indexed_reason():
    call = fl.ClientCall(repo="c", file=None, line=1, http_method="GET", route="/a/b", origin="js")
    assert nfr.js_no_route_reason(call, [fl.ServerIndex([])]) == "no_servers_indexed"


def _srv(method, route):
    return fl.ServerEndpoint("caller", "Impl.java", 1, method, route, origin="caller")


def test_feign_no_route_match_served_by_caller_repo():
    call = fl.ClientCall(
        repo="caller", file="C.java", line=1, http_method="GET", route="/v2/collections/{id}",
        service="services-api-analytics",
    )
    result = fl.LinkResult(fl.NO_ROUTE_MATCH)
    own = [_srv("GET", "/v2/collections/{collectionId}")]
    reason = nfr.cross_repo_row_reason(call, result, None, None, own)
    assert reason == "served_by_caller_repo"
    # Another method on the same route does not count: the join's own rule is applied.
    other = [_srv("POST", "/v2/collections/{id}")]
    assert nfr.cross_repo_row_reason(call, result, None, None, other) is None
    assert nfr.cross_repo_row_reason(call, result, None, None, []) is None
    matched = SimpleNamespace(status=fl.MATCHED, reason=None)
    assert nfr.cross_repo_row_reason(call, matched, None, None, own) is None
    unmatchable = SimpleNamespace(status=fl.UNMATCHABLE, reason="url_literal")
    assert nfr.cross_repo_row_reason(call, unmatchable, None, None, own) is None


FEIGN = """\
package a;
import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;
@FeignClient(name = "analytics")
public interface CollectionClient {
    @GetMapping("/v2/collections/{id}")
    String get(@PathVariable("id") String id);
    @GetMapping("/v2/nothing")
    String nothing();
}
"""
CONTROLLER = """\
package a;
import org.springframework.web.bind.annotation.*;
@RestController
public class CollectionImpl {
    @GetMapping("/v2/collections/{collectionId}")
    public String get(@PathVariable String collectionId) { return ""; }
}
"""


def test_feign_row_through_the_tool_says_served_by_caller_repo(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "analytics", {
        "src/main/java/a/Other.java": CONTROLLER.replace("CollectionImpl", "Other").replace(
            "/v2/collections/{collectionId}", "/v2/other"),
    })
    make_repo(tmp_path, registry, "caller", {
        "src/main/java/a/CollectionClient.java": FEIGN,
        "src/main/java/a/CollectionImpl.java": CONTROLLER,
    })
    reply = cross_repo_requests_func(repos=["caller"], origin="feign")
    by_route = {r["route"]: r for r in reply["results"]}
    assert by_route["/v2/collections/{id}"]["status"] == "no_route_match"
    assert by_route["/v2/collections/{id}"]["reason"] == "served_by_caller_repo"
    assert by_route["/v2/nothing"]["status"] == "no_route_match"
    assert "reason" not in by_route["/v2/nothing"]
    assert reply["counts_by_reason"] == {"served_by_caller_repo": 1}
