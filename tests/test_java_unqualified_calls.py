"""A plain ``helper()`` in Java binds to the class or ancestor that declares it.

Java reads an unqualified call in the calling class first, then in what the
class inherits. A test class calling ``givenAuthenticated()`` that its abstract
base class declares used to stay unresolved, because the call has no receiver
for the resolver to read and the base class is not imported when it shares the
package. The call now follows the same INHERITS walk ``this.helper()`` uses.

Each test pins a way the pass could pick a wrong target: an ancestor the graph
cannot name, a call on another object, a method name that two unrelated classes
share, overloads, and a nearer class that overrides the method.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.tools.build import build_or_update_graph


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def _calls_from(store: GraphStore, caller_suffix: str, method: str) -> list[dict]:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra, target_resolution, "
        "confidence_tier FROM edges WHERE kind = 'CALLS' ORDER BY line, id"
    ).fetchall()
    return [
        {
            "target": row["target_qualified"],
            "extra": json.loads(row["extra"] or "{}"),
            "resolution": row["target_resolution"],
            "tier": row["confidence_tier"],
        }
        for row in rows
        if row["source_qualified"].endswith(caller_suffix)
        and row["target_qualified"].rsplit(".", 1)[-1].rsplit("::", 1)[-1].split(":")[0] == method
    ]


def _one(store: GraphStore, caller_suffix: str, method: str) -> dict:
    rows = _calls_from(store, caller_suffix, method)
    assert len(rows) == 1, rows
    return rows[0]


BASE = (
    "package base;\n\n"
    "public abstract class BaseApiTest {\n"
    "    protected void givenAuthenticated() {}\n"
    "}\n"
)


def test_call_to_a_method_the_base_class_declares_binds_across_packages(tmp_path):
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/web/UserApiTest.java": (
            "package web;\n\n"
            "import base.BaseApiTest;\n\n"
            "public class UserApiTest extends BaseApiTest {\n"
            "    public void testList() {\n"
            "        givenAuthenticated();\n"
            "    }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "UserApiTest.testList", "givenAuthenticated")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/base/BaseApiTest.java::BaseApiTest.givenAuthenticated"
    assert row["resolution"] == "direct"


def test_call_binds_to_a_base_class_in_the_same_package_without_an_import(tmp_path):
    store = _build(tmp_path, {
        "src/web/BaseApiTest.java": BASE.replace("package base;", "package web;"),
        "src/web/UserApiTest.java": (
            "package web;\n\n"
            "public class UserApiTest extends BaseApiTest {\n"
            "    public void testList() { givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "UserApiTest.testList", "givenAuthenticated")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/web/BaseApiTest.java::BaseApiTest.givenAuthenticated"
    assert row["extra"]["inherited_through"].endswith("BaseApiTest.java::BaseApiTest")


def test_call_binds_through_two_levels_of_ancestors(tmp_path):
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/base/Mid.java": "package base;\n\npublic abstract class Mid extends BaseApiTest {}\n",
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import base.Mid;\n\n"
            "public class Leaf extends Mid {\n"
            "    void run() { givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "Leaf.run", "givenAuthenticated")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/base/BaseApiTest.java::BaseApiTest.givenAuthenticated"


def test_the_nearest_class_that_declares_the_method_wins(tmp_path):
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/base/Mid.java": (
            "package base;\n\n"
            "public abstract class Mid extends BaseApiTest {\n"
            "    @Override protected void givenAuthenticated() {}\n"
            "}\n"
        ),
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import base.Mid;\n\n"
            "public class Leaf extends Mid {\n"
            "    void run() { givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "Leaf.run", "givenAuthenticated")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/base/Mid.java::Mid.givenAuthenticated"


def test_overloaded_ancestor_method_is_ambiguous_not_bound(tmp_path):
    store = _build(tmp_path, {
        "src/base/Base.java": (
            "package base;\n\n"
            "public abstract class Base {\n"
            "    protected void send(String a) {}\n"
            "    protected void send(String a, int b) {}\n"
            "}\n"
        ),
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import base.Base;\n\n"
            "public class Leaf extends Base {\n"
            "    void run() { send(\"x\"); }\n"
            "}\n"
        ),
    })
    try:
        row = _one(store, "Leaf.run", "send")
    finally:
        store.close()

    assert row["resolution"] == "unresolved"
    assert row["tier"] == "AMBIGUOUS"
    assert row["extra"]["ambiguous_target_count"] == 2


def test_ancestor_outside_the_graph_leaves_the_call_unresolved(tmp_path):
    """``Leaf extends ext.Library`` may declare ``givenAuthenticated`` itself."""
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import ext.Library;\n\n"
            "public class Leaf extends Library {\n"
            "    void run() { givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    try:
        row = _one(store, "Leaf.run", "givenAuthenticated")
    finally:
        store.close()

    assert row["resolution"] == "unresolved"


def test_unrelated_class_with_the_same_method_name_does_not_bind(tmp_path):
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/web/Leaf.java": (
            "package web;\n\n"
            "public class Leaf {\n"
            "    void run() { givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    try:
        row = _one(store, "Leaf.run", "givenAuthenticated")
    finally:
        store.close()

    assert row["resolution"] == "unresolved"


def test_a_call_on_another_object_is_not_read_as_an_inherited_call(tmp_path):
    """``other.givenAuthenticated()`` is whatever ``other`` is, not ``Leaf``'s base."""
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import base.BaseApiTest;\n\n"
            "public class Leaf extends BaseApiTest {\n"
            "    void run(Unknown other) { other.givenAuthenticated(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        rows = _calls_from(store, "Leaf.run", "givenAuthenticated")
    finally:
        store.close()

    assert rows
    assert all(
        row["target"] != f"{root}/src/base/BaseApiTest.java::BaseApiTest.givenAuthenticated"
        for row in rows
    )


def test_library_static_import_call_stays_unresolved(tmp_path):
    store = _build(tmp_path, {
        "src/base/BaseApiTest.java": BASE,
        "src/web/Leaf.java": (
            "package web;\n\n"
            "import static org.junit.Assert.assertEquals;\n"
            "import base.BaseApiTest;\n\n"
            "public class Leaf extends BaseApiTest {\n"
            "    void run() { assertEquals(1, 1); }\n"
            "}\n"
        ),
    })
    try:
        row = _one(store, "Leaf.run", "assertEquals")
    finally:
        store.close()

    assert row["resolution"] == "unresolved"


# -- Resolution that was already there, pinned beside the new pass -------------

SERVICE = (
    "package svc;\n\n"
    "public class UserService {\n"
    "    public static int count() { return 0; }\n"
    "    public void save() {}\n"
    "}\n"
)


def test_call_on_a_field_typed_with_an_indexed_class_binds(tmp_path):
    store = _build(tmp_path, {
        "src/svc/UserService.java": SERVICE,
        "src/web/Controller.java": (
            "package web;\n\n"
            "import svc.UserService;\n\n"
            "public class Controller {\n"
            "    private final UserService users;\n"
            "    public Controller(UserService users) { this.users = users; }\n"
            "    public void post() { users.save(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "Controller.post", "save")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/svc/UserService.java::UserService.save"


def test_local_variable_shadows_the_field_type(tmp_path):
    store = _build(tmp_path, {
        "src/svc/UserService.java": SERVICE,
        "src/svc/Other.java": (
            "package svc;\n\npublic class Other {\n    public void save() {}\n}\n"
        ),
        "src/web/Controller.java": (
            "package web;\n\n"
            "import svc.Other;\n"
            "import svc.UserService;\n\n"
            "public class Controller {\n"
            "    private UserService users;\n"
            "    public void post() {\n"
            "        Other users = new Other();\n"
            "        users.save();\n"
            "    }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "Controller.post", "save")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/svc/Other.java::Other.save"


def test_static_call_on_an_indexed_class_binds(tmp_path):
    store = _build(tmp_path, {
        "src/svc/UserService.java": SERVICE,
        "src/web/Controller.java": (
            "package web;\n\n"
            "import svc.UserService;\n\n"
            "public class Controller {\n"
            "    public int total() { return UserService.count(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _one(store, "Controller.total", "count")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/svc/UserService.java::UserService.count"


def test_call_on_a_jdk_type_stays_unresolved(tmp_path):
    store = _build(tmp_path, {
        "src/svc/UserService.java": SERVICE,
        "src/web/Controller.java": (
            "package web;\n\n"
            "import java.util.List;\n\n"
            "public class Controller {\n"
            "    public void post(List<String> names) { names.add(\"x\"); }\n"
            "}\n"
        ),
    })
    try:
        row = _one(store, "Controller.post", "add")
    finally:
        store.close()

    assert row["resolution"] == "unresolved"
