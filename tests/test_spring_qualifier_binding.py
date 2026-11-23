"""Spring DI binding when an interface has several implementations.

An injected interface with more than one production implementation is bound to
one of them from the injection point's ``@Qualifier``, then a single
``@Primary`` class, then the injection point's name against bean names.  Any
other shape keeps the call on the interface method, as before.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Optional

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.spring_resolver import resolve_spring_di_calls
from code_review_graph.tools.query import query_graph

MAIN = "src/main/java/app/orders"

_INTERFACE = (
    "package app.orders;\n"
    "public interface OrderMgr { String find(int id); }\n"
)

_IMPORTS = (
    "import org.springframework.beans.factory.annotation.Autowired;\n"
    "import org.springframework.beans.factory.annotation.Qualifier;\n"
    "import org.springframework.context.annotation.Primary;\n"
    "import org.springframework.stereotype.Component;\n"
    "import org.springframework.stereotype.Service;\n"
)


def _impl(name: str, annotations: str) -> str:
    return (
        "package app.orders;\n"
        f"{_IMPORTS}"
        f"{annotations}\n"
        f"public class {name} implements OrderMgr {{\n"
        '    public String find(int id) { return "x"; }\n'
        "}\n"
    )


def _caller(body: str, *, annotation: str = "@Service") -> str:
    return (
        "package app.orders;\n"
        f"{_IMPORTS}"
        f"{annotation}\n"
        "public class OrderService {\n"
        f"{body}\n"
        '    public String place() { return orderMgr.find(1); }\n'
        "}\n"
    )


def _ctor(point: str, name: str = "orderMgr") -> str:
    return (
        "    private final OrderMgr orderMgr;\n"
        "    @Autowired\n"
        f"    public OrderService({point} OrderMgr {name}) {{ this.orderMgr = {name}; }}\n"
    )


def _files(
    caller: str,
    caching: str = '@Component("CachingOrderMgr")',
    real: str = '@Component("OrderMgrImpl")',
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    files = {
        f"{MAIN}/OrderMgr.java": _INTERFACE,
        f"{MAIN}/CachingOrderMgr.java": _impl("CachingOrderMgr", caching),
        f"{MAIN}/OrderMgrImpl.java": _impl("OrderMgrImpl", real),
        f"{MAIN}/OrderService.java": caller,
    }
    files.update(extra or {})
    return files


def _build(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    store = GraphStore(str(tmp_path / "graph.db"))
    full_build(tmp_path, store)
    run_post_processing(store)
    return store


def _place_edge(store: GraphStore) -> tuple[str, dict]:
    rows = store._conn.execute(
        "SELECT target_qualified, extra FROM edges "
        "WHERE kind = 'CALLS' AND source_qualified LIKE '%::OrderService.place'"
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["target_qualified"], json.loads(rows[0]["extra"])


def _target_of(tmp_path: Path, files: dict[str, str]) -> str:
    with _build(tmp_path, files) as store:
        target, _ = _place_edge(store)
        return target.split("::", 1)[-1]


INTERFACE_TARGET = "OrderMgr.find"


def test_constructor_qualifier_matches_explicit_component_name(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("realMgr")')),
        caching='@Component("cachingMgr")',
        real='@Component("realMgr")',
    )
    with _build(tmp_path, files) as store:
        target, extra = _place_edge(store)
    assert target.endswith("::OrderMgrImpl.find")
    assert extra["spring_resolved"] is True
    assert extra["resolution"] == "spring_qualifier"
    assert extra["qualifier"] == "realMgr"
    assert extra["injected_type"] == "OrderMgr"


def test_qualifier_value_attribute_form(tmp_path: Path) -> None:
    files = _files(_caller(_ctor('@Qualifier(value = "OrderMgrImpl")')))
    assert _target_of(tmp_path, files) == "OrderMgrImpl.find"


def test_qualifier_declared_on_the_implementing_class(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("fast")')),
        caching='@Component @Qualifier("fast")',
        real="@Component",
    )
    assert _target_of(tmp_path, files) == "CachingOrderMgr.find"


def test_default_bean_name_lowercases_the_first_letter(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("orderMgrImpl")')),
        caching="@Service",
        real="@Service",
    )
    assert _target_of(tmp_path, files) == "OrderMgrImpl.find"


def test_matching_is_exact_and_case_sensitive(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("OrderMgrImpl")')),
        caching="@Service",
        real="@Service",
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_explicit_name_replaces_the_default_name(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("orderMgrImpl")')),
        caching="@Service",
        real='@Service("other")',
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_field_autowired_with_qualifier(tmp_path: Path) -> None:
    body = (
        '    @Autowired @Qualifier("CachingOrderMgr")\n'
        "    private OrderMgr orderMgr;\n"
    )
    with _build(tmp_path, _files(_caller(body))) as store:
        target, extra = _place_edge(store)
    assert target.endswith("::CachingOrderMgr.find")
    assert extra["resolution"] == "spring_qualifier"


def test_qualifier_matching_no_candidate_keeps_the_interface(tmp_path: Path) -> None:
    files = _files(_caller(_ctor('@Qualifier("missing")')))
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_qualifier_matching_two_candidates_keeps_the_interface(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("shared")')),
        caching='@Component @Qualifier("shared")',
        real='@Component @Qualifier("shared")',
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_qualifier_that_is_a_constant_is_not_guessed(tmp_path: Path) -> None:
    files = _files(_caller(_ctor("@Qualifier(Names.REAL)", "orderMgrImpl")))
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_qualifier_wins_over_primary_and_never_falls_back(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("OrderMgrImpl")')),
        caching='@Component("CachingOrderMgr") @Primary',
    )
    assert _target_of(tmp_path, files) == "OrderMgrImpl.find"
    files = _files(
        _caller(_ctor('@Qualifier("missing")')),
        caching='@Component("CachingOrderMgr") @Primary',
    )
    assert _target_of(tmp_path / "second", files) == INTERFACE_TARGET


def test_primary_picks_the_single_primary_class(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor("")),
        caching='@Component("CachingOrderMgr") @Primary',
    )
    with _build(tmp_path, files) as store:
        target, extra = _place_edge(store)
    assert target.endswith("::CachingOrderMgr.find")
    assert extra["resolution"] == "spring_primary"
    assert "qualifier" not in extra and "bean_name" not in extra


def test_two_primaries_fall_through_to_the_name_rule(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor("")),
        caching='@Component("CachingOrderMgr") @Primary',
        real='@Component("OrderMgrImpl") @Primary',
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET
    files = _files(
        _caller(_ctor("")),
        caching='@Component("CachingOrderMgr") @Primary',
        real='@Component("orderMgr") @Primary',
    )
    assert _target_of(tmp_path / "named", files) == "OrderMgrImpl.find"


def test_conditional_candidates_disable_primary_and_name_rules(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor("")),
        caching='@Component("CachingOrderMgr") @Primary @ConditionalOnProperty("x.enabled")',
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET
    files = _files(
        _caller(_ctor('@Qualifier("CachingOrderMgr")')),
        caching='@Component("CachingOrderMgr") @Profile("cache")',
    )
    assert _target_of(tmp_path / "qualified", files) == "CachingOrderMgr.find"


def test_injection_point_name_matches_one_bean_name(tmp_path: Path) -> None:
    body = (
        "    @Autowired private OrderMgr orderMgr;\n"
    )
    files = _files(
        _caller(body),
        caching='@Component("orderMgr")',
        real='@Component("OrderMgrImpl")',
    )
    with _build(tmp_path, files) as store:
        target, extra = _place_edge(store)
    assert target.endswith("::CachingOrderMgr.find")
    assert extra["resolution"] == "spring_bean_name"
    assert extra["bean_name"] == "orderMgr"


def test_injection_point_name_matching_default_bean_name(tmp_path: Path) -> None:
    body = "    @Autowired private OrderMgr orderMgrImpl;\n"
    caller = _caller(body).replace("orderMgr.find", "orderMgrImpl.find")
    files = _files(caller, caching="@Service", real="@Service")
    assert _target_of(tmp_path, files) == "OrderMgrImpl.find"


def test_injection_point_name_matching_nothing_keeps_the_interface(tmp_path: Path) -> None:
    files = _files(_caller(_ctor("")))
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_bean_name_that_is_a_constant_is_not_guessed(tmp_path: Path) -> None:
    body = "    @Autowired private OrderMgr orderMgr;\n"
    files = _files(
        _caller(body),
        caching="@Component(Names.CACHING)",
        real='@Component("OrderMgrImpl")',
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_method_missing_on_the_chosen_class_keeps_the_interface(tmp_path: Path) -> None:
    files = _files(
        _caller(_ctor('@Qualifier("OrderMgrImpl")')),
        real='@Component("OrderMgrImpl")',
    )
    files[f"{MAIN}/OrderMgrImpl.java"] = (
        "package app.orders;\n"
        f"{_IMPORTS}"
        '@Component("OrderMgrImpl")\n'
        "public abstract class OrderMgrImpl implements OrderMgr { }\n"
    )
    assert _target_of(tmp_path, files) == INTERFACE_TARGET


def test_single_implementation_rule_is_unchanged(tmp_path: Path) -> None:
    files = {
        f"{MAIN}/OrderMgr.java": _INTERFACE,
        f"{MAIN}/OrderMgrImpl.java": _impl("OrderMgrImpl", "@Service"),
        f"{MAIN}/OrderService.java": _caller(_ctor('@Qualifier("whatever")')),
    }
    with _build(tmp_path, files) as store:
        target, extra = _place_edge(store)
    assert target.endswith("::OrderMgrImpl.find")
    assert extra == {
        "receiver": "orderMgr",
        "receiver_resolution": "typed_receiver",
        "receiver_type": "OrderMgr",
        "spring_resolved": True,
        "injected_type": "OrderMgr",
        "java_type": "app.orders.OrderMgr",
    }


def test_test_classes_are_not_candidates(tmp_path: Path) -> None:
    mock = (
        "package app.orders;\n"
        f"{_IMPORTS}"
        '@Component("mockMgr")\n'
        "public class MockOrderMgr implements OrderMgr {\n"
        '    public String find(int id) { return "t"; }\n'
        "}\n"
    )
    extra = {"src/test/java/app/orders/MockOrderMgr.java": mock}
    files = _files(_caller(_ctor('@Qualifier("mockMgr")')), extra=extra)
    assert _target_of(tmp_path, files) == INTERFACE_TARGET
    files = _files(_caller(_ctor('@Qualifier("OrderMgrImpl")')), extra=extra)
    assert _target_of(tmp_path / "second", files) == "OrderMgrImpl.find"


def test_decorator_chain_resolves_end_to_end(tmp_path: Path) -> None:
    controller = (
        "package app.orders;\n"
        f"{_IMPORTS}"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "@RestController\n"
        "public class OrderController {\n"
        "    private final OrderMgr orderMgr;\n"
        "    @Autowired\n"
        '    public OrderController(@Qualifier("CachingOrderMgr") OrderMgr orderMgr) {\n'
        "        this.orderMgr = orderMgr;\n"
        "    }\n"
        "    public String get() { return orderMgr.find(1); }\n"
        "}\n"
    )
    caching = (
        "package app.orders;\n"
        f"{_IMPORTS}"
        '@Component("CachingOrderMgr")\n'
        "public class CachingOrderMgr implements OrderMgr {\n"
        "    private final OrderMgr delegate;\n"
        "    @Autowired\n"
        '    public CachingOrderMgr(@Qualifier("OrderMgrImpl") OrderMgr delegate) {\n'
        "        this.delegate = delegate;\n"
        "    }\n"
        "    public String find(int id) { return delegate.find(id); }\n"
        "}\n"
    )
    files = _files(_caller(_ctor('@Qualifier("OrderMgrImpl")')))
    files[f"{MAIN}/OrderController.java"] = controller
    files[f"{MAIN}/CachingOrderMgr.java"] = caching
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".code-review-graph").mkdir()
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    with GraphStore(str(root / ".code-review-graph" / "graph.db")) as store:
        full_build(root, store)
        run_post_processing(store)
        impl_find = next(
            row["qualified_name"]
            for row in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE name = 'find' "
                "AND parent_name = 'OrderMgrImpl'"
            )
        )

    callers = query_graph("callers_of", impl_find, str(root))
    caller_names = {node["qualified_name"].split("::")[-1] for node in callers["results"]}
    assert caller_names == {"CachingOrderMgr.find", "OrderService.place"}

    route = query_graph("path_to", "OrderController.get", str(root), to=impl_find)
    assert route["result_count"] == 2
    hops = [(hop["from"].split("::")[-1], hop["to"].split("::")[-1]) for hop in route["results"]]
    assert hops == [
        ("OrderController.get", "CachingOrderMgr.find"),
        ("CachingOrderMgr.find", "OrderMgrImpl.find"),
    ]
    assert {hop["kind"] for hop in route["results"]} == {"CALLS"}


def _graph_edges(tmp_path: Path, files: list[tuple[str, str]]) -> list[tuple]:
    parser = CodeParser()
    with GraphStore(str(tmp_path / "graph.db")) as store:
        for relative, source in files:
            nodes, edges = parser.parse_bytes(Path(relative), source.encode("utf-8"))
            for node in nodes:
                store.upsert_node(node)
            for edge in edges:
                store.upsert_edge(edge)
        store.commit()
        resolve_spring_di_calls(store)
        return sorted(
            (row["kind"], row["source_qualified"], row["target_qualified"], row["extra"])
            for row in store._conn.execute(
                "SELECT kind, source_qualified, target_qualified, extra FROM edges"
            )
        )


@pytest.mark.determinism
def test_result_does_not_depend_on_file_order(tmp_path: Path) -> None:
    files = _files(_caller(_ctor('@Qualifier("OrderMgrImpl")')))
    ordered = sorted(files.items())
    baseline = _graph_edges(tmp_path / "base", ordered)
    assert any(
        row[0] == "CALLS" and row[2].endswith("::OrderMgrImpl.find") for row in baseline
    )
    rng = random.Random(7)
    for attempt in range(4):
        shuffled = list(ordered)
        rng.shuffle(shuffled)
        assert _graph_edges(tmp_path / f"run{attempt}", shuffled) == baseline
    assert _graph_edges(tmp_path / "rev", list(reversed(ordered))) == baseline


def test_resolver_is_idempotent(tmp_path: Path) -> None:
    files = _files(_caller(_ctor('@Qualifier("OrderMgrImpl")')))
    with _build(tmp_path, files) as store:
        before = _place_edge(store)
        resolve_spring_di_calls(store)
        assert _place_edge(store) == before
