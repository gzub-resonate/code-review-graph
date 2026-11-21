"""Cross-file Scala import and call resolution.

Every case builds a scratch copy of ``tests/fixtures/scala_resolution`` (or a
few small files written inline) with ``full_build`` / ``incremental_update``,
the paths the CLI and MCP tools use, and reads the persisted graph back.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.scala_resolver import _arity_bounds, resolve_scala_calls
from code_review_graph.tools.query import query_graph

FIXTURE = Path(__file__).parent / "fixtures" / "scala_resolution"
MAIN = "src/main/scala/com/acme/"
TEST = "src/test/scala/com/acme/"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _copy_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    (root / ".git").mkdir()
    return root


def _store(root: Path) -> GraphStore:
    return GraphStore(root / ".code-review-graph" / "graph.db")


class Graph:
    """Short, root-relative views over a built graph."""

    def __init__(self, store: GraphStore, root: Path) -> None:
        self.store = store
        self.prefix = f"{root.resolve()}/"
        self.main = MAIN

    def short(self, value: str) -> str:
        return value.replace(self.prefix, "").replace(self.main, "").replace(TEST, "")

    def call(self, file: str, line: int, name: str | None = None) -> dict:
        rows = [
            r for r in self.store._conn.execute(
                "SELECT * FROM edges WHERE kind = 'CALLS' AND line = ? AND file_path LIKE ?",
                (line, f"%{file}"),
            )
        ]
        if name is not None:
            rows = [r for r in rows if json.loads(r["extra"]).get("scala_name") == name]
        assert len(rows) == 1, (file, line, name, [r["target_qualified"] for r in rows])
        row = rows[0]
        extra = json.loads(row["extra"])
        return {
            "target": self.short(row["target_qualified"]),
            "rule": extra.get("scala_resolved"),
            "ambiguous": [self.short(t) for t in extra.get("ambiguous_targets", [])],
            "tier": row["confidence_tier"],
            "resolution": row["target_resolution"],
            "extra": extra,
        }

    def imports(self, file: str) -> list[tuple[int, str]]:
        return sorted(
            (r["line"], self.short(r["target_qualified"]))
            for r in self.store._conn.execute(
                "SELECT * FROM edges WHERE kind = 'IMPORTS_FROM' AND file_path LIKE ?",
                (f"%{file}",),
            )
        )

    def snapshot(self) -> dict:
        """Everything a build writes, without row ids or timestamps."""
        conn = self.store._conn
        def canonical(row) -> tuple:
            # ``extra`` is JSON: compare it as data, not as key order.
            return tuple(
                json.dumps(json.loads(v), sort_keys=True) if key == "extra" else v
                for key, v in zip(row.keys(), row)
            )

        nodes = sorted(
            canonical(r) for r in conn.execute(
                "SELECT qualified_name, kind, name, file_path, line_start, line_end, "
                "parent_name, extra FROM nodes",
            )
        )
        edges = sorted(
            canonical(r) for r in conn.execute(
                "SELECT kind, source_qualified, target_qualified, file_path, line, extra, "
                "confidence_tier, target_resolution FROM edges",
            )
        )
        return {"nodes": nodes, "edges": edges}


def _built(tmp_path: Path):
    root = _copy_fixture(tmp_path)
    store = _store(root)
    full_build(root, store)
    return root, store, Graph(store, root)


@pytest.fixture()
def graph(tmp_path):
    root, store, g = _built(tmp_path)
    try:
        yield g
    finally:
        store.close()


RUNNER = "app/Runner.scala"
EXTRAS = "app/Extras.scala"


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_plain_selector_and_renamed_imports_resolve_to_their_files(graph):
    imports = graph.imports(RUNNER)
    shapes = "model/Shapes.scala"
    helpers = "util/Helpers.scala"
    # ``{Circle, Geometry, Point, Store => ModelStore}`` is four names, one file.
    assert [t for line, t in imports if line == 3] == [shapes] * 4
    assert (4, helpers) in imports
    # ``Palette._`` is a wildcard over an object: the file that defines it.
    assert (5, helpers) in imports


def test_import_outside_the_repository_keeps_its_dotted_name(graph):
    assert (6, "scala.collection.mutable") in graph.imports(RUNNER)
    assert (3, "org.example.Helpers") in graph.imports("chain/External.scala")


def test_package_wildcard_import_stays_raw(graph):
    # A package spans many files, so there is no single file to point at.
    assert (4, "com.acme.util.*") in graph.imports(EXTRAS)


def test_relative_import_under_chained_package_clauses(graph):
    # ``package com.acme`` then ``package chain`` makes ``util.Helpers`` mean
    # ``com.acme.util.Helpers``.
    assert graph.imports("chain/Chained.scala") == [(4, "util/Helpers.scala")]
    assert graph.call("chain/Chained.scala", 11, "twice")["target"] == (
        "util/Helpers.scala::Helpers.twice"
    )


def test_import_edges_record_what_the_source_wrote(graph):
    rows = graph.store._conn.execute(
        "SELECT extra FROM edges WHERE kind = 'IMPORTS_FROM' AND file_path LIKE ? "
        "AND line = 3 ORDER BY id",
        (f"%{RUNNER}",),
    ).fetchall()
    extras = [json.loads(r["extra"]) for r in rows]
    renamed = [e for e in extras if e.get("scala_alias")]
    assert [e["scala_import"] for e in renamed] == ["com.acme.model.Store"]
    assert renamed[0]["scala_alias"] == "ModelStore"
    assert {e.get("import_resolution") for e in extras} == {"scala_scope"}


# ---------------------------------------------------------------------------
# Rule a: Foo.bar(...)
# ---------------------------------------------------------------------------


def test_object_member_through_an_explicit_import(graph):
    call = graph.call(RUNNER, 12, "twice")
    assert call["target"] == "util/Helpers.scala::Helpers.twice"
    assert call["rule"] == "object_member"
    assert call["tier"] == "INFERRED"
    assert call["resolution"] == "direct"
    assert graph.call(RUNNER, 13, "origin")["target"] == "model/Shapes.scala::Geometry.origin"


def test_object_member_in_the_same_package_needs_no_import(graph):
    assert graph.call("util/Uses.scala", 4, "twice")["target"] == (
        "util/Helpers.scala::Helpers.twice"
    )
    assert graph.call("util/Uses.scala", 5, "render")["target"] == (
        "util/Helpers.scala::Palette.render"
    )


def test_object_member_through_a_package_wildcard(graph):
    assert graph.call(EXTRAS, 30, "twice")["target"] == "util/Helpers.scala::Helpers.twice"


def test_two_objects_with_the_same_name_in_different_objects_stay_apart(graph):
    # ``Helpers.twice`` and ``Palette.twice`` share a name, not an owner.
    assert graph.call("app/RunnerSpec.scala", 8, "twice")["target"] == (
        "util/Helpers.scala::Helpers.twice"
    )


# ---------------------------------------------------------------------------
# Rule b: constructors and companion apply
# ---------------------------------------------------------------------------


def test_new_links_the_class_even_with_a_companion_object(graph):
    call = graph.call(RUNNER, 14, "Circle")
    assert call["target"] == "model/Shapes.scala::Circle"
    assert call["rule"] == "constructor"
    # ``Store`` has a companion object; ``new Store`` still reaches the class.
    inside = graph.call("model/Shapes.scala", 22, "Store")
    assert inside["target"] == "model/Shapes.scala::Store"
    assert inside["resolution"] == "direct"


def test_companion_apply_links_apply_not_the_class(graph):
    call = graph.call(RUNNER, 18, "ModelStore")
    assert call["target"] == "model/Shapes.scala::Store.apply"
    assert call["rule"] == "apply"


def test_case_class_call_links_the_class(graph):
    call = graph.call(RUNNER, 19, "Point")
    assert call["target"] == "model/Shapes.scala::Point"
    assert call["rule"] == "case_class_constructor"


def test_case_class_with_an_explicit_companion_apply_is_ambiguous(graph):
    # The compiler's apply and the one written: nothing at the call picks one.
    call = graph.call(EXTRAS, 47, "Wrapper")
    assert call["resolution"] == "unresolved"
    assert call["tier"] == "AMBIGUOUS"
    assert call["ambiguous"] == [
        f"{EXTRAS}::Wrapper", f"{EXTRAS}::Wrapper.apply",
    ]


# ---------------------------------------------------------------------------
# Rule c: values of a declared type, enclosing and inherited members
# ---------------------------------------------------------------------------


def test_receiver_declared_as_a_parameter_field_or_new(graph):
    assert graph.call(RUNNER, 17, "save")["target"] == "model/Shapes.scala::Store.save"
    assert graph.call(RUNNER, 16, "grow")["target"] == "model/Shapes.scala::Circle.grow"
    assert graph.call(RUNNER, 15, "area")["rule"] == "typed_receiver"
    assert graph.call(EXTRAS, 35, "area")["target"] == "model/Shapes.scala::Circle.area"


def test_call_on_a_freshly_built_value(graph):
    call = graph.call(EXTRAS, 37, "grow")
    assert call["target"] == "model/Shapes.scala::Circle.grow"


def test_member_inherited_through_an_in_repository_parent(graph):
    call = graph.call(RUNNER, 20, "inheritedHelper")
    assert call["target"] == "app/Base.scala::Base.inheritedHelper"
    assert graph.call(RUNNER, 21, "inheritedHelper")["rule"] == "this_member"


def test_member_brought_in_by_a_wildcard_import_of_an_object(graph):
    call = graph.call(RUNNER, 22, "render")
    assert call["target"] == "util/Helpers.scala::Palette.render"
    assert call["rule"] == "wildcard_member"


def test_member_reached_through_two_levels_of_inheritance(tmp_path):
    _write(tmp_path, {
        "a/Tree.scala": (
            "package demo\n"
            "class Top { def topMethod(): Int = 1 }\n"
            "class Mid extends Top\n"
            "class Leaf extends Mid\n"
        ),
        "b/Use.scala": (
            "package demo\n"
            "object Use { def run(leaf: Leaf): Int = leaf.topMethod() }\n"
        ),
    })
    (tmp_path / ".git").mkdir()
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        g = Graph(store, tmp_path)
        call = g.call("b/Use.scala", 2, "topMethod")
    assert call["target"] == "a/Tree.scala::Top.topMethod"
    assert call["extra"]["scala_via"].endswith("::Leaf")


def test_private_member_of_a_parent_is_not_inherited(tmp_path):
    _write(tmp_path, {
        "a/Parent.scala": (
            "package demo\n"
            "class Parent {\n"
            "  private def hidden(): Int = 1\n"
            "  private[this] def alsoHidden(): Int = 1\n"
            "  def shown(): Int = 2\n"
            "}\n"
        ),
        "b/Child.scala": (
            "package demo\n"
            "class Child extends Parent {\n"
            "  def a(): Int = hidden()\n"
            "  def b(): Int = alsoHidden()\n"
            "  def c(): Int = shown()\n"
            "}\n"
        ),
    })
    (tmp_path / ".git").mkdir()
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        g = Graph(store, tmp_path)
        hidden = g.call("b/Child.scala", 3, "hidden")
        also = g.call("b/Child.scala", 4, "alsoHidden")
        shown = g.call("b/Child.scala", 5, "shown")
    assert hidden["target"] == "hidden"
    assert also["target"] == "alsoHidden"
    assert shown["target"] == "a/Parent.scala::Parent.shown"


# ---------------------------------------------------------------------------
# Names that must stay unresolved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "line", "name"),
    [
        (RUNNER, 23, "Map"),          # scala.collection.mutable.Map
        (RUNNER, 24, "println"),      # Predef
        (RUNNER, 26, "drop"),         # a value of unknown type; Store.drop exists
        (RUNNER, 27, "load"),         # a lambda parameter; Store.load exists
        (RUNNER, 29, "thing"),        # an object that is not in the repository
        (EXTRAS, 32, "assert"),       # Predef; Helpers.assert exists in the repository
        ("chain/External.scala", 6, "twice"),  # imported from org.example
    ],
)
def test_library_names_are_never_linked(graph, file, line, name):
    call = graph.call(file, line, name)
    assert call["target"] == name
    assert call["rule"] is None
    assert call["resolution"] == "unresolved"


def test_a_wildcard_member_is_not_taken_when_the_class_has_an_outside_parent(graph):
    # ``Blocked extends scala.Serializable``: the parent could declare ``render``.
    call = graph.call(EXTRAS, 26, "render")
    assert call["target"] == "render"
    assert call["rule"] is None


def test_argument_count_rules_out_another_overload(graph):
    assert graph.call(EXTRAS, 31, "twice")["target"] == "twice"
    assert graph.call(EXTRAS, 30, "twice")["rule"] == "object_member"


def test_overloads_with_the_same_arity_are_ambiguous_not_guessed(graph):
    call = graph.call(RUNNER, 28, "pick")
    assert call["resolution"] == "unresolved"
    assert call["tier"] == "AMBIGUOUS"
    assert len(call["ambiguous"]) == 2
    assert all("Helpers.pick" in target for target in call["ambiguous"])


def test_an_object_declared_twice_is_ambiguous(graph):
    call = graph.call("dup/Use.scala", 4, "hi")
    assert call["resolution"] == "unresolved"
    assert sorted(call["ambiguous"]) == [
        "dup/One.scala::Twin.hi", "dup/Two.scala::Twin.hi",
    ]


def test_a_local_value_shadows_an_object_of_the_same_name(tmp_path):
    _write(tmp_path, {
        "a/Helpers.scala": "package demo\nobject Helpers { def twice(x: Int): Int = x }\n",
        "b/Use.scala": (
            "package demo\n"
            "class Other { def twice(x: Int): Int = x }\n"
            "object Use {\n"
            "  def run(): Int = {\n"
            "    val Helpers = new Other\n"
            "    Helpers.twice(1)\n"
            "  }\n"
            "  def viaParam(Helpers: Other): Int = Helpers.twice(2)\n"
            "}\n"
        ),
    })
    (tmp_path / ".git").mkdir()
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        g = Graph(store, tmp_path)
        local = g.call("b/Use.scala", 6, "twice")
        param = g.call("b/Use.scala", 8, "twice")
    assert local["rule"] is None
    assert param["rule"] is None


def test_a_name_imported_from_outside_hides_the_same_package_object(graph):
    # ``com.acme.util.Helpers`` is in the repository but not in scope there.
    call = graph.call("chain/External.scala", 6, "twice")
    assert call["resolution"] == "unresolved"


# ---------------------------------------------------------------------------
# The rest of the graph keeps working
# ---------------------------------------------------------------------------


def test_callers_of_finds_calls_from_other_files(tmp_path):
    root, store, _ = _built(tmp_path)
    try:
        result = query_graph("callers_of", "Helpers.twice", str(root))
    finally:
        store.close()
    assert result["status"] == "ok", result.get("summary")
    names = {row["name"] for row in result["results"]}
    assert {"run", "go", "samePackage", "test_run"} <= names


def test_resolved_calls_count_as_direct_and_inferred(graph):
    rows = graph.store._conn.execute(
        "SELECT confidence_tier, target_resolution FROM edges WHERE kind = 'CALLS' "
        "AND extra LIKE '%\"scala_resolved\"%'",
    ).fetchall()
    assert rows
    assert {(r[0], r[1]) for r in rows} == {("INFERRED", "direct")}


def test_tested_by_mirrors_follow_a_rebound_call(graph):
    rows = graph.store._conn.execute(
        "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'TESTED_BY' "
        "AND source_qualified LIKE '%Helpers.twice'",
    ).fetchall()
    assert [(graph.short(r[0]), graph.short(r[1])) for r in rows] == [
        ("util/Helpers.scala::Helpers.twice", "app/RunnerSpec.scala::RunnerSpec.test_run"),
    ]


def test_resolver_is_idempotent(graph):
    before = graph.snapshot()
    stats = resolve_scala_calls(graph.store)
    assert stats["calls_resolved"] > 0
    assert graph.snapshot() == before


def test_edges_from_an_older_parser_are_left_alone(graph):
    conn = graph.store._conn
    conn.execute(
        "UPDATE edges SET extra = json_remove(extra, '$.scala_form') "
        "WHERE kind = 'CALLS' AND extra LIKE '%scala_form%'",
    )
    conn.commit()
    before = graph.snapshot()
    resolve_scala_calls(graph.store)
    assert graph.snapshot() == before


def test_non_scala_repository_is_untouched(tmp_path):
    _write(tmp_path, {"app.py": "def helper():\n    return 1\n\ndef main():\n    helper()\n"})
    (tmp_path / ".git").mkdir()
    with _store(tmp_path) as store:
        result = full_build(tmp_path, store)
        assert result["scala_resolution"] == {
            "files_indexed": 0,
            "imports_resolved": 0,
            "calls_resolved": 0,
            "calls_ambiguous": 0,
            "calls_restored": 0,
        }


# ---------------------------------------------------------------------------
# Determinism, update equivalence, bounds
# ---------------------------------------------------------------------------


def test_two_builds_write_the_same_graph(tmp_path):
    first, store_a, graph_a = _built(tmp_path / "one")
    second, store_b, graph_b = _built(tmp_path / "two")
    try:
        a = graph_a.snapshot()
        b = graph_b.snapshot()
    finally:
        store_a.close()
        store_b.close()
    prefix_a, prefix_b = f"{first.resolve()}/", f"{second.resolve()}/"

    def strip(rows: dict, prefix: str) -> str:
        return json.dumps(rows, sort_keys=True).replace(prefix, "")

    assert strip(a, prefix_a) == strip(b, prefix_b)


def _fresh_snapshot(root: Path) -> str:
    data = root / ".code-review-graph"
    shutil.rmtree(data, ignore_errors=True)
    with _store(root) as store:
        full_build(root, store)
        snap = Graph(store, root).snapshot()
    return json.dumps(snap, sort_keys=True)


def _updated_snapshot(root: Path, changed: list[str]) -> str:
    with _store(root) as store:
        incremental_update(root, store, changed_files=changed)
        snap = Graph(store, root).snapshot()
    return json.dumps(snap, sort_keys=True)


def test_update_after_adding_a_member_equals_a_fresh_build(tmp_path):
    root = _copy_fixture(tmp_path)
    with _store(root) as store:
        full_build(root, store)
    helpers = root / MAIN / "util/Helpers.scala"
    helpers.write_text(
        helpers.read_text().replace(
            "def twice(x: Int): Int = x * 2",
            "def twice(x: Int): Int = x * 2\n  def thrice(x: Int): Int = x * 3",
        ),
        encoding="utf-8",
    )
    updated = _updated_snapshot(root, [f"{MAIN}util/Helpers.scala"])
    assert updated == _fresh_snapshot(root)


def test_update_after_a_second_definition_makes_calls_ambiguous(tmp_path):
    root = _copy_fixture(tmp_path)
    with _store(root) as store:
        full_build(root, store)
    _write(root, {f"{MAIN}util/Copy.scala": (
        "package com.acme.util\nobject Helpers { def twice(x: Int): Int = x }\n"
    )})
    updated = _updated_snapshot(root, [f"{MAIN}util/Copy.scala"])
    assert updated == _fresh_snapshot(root)
    with _store(root) as store:
        call = Graph(store, root).call(RUNNER, 12, "twice")
    assert call["resolution"] == "unresolved"
    assert len(call["ambiguous"]) == 2


def test_update_after_deleting_a_definition_unlinks_its_callers(tmp_path):
    root = _copy_fixture(tmp_path)
    with _store(root) as store:
        full_build(root, store)
    (root / MAIN / "util/Helpers.scala").unlink()
    updated = _updated_snapshot(root, [f"{MAIN}util/Helpers.scala"])
    assert updated == _fresh_snapshot(root)
    with _store(root) as store:
        call = Graph(store, root).call(RUNNER, 12, "twice")
    assert call["target"] == "twice"
    assert call["rule"] is None


def test_update_after_editing_a_caller_equals_a_fresh_build(tmp_path):
    root = _copy_fixture(tmp_path)
    with _store(root) as store:
        full_build(root, store)
    uses = root / MAIN / "util/Uses.scala"
    uses.write_text(uses.read_text() + "\nobject More { def x(): Int = Helpers.twice(5) }\n")
    updated = _updated_snapshot(root, [f"{MAIN}util/Uses.scala"])
    assert updated == _fresh_snapshot(root)


def test_cyclic_and_deep_inheritance_terminates(tmp_path):
    chain = "".join(f"class C{i} extends C{i + 1}\n" for i in range(60))
    _write(tmp_path, {
        "a/Cycle.scala": (
            "package demo\n"
            "class A extends B\nclass B extends A\n"
            + chain + "class C60 { def deep(): Int = 1 }\n"
        ),
        "b/Use.scala": (
            "package demo\n"
            "object Use {\n"
            "  def a(x: A): Int = x.missing()\n"
            "  def b(x: C0): Int = x.deep()\n"
            "}\n"
        ),
    })
    (tmp_path / ".git").mkdir()
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        g = Graph(store, tmp_path)
        assert g.call("b/Use.scala", 3, "missing")["rule"] is None
        # Sixty parents is past the walk's bound: unresolved, not an error.
        assert g.call("b/Use.scala", 4, "deep")["rule"] is None


@pytest.mark.parametrize(
    ("params", "bounds"),
    [
        ("()", (0, 0)),
        ("(a: Int)", (1, 1)),
        ("(a: Int, b: Map[String, Int])", (2, 2)),
        ("(a: Int, b: Int = 3)", (1, 2)),
        ("(a: Int, rest: String*)", (1, float("inf"))),
        ("(f: (Int, Int) => Int)", (1, 1)),
        ("(implicit s: Session)", None),
        (None, None),
    ],
)
def test_arity_bounds_read_the_first_parameter_list(params, bounds):
    assert _arity_bounds(params) == bounds
