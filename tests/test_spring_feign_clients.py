"""Spring Cloud OpenFeign clients are outbound calls, not served endpoints.

A ``@FeignClient`` interface describes requests the service makes. Its mapping
annotations emit ``Endpoint`` nodes with ``role = "client"`` and ``REQUESTS``
edges, never ``HANDLES`` edges, so ``handlers_of`` and ``endpoints_for`` stay
server-only.
"""

from pathlib import Path

from code_review_graph.constants import QUERY_PATTERNS, SPRING_FEIGN_EDGE_KIND
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

CLIENT_SOURCE = """
package com.example.orders;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;

@FeignClient(
    name = "billing-service",
    contextId = "invoices",
    url = "${billing.url}",
    path = "/api/"
)
public interface BillingClient {

    @GetMapping("/invoices/{id}/")
    Invoice getInvoice(@PathVariable("id") String id);

    @PostMapping("/invoices")
    Invoice createInvoice(@RequestBody InvoiceRequest request);
}
"""


def _parse(tmp_path: Path, source: str, name: str = "BillingClient.java"):
    path = tmp_path / name
    nodes, edges = CodeParser().parse_bytes(path, source.encode())
    return path, nodes, edges


def _client_endpoints(nodes):
    return [
        node for node in nodes
        if node.kind == "Endpoint" and node.extra.get("role") == "client"
    ]


def test_feign_client_methods_emit_client_endpoints_not_handlers(tmp_path: Path) -> None:
    _, nodes, edges = _parse(tmp_path, CLIENT_SOURCE)

    endpoints = _client_endpoints(nodes)
    assert [(n.extra["http_method"], n.extra["route"]) for n in endpoints] == [
        ("GET", "/api/invoices/{id}"),
        ("POST", "/api/invoices"),
    ]
    assert {n.extra["service"] for n in endpoints} == {"billing-service"}
    assert {n.extra["context_id"] for n in endpoints} == {"invoices"}
    assert {n.extra["client"] for n in endpoints} == {"feign"}
    assert not [n for n in nodes if n.kind == "Endpoint" and n not in endpoints]

    assert SPRING_FEIGN_EDGE_KIND == "REQUESTS"
    requests = [e for e in edges if e.kind == SPRING_FEIGN_EDGE_KIND]
    assert len(requests) == 2
    assert not [e for e in edges if e.kind == "HANDLES"]

    interface = next(n for n in nodes if n.kind == "Class")
    assert interface.extra["spring_feign_client"] is True
    assert interface.extra["feign_name"] == "billing-service"
    assert interface.extra["feign_context_id"] == "invoices"
    methods = [n for n in nodes if n.kind == "Function"]
    assert all(n.extra["spring_feign_request_count"] == 1 for n in methods)
    assert not any(n.extra.get("spring_endpoint") for n in methods)


def test_feign_path_and_legacy_interface_mapping_prefix_the_route(tmp_path: Path) -> None:
    source = """
    @FeignClient(name = "billing-service", path = "/api")
    @RequestMapping("/billing")
    interface LegacyBillingClient {
        @GetMapping("/invoices")
        Object list();
    }
    """
    _, nodes, _ = _parse(tmp_path, source, "LegacyBillingClient.java")

    assert [n.extra["route"] for n in _client_endpoints(nodes)] == [
        "/api/billing/invoices",
    ]


def test_feign_request_mapping_without_method_defaults_to_get(tmp_path: Path) -> None:
    source = """
    @FeignClient("billing-service")
    interface BillingClient {
        @RequestMapping("/plain")
        Object plain();

        @RequestMapping(value = "/explicit", method = RequestMethod.DELETE)
        void explicit();

        @GetMapping
        Object root();
    }
    """
    _, nodes, _ = _parse(tmp_path, source)

    assert [
        (n.extra["http_method"], n.extra["route"]) for n in _client_endpoints(nodes)
    ] == [("GET", "/plain"), ("DELETE", "/explicit"), ("GET", "/")]
    assert {n.extra["service"] for n in _client_endpoints(nodes)} == {"billing-service"}


def test_feign_multiple_paths_and_methods_expand(tmp_path: Path) -> None:
    source = """
    @FeignClient(name = "billing-service")
    interface BillingClient {
        @GetMapping({"/a", "/b"})
        Object both();

        @RequestMapping(
            path = "/x",
            method = {RequestMethod.GET, RequestMethod.POST}
        )
        Object multi();
    }
    """
    _, nodes, _ = _parse(tmp_path, source)
    endpoints = _client_endpoints(nodes)

    assert [(n.extra["client_method"], n.extra["http_method"], n.extra["route"])
            for n in endpoints] == [
        ("both", "GET", "/a"),
        ("both", "GET", "/b"),
        ("multi", "GET", "/x"),
        ("multi", "POST", "/x"),
    ]
    assert len({n.name for n in endpoints}) == 4


def test_feign_placeholders_are_recorded_as_config_keys_without_values(
    tmp_path: Path,
) -> None:
    source = """
    @FeignClient(
        name = "${billing.name}",
        url = "http://billing.invalid:8080/${billing.url.suffix:fallback}",
        path = "${billing.prefix:/api}"
    )
    interface BillingClient {
        @GetMapping("/${billing.segment}/items")
        Object items();
    }
    """
    _, nodes, edges = _parse(tmp_path, source)

    (endpoint,) = _client_endpoints(nodes)
    assert endpoint.extra["route"] == "/${billing.prefix:/api}/${billing.segment}/items"
    assert endpoint.extra["config_keys"] == [
        "billing.name", "billing.url.suffix", "billing.prefix", "billing.segment",
    ]
    assert "service" not in endpoint.extra
    assert endpoint.extra["service_unresolved"] is True
    assert endpoint.extra["url_literal"] is True
    assert endpoint.extra["route_dynamic"] is False

    config_edges = [e for e in edges if e.kind == "DEPENDS_ON_CONFIG"]
    assert [e.target for e in config_edges] == [
        "config:billing.name", "config:billing.url.suffix", "config:billing.prefix",
    ]
    assert {e.extra["resolution"] for e in config_edges} == {"feign_client"}
    assert {e.source.rsplit("::", 1)[-1] for e in config_edges} == {"BillingClient"}

    # No hostname or default value is retained anywhere.
    for item in [*nodes, *edges]:
        assert "billing.invalid" not in repr(item.extra)
        assert "8080" not in repr(item.extra)


def test_feign_constant_paths_are_marked_dynamic(tmp_path: Path) -> None:
    source = """
    @FeignClient(name = "billing-service")
    interface BillingClient {
        @GetMapping("/api" + "/folded")
        Object folded();

        @GetMapping(BASE + "/items")
        Object partial();

        @GetMapping(ApiPaths.ITEMS)
        Object constant();
    }
    """
    _, nodes, _ = _parse(tmp_path, source)
    by_method = {n.extra["client_method"]: n for n in _client_endpoints(nodes)}

    assert by_method["folded"].extra["route"] == "/api/folded"
    assert by_method["folded"].extra["route_dynamic"] is False
    assert by_method["partial"].extra["route"] == "/items"
    assert by_method["partial"].extra["route_dynamic"] is True
    assert by_method["constant"].extra["route"] == "/"
    assert by_method["constant"].extra["route_dynamic"] is True


def test_controller_in_the_same_file_still_emits_server_endpoints(tmp_path: Path) -> None:
    source = """
    @FeignClient(name = "billing-service", path = "/api")
    interface BillingClient {
        @GetMapping("/invoices/{id}")
        Object getInvoice();
    }

    @RestController
    @RequestMapping("/api")
    class InvoiceController {
        @GetMapping("/invoices/{id}")
        Object get() { return null; }
    }
    """
    _, nodes, edges = _parse(tmp_path, source, "Mixed.java")

    server = [n for n in nodes if n.kind == "Endpoint" and "role" not in n.extra]
    assert [(n.extra["handler"], n.extra["http_method"], n.extra["route"])
            for n in server] == [("get", "GET", "/api/invoices/{id}")]
    assert "client" not in {n.extra.get("client") for n in server}
    handles = [e for e in edges if e.kind == "HANDLES"]
    assert len(handles) == 1
    assert handles[0].source.endswith("::InvoiceController.get")
    requests = [e for e in edges if e.kind == SPRING_FEIGN_EDGE_KIND]
    assert len(requests) == 1
    assert requests[0].source.endswith("::BillingClient.getInvoice")


def test_request_queries_follow_requests_edges(tmp_path: Path) -> None:
    path, nodes, edges = _parse(tmp_path, CLIENT_SOURCE)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")

    method_qn = f"{path.as_posix()}::BillingClient.getInvoice"
    endpoint = next(
        n for n in _client_endpoints(nodes) if n.extra["client_method"] == "getInvoice"
    )
    endpoint_qn = f"{path.as_posix()}::BillingClient.{endpoint.name}"

    requested = query_graph("requests_of", method_qn, repo_root=str(tmp_path))
    assert requested["status"] == "ok"
    assert [r["qualified_name"] for r in requested["results"]] == [endpoint_qn]
    assert {r["kind"] for r in requested["results"]} == {"Endpoint"}
    assert {e["kind"] for e in requested["edges"]} == {SPRING_FEIGN_EDGE_KIND}

    requesters = query_graph("requesters_of", endpoint_qn, repo_root=str(tmp_path))
    assert requesters["status"] == "ok"
    assert [r["qualified_name"] for r in requesters["results"]] == [method_qn]

    served = query_graph("endpoints_for", method_qn, repo_root=str(tmp_path))
    assert served["results"] == []
    handled = query_graph("handlers_of", endpoint_qn, repo_root=str(tmp_path))
    assert handled["results"] == []

    assert {"requests_of", "requesters_of"} <= set(QUERY_PATTERNS)


def test_nested_feign_interface_resolves_through_the_full_scope_chain(
    tmp_path: Path,
) -> None:
    source = """
    class Outer {
        @FeignClient(name = "billing-service", path = "/api")
        @RequestMapping("/v1")
        interface InnerClient {
            @GetMapping("/items")
            Object items();
        }
    }
    """
    _, nodes, edges = _parse(tmp_path, source, "Outer.java")

    endpoints = _client_endpoints(nodes)
    assert [n.extra["route"] for n in endpoints] == ["/api/v1/items"]
    assert endpoints[0].parent_name == "Outer.InnerClient"
    assert not [e for e in edges if e.kind == "HANDLES"]


def test_same_named_feign_interfaces_in_one_file_keep_distinct_endpoints(
    tmp_path: Path,
) -> None:
    source = """
    @FeignClient(name = "billing-service")
    interface BillingClient {
        @GetMapping("/same")
        Object first();

        @GetMapping("/same")
        Object second();
    }
    """
    _, nodes, edges = _parse(tmp_path, source)

    endpoints = _client_endpoints(nodes)
    assert len(endpoints) == 2
    assert len({n.name for n in endpoints}) == 2
    assert len({e.target for e in edges if e.kind == SPRING_FEIGN_EDGE_KIND}) == 2
