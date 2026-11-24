"""``exclude_data_types`` on get_hub_nodes: enums, records, Lombok and accessor-only Java
classes, dataclasses and data-only TS types are left out of the ranking; classes with
behavior are not."""

from __future__ import annotations

import pytest

from code_review_graph.analysis import data_type_label, find_hub_nodes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo, normalize_file_path
from code_review_graph.tools import get_hub_nodes_func


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        s.root = tmp_path
        yield s


def _file(repo, rel):
    return normalize_file_path(repo.root / rel)


def _edge(repo, kind, source, target):
    repo.upsert_edge(EdgeInfo(kind, source, target, source.split("::")[0], 3))


def _type(repo, name, language="java", callers=6, methods=(), extra=None, bases=()):
    """A Class node, its methods ``(name, params)`` and *callers* distinct callers."""
    file = _file(repo, f"src/main/{name}.x")
    repo.upsert_node(NodeInfo("Class", name, file, 1, 50, language, extra=extra or {}))
    for m, params in methods:
        repo.upsert_node(
            NodeInfo("Function", m, file, 2, 4, language, parent_name=name, params=params),
        )
    qn = f"{file}::{name}"
    for i in range(callers):
        caller = _file(repo, f"src/main/{name}Caller{i}.x")
        repo.upsert_node(NodeInfo("Function", f"use{name}{i}", caller, 1, 3, language))
        _edge(repo, "CALLS", f"{caller}::use{name}{i}", qn)
    for base in bases:
        _edge(repo, "INHERITS", qn, base)
    return qn


def _java(repo, name, annotations=(), methods=(), **kw):
    extra = {"java_kind": "concrete", "decorators": list(annotations)}
    return _type(repo, name, "java", methods=methods, extra=extra, **kw)


ACCESSORS = [("getA", "()"), ("setA", "(String a)"), ("isB", "()"), ("equals", "(Object o)")]


def _build(repo):
    q = {
        "enum": _type(repo, "Color", extra={"type_form": "enum"}, callers=9),
        "lombok": _java(repo, "UserDto", ["Data", "Builder"], callers=8),
        "record": _type(repo, "Point", "csharp", extra={"type_form": "record"}, callers=7),
        "dataclass": _type(
            repo, "Row", "python", extra={"decorators": ["dataclass(frozen=True)"]}, callers=6,
        ),
        "pyenum": _type(repo, "Mode", "python", bases=["Enum"], callers=6),
        "pydantic": _type(repo, "Payload", "python", bases=["pydantic.BaseModel"], callers=5),
        "pojo": _java(repo, "Plain", methods=ACCESSORS, callers=5),
        "ts_iface": _type(
            repo, "Shape", "typescript",
            extra={"typescript_kind": "interface_declaration", "ts_data_shape": True}, callers=5,
        ),
        # behavior: kept
        "real": _java(repo, "PriceCalc", methods=[("getA", "()"), ("compute", "(int x)")],
                      callers=10),
        "lombok_biz": _java(
            repo, "Order", ["Data"], methods=[("getA", "()"), ("applyDiscount", "(int pct)")],
            callers=9,
        ),
        "service": _type(
            repo, "Billing", "java", callers=8,
            extra={"java_kind": "concrete", "spring_stereotype": "Service",
                   "decorators": ["Service", "RequiredArgsConstructor"]},
        ),
        "exception": _java(repo, "NotFound", methods=[("NotFound", "()")], callers=5),
        "ts_behavior": _type(
            repo, "Api", "typescript", extra={"typescript_kind": "interface_declaration"},
            callers=5,
        ),
        "py_behavior": _type(
            repo, "Model", "python", bases=["BaseModel"], methods=[("calc", "(self)")], callers=5,
        ),
    }
    repo.commit()
    return q


DATA = {"enum", "lombok", "record", "dataclass", "pyenum", "pydantic", "pojo", "ts_iface"}


def _names(result):
    return [h["qualified_name"] for h in result["hub_nodes"]]


def test_data_types_are_filtered_and_behavior_classes_kept(repo):
    q = _build(repo)
    result = get_hub_nodes_func(repo_root=str(repo.root), top_n=100, exclude_data_types=True)
    names = _names(result)
    for key in DATA:
        assert q[key] not in names, key
    for key in set(q) - DATA:
        assert q[key] in names, key
    assert result["data_types_excluded"] == len(DATA)
    assert f"{len(DATA)} data type node(s) excluded" in result["summary"]


@pytest.mark.parametrize("key,label", [
    ("enum", "enum"), ("lombok", "lombok_data"), ("record", "record"),
    ("dataclass", "dataclass"), ("pyenum", "enum"), ("pydantic", "pydantic"),
    ("pojo", "pojo"), ("ts_iface", "interface"),
    ("real", None), ("lombok_biz", None), ("service", None), ("exception", None),
    ("ts_behavior", None), ("py_behavior", None),
])
def test_rows_carry_the_data_type_label_with_or_without_the_filter(repo, key, label):
    q = _build(repo)
    rows = {
        r["qualified_name"]: r
        for r in find_hub_nodes(repo, top_n=100, include_tests=True)
    }
    assert rows[q[key]]["data_type"] == label


def test_default_keeps_data_types(repo):
    q = _build(repo)
    result = get_hub_nodes_func(repo_root=str(repo.root), top_n=100)
    assert set(q.values()) <= set(_names(result))
    assert result["data_types_excluded"] == 0
    assert "data type" not in result["summary"]


def test_ranking_degree_and_total_degree_are_unchanged_by_the_filter(repo):
    q = _build(repo)
    big = 10**6
    plain = {h["qualified_name"]: h for h in find_hub_nodes(repo, top_n=big)}
    kept = find_hub_nodes(repo, top_n=big, exclude_data_types=True)
    assert len(kept) == len(plain) - len(DATA)
    for h in kept:
        before = plain[h["qualified_name"]]
        assert h["total_degree"] == before["total_degree"]
        assert h["ranking_degree"] == before["ranking_degree"]
    assert plain[q["enum"]]["total_degree"] == 9


def test_minimal_rows_show_data_type(repo):
    q = _build(repo)
    result = get_hub_nodes_func(repo_root=str(repo.root), top_n=100, detail_level="minimal")
    row = next(h for h in result["hub_nodes"] if h["name"] == "Color")
    assert row["data_type"] == "enum"
    assert q["enum"]


def test_total_and_paging_describe_the_filtered_list(repo):
    _build(repo)
    full = get_hub_nodes_func(repo_root=str(repo.root), top_n=100)
    first = get_hub_nodes_func(repo_root=str(repo.root), top_n=2, exclude_data_types=True)
    assert first["total"] == full["total"] - len(DATA)
    second = get_hub_nodes_func(
        repo_root=str(repo.root), top_n=2, exclude_data_types=True, cursor=first["next_cursor"],
    )
    assert not set(_names(first)) & set(_names(second))


def test_cursor_is_refused_for_the_other_data_type_value(repo):
    _build(repo)
    cursor = get_hub_nodes_func(repo_root=str(repo.root), top_n=2)["next_cursor"]
    with pytest.raises(ValueError):
        get_hub_nodes_func(
            repo_root=str(repo.root), top_n=2, cursor=cursor, exclude_data_types=True,
        )
    cursor = get_hub_nodes_func(
        repo_root=str(repo.root), top_n=2, exclude_data_types=True,
    )["next_cursor"]
    with pytest.raises(ValueError):
        get_hub_nodes_func(repo_root=str(repo.root), top_n=2, cursor=cursor)


def test_static_predicate_is_not_an_accessor(repo):
    qn = _java(repo, "TagConstants", methods=[("isStreamTag", "(String type)")])
    repo.commit()
    rows = {r["qualified_name"]: r for r in find_hub_nodes(repo, top_n=100)}
    assert rows[qn]["data_type"] is None


def test_enum_is_data_even_without_other_information():
    from code_review_graph.graph import GraphNode
    node = GraphNode(1, "Class", "E", "f::E", "f", 1, 2, "java", None, None, None, False,
                     None, {"type_form": "enum"})
    assert data_type_label(node) == "enum"
    node.kind = "Function"
    assert data_type_label(node) is None


# ---------------------------------------------------------------- parser facts


def _parse(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source)
    nodes, _ = CodeParser().parse_file(path)
    return {n.name: n for n in nodes if n.kind == "Class"}


def test_parser_marks_java_enum_and_keeps_java_kind(tmp_path):
    classes = _parse(tmp_path, "A.java", "public enum Color { RED }\n"
                     "@Data public class Dto { private int a; }\n")
    assert classes["Color"].extra["type_form"] == "enum"
    assert classes["Color"].extra["java_kind"] == "concrete"
    assert "type_form" not in classes["Dto"].extra


def test_parser_marks_kotlin_data_and_enum_classes(tmp_path):
    classes = _parse(tmp_path, "a.kt", "data class K(val a: Int)\nenum class Z { A }\n"
                     "class Q { fun f() {} }\n")
    assert classes["K"].extra["type_form"] == "data_class"
    assert classes["Z"].extra["type_form"] == "enum"
    assert "type_form" not in classes["Q"].extra


def test_parser_marks_ts_data_shapes_only(tmp_path):
    classes = _parse(tmp_path, "a.ts", """
export enum E { A }
export interface D { a: number; b?: string }
export interface Svc { run(): void }
export interface Cb { cb: (x: number) => void }
export type O = { a: number };
export type U = 'a' | 'b';
export type F = (x: number) => void;
export class C { run() {} }
""")
    assert classes["E"].extra["type_form"] == "enum"
    flagged = {n for n, c in classes.items() if c.extra.get("ts_data_shape")}
    assert flagged == {"D", "O", "U"}
