"""Ember convention resolver over synthetic classic, pods and addon apps."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path

FIXTURES = Path(__file__).parent / "fixtures" / "ember"


def _build(tmp_path: Path, name: str, copy_name: str | None = None) -> tuple[Path, sqlite3.Connection]:
    root = tmp_path / (copy_name or name)
    shutil.copytree(FIXTURES / name, root)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
    finally:
        store.close()
    conn = sqlite3.connect(get_db_path(root))
    conn.row_factory = sqlite3.Row
    return root.resolve(), conn


def _rel(root: Path, value: str) -> str:
    return value.replace(str(root) + "/", "")


def _edges(conn, root: Path, kind: str, where: str = "1=1"):
    rows = conn.execute(
        f"SELECT * FROM edges WHERE kind = ? AND {where} ORDER BY file_path, line, id", (kind,)  # noqa: S608
    ).fetchall()
    return [
        (_rel(root, r["source_qualified"]), _rel(root, r["target_qualified"]),
         json.loads(r["extra"]), r["confidence_tier"])
        for r in rows
    ]


@pytest.fixture(scope="module")
def classic(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("classic"), "classic")


@pytest.fixture(scope="module")
def pods(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("pods"), "pods")


@pytest.fixture(scope="module")
def addon(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("addon"), "addon")


def test_classic_injections_bind_to_service_files(classic) -> None:
    root, conn = classic
    bound = {(s, t) for s, t, _, _ in _edges(conn, root, "INJECTS", "extra LIKE '%\"ember_resolved\"%'")}
    assert ("app/routes/projects.js", "app/services/retry-queue.js") in bound
    assert ("app/services/audit-log.js", "app/services/retry-queue.js") in bound
    assert ("app/mixins/audited.js", "app/services/audit-log.js") in bound
    assert ("app/components/task-card.js", "app/services/audit-log.js") in bound
    assert ("app/controllers/projects.js", "app/routes/projects/show.js") not in bound


def test_injection_is_inferred_and_unresolved_one_says_why(classic) -> None:
    root, conn = classic
    rows = {e[2]["field_name"]: e for e in _edges(conn, root, "INJECTS", "file_path LIKE '%routes/projects.js'")}
    assert rows["queue"][3] == "INFERRED"
    missing = rows["missingThing"]
    assert missing[1] == "ember:service:not-in-this-repo"
    assert missing[2]["ember_resolution"] == "unresolved"
    assert missing[2]["ember_reason"] == "no_module"
    assert missing[3] == "UNRESOLVED"
    # ``session`` names no service module in this app either.
    assert rows["session"][2]["ember_reason"] == "no_module"


def test_controller_injection_resolves_by_controller_name_not_service(classic) -> None:
    root, conn = classic
    (row,) = _edges(conn, root, "INJECTS", "file_path LIKE '%controllers/projects.js'")
    assert row[1] == "ember:controller:projects/show"  # no controllers/projects/show.js here


def test_calls_through_injected_property_reach_the_service_method(classic) -> None:
    root, conn = classic
    calls = {
        (s.rsplit("::", 1)[-1], e["ember_method"]): t
        for s, t, e, _ in _edges(conn, root, "CALLS", "extra LIKE '%\"ember_resolved\"%'")
        if s.startswith("app/routes/projects.js")
    }
    assert calls[("model", "enqueue")] == "app/services/retry-queue.js::enqueue"
    assert calls[("model", "flush")] == "app/services/retry-queue.js::flush"


def test_same_named_local_method_is_not_confused_with_the_service_method(classic) -> None:
    """``this.queue.enqueue()`` must not bind to the route's own ``enqueue``."""
    root, conn = classic
    targets = [
        t for s, t, e, _ in _edges(conn, root, "CALLS")
        if s == "app/routes/projects.js::model" and e.get("ember_method") == "enqueue"
    ]
    assert targets == ["app/services/retry-queue.js::enqueue"]


def test_name_collision_between_two_services_binds_by_injected_service(classic) -> None:
    """Both retry-queue and audit-log define ``enqueue``; each caller gets its own."""
    root, conn = classic
    (call,) = [
        t for s, t, e, _ in _edges(conn, root, "CALLS")
        if s == "app/services/audit-log.js::record" and e.get("ember_method") == "enqueue"
    ]
    assert call == "app/services/retry-queue.js::enqueue"


def test_call_through_unresolved_service_is_left_unbound(classic) -> None:
    root, conn = classic
    rows = [
        (t, e) for s, t, e, _ in _edges(conn, root, "CALLS")
        if s == "app/routes/projects.js::model" and e.get("ember_receiver") == "missingThing"
    ]
    assert rows and all("ember_resolved" not in e for _, e in rows)


def test_missing_method_stays_a_claim_not_a_node(classic) -> None:
    root, conn = classic
    rows = [
        r for r in conn.execute("SELECT * FROM edges WHERE kind = 'CALLS' AND extra LIKE '%ember_property%'")
        if json.loads(r["extra"])["ember_method"] == "record" and r["target_resolution"] == "unresolved"
    ]
    # ``auditLog.record`` exists in the fixture, so it is bound; nothing is invented.
    assert not rows


def test_model_names_bind_for_store_calls_and_relationships(classic) -> None:
    root, conn = classic
    refs = {(s.rsplit("::", 1)[0], t, e.get("ember_relationship") or e.get("ember_store_method"))
            for s, t, e, _ in _edges(conn, root, "REFERENCES", "extra LIKE '%ember_ref%'")}
    assert ("app/routes/projects.js", "app/models/project.js", "findRecord") in refs
    assert ("app/routes/projects/show.js", "app/models/task.js", "query") in refs
    assert ("app/models/project.js", "app/models/task.js", "hasMany") in refs
    assert ("app/models/task.js", "app/models/project.js", "belongsTo") in refs
    # ``person`` has no model file: the placeholder stays.
    assert ("app/models/project.js", "ember:model:person", "belongsTo") in refs


def test_module_prefix_imports_need_two_hits_before_a_prefix_counts(classic) -> None:
    root, conn = classic
    imports = {t for _, t, _, _ in _edges(conn, root, "IMPORTS_FROM", "file_path LIKE '%routes/projects.js'")}
    assert "app/mixins/audited.js" in imports
    assert "app/utils/format.js" in imports


def test_a_lone_prefix_is_not_taken_for_an_app(tmp_path: Path) -> None:
    root = tmp_path / "solo"
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "services" / "a.js").write_text("export default {};\n")
    (root / "app" / "services" / "b.js").write_text(
        "import Service from '@ember/service';\nimport a from 'lodash/services/a';\n"
        "export default Service.extend({});\n")
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
    finally:
        store.close()
    conn = sqlite3.connect(get_db_path(root))
    targets = [r[0] for r in conn.execute(
        "SELECT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM' AND file_path LIKE '%b.js'")]
    assert "lodash/services/a" in targets


def test_template_invocations_bind_to_component_files(classic) -> None:
    root, conn = classic
    rows = {(e["ember_name"], e["ember_ref"]): t
            for _, t, e, _ in _edges(conn, root, "IMPORTS_FROM", "file_path LIKE '%templates/projects.hbs'")}
    assert rows[("task-card", "component")] == "app/components/task-card.js"
    assert rows[("project-link", "invocable")] == "ember:invocable:project-link"
    assert rows[("did-render-thing", "modifier")] == "ember:modifier:did-render-thing"
    assert ("commented-out-widget", "invocable") not in rows


def test_nested_angle_component_and_template_only_names(classic) -> None:
    root, conn = classic
    rows = {e["ember_name"]: t for _, t, e, _ in _edges(
        conn, root, "IMPORTS_FROM", "file_path LIKE '%templates/components/task-card.hbs'")}
    assert rows["task-card/footer"] == "app/components/task-card/footer.js"
    assert rows["status-pill"] == "app/components/status-pill.js"


def test_component_class_links_to_its_classic_template(classic) -> None:
    root, conn = classic
    links = [(s, t) for s, t, e, _ in _edges(conn, root, "IMPORTS_FROM") if e.get("ember_link")]
    assert ("app/components/task-card.js", "app/templates/components/task-card.hbs") in links


def test_routes_become_endpoints_handled_by_route_controller_template(classic) -> None:
    root, conn = classic
    endpoints = {json.loads(r["extra"])["route_name"]: r["qualified_name"]
                 for r in conn.execute("SELECT * FROM nodes WHERE kind = 'Endpoint'")
                 if "route_name" in json.loads(r["extra"])}
    assert set(endpoints) == {"projects", "projects.show", "archive", "about", "legacy-reports"}
    handlers = {}
    for s, t, e, tier in _edges(conn, root, "HANDLES"):
        handlers.setdefault(e["route_name"], set()).add((s.split("::")[0], e["ember_role"]))
        assert tier == "INFERRED"
    assert handlers["projects"] == {
        ("app/routes/projects.js", "route"),
        ("app/controllers/projects.js", "controller"),
        ("app/templates/projects.hbs", "template"),
    }
    assert handlers["projects.show"] == {
        ("app/routes/projects/show.js", "route"), ("app/templates/projects/show.hbs", "template"),
    }
    assert "about" not in handlers


def test_pods_layout_binds_services_routes_components_and_helpers(pods) -> None:
    root, conn = pods
    inj = {(e["field_name"], t) for _, t, e, _ in _edges(conn, root, "INJECTS")}
    assert ("sessionState", "app/services/session-state.js::SessionState") in inj
    assert ("alias", "app/services/session-state.js::SessionState") in inj
    assert ("store", "ember:service:store") in inj
    calls = {t for _, t, e, _ in _edges(conn, root, "CALLS", "extra LIKE '%ember_property%'")}
    assert "app/services/session-state.js::SessionState.refresh" in calls
    assert "app/services/session-state.js::SessionState.signOut" in calls
    handlers = {(s.split("::")[0], e["ember_role"]) for s, _, e, _ in _edges(conn, root, "HANDLES")}
    assert ("app/projects/route.js", "route") in handlers
    assert ("app/projects/controller.js", "controller") in handlers
    assert ("app/projects/template.hbs", "template") in handlers
    tmpl = {e["ember_name"]: t for _, t, e, _ in _edges(
        conn, root, "IMPORTS_FROM", "file_path LIKE '%components/user-badge/template.hbs'")}
    assert tmpl["format-user"] == "app/helpers/format-user.js"
    link = [(s, t) for s, t, e, _ in _edges(conn, root, "IMPORTS_FROM") if e.get("ember_link")]
    assert ("app/components/user-badge/component.js",
            "app/components/user-badge/template.hbs") in link
    assert {(s.split("::")[0], t) for s, t, e, _ in _edges(conn, root, "IMPORTS_FROM")
            if e.get("ember_name") == "user-badge"} == {
        ("app/projects/template.hbs", "app/components/user-badge/component.js")}


def test_self_referencing_relationship_binds_to_its_own_model(pods) -> None:
    root, conn = pods
    (row,) = _edges(conn, root, "REFERENCES", "extra LIKE '%ember_relationship%'")
    assert row[1] == "app/models/project.js::Project"


def test_addon_reexport_is_followed_and_dummy_app_wins_locally(addon) -> None:
    root, conn = addon
    (row,) = _edges(conn, root, "INJECTS")
    assert row[1] == "addon/services/logger.js::Logger"
    (call,) = _edges(conn, root, "CALLS", "extra LIKE '%ember_property%'")
    assert call[1] == "addon/services/logger.js::Logger.info"


def test_ambiguous_service_stays_unbound_with_a_reason(tmp_path: Path) -> None:
    root = tmp_path / "amb"
    for sub in ("one", "two"):
        d = root / sub / "addon" / "services"
        d.mkdir(parents=True)
        (d / "shared.js").write_text(
            "import Service from '@ember/service';\nexport default Service.extend({ run() {} });\n")
    consumer = root / "app" / "routes"
    consumer.mkdir(parents=True)
    (consumer / "index.js").write_text(
        "import Route from '@ember/routing/route';\nimport { inject as service } from '@ember/service';\n"
        "export default Route.extend({ shared: service(), go() { this.shared.run(); } });\n")
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
    finally:
        store.close()
    conn = sqlite3.connect(get_db_path(root))
    conn.row_factory = sqlite3.Row
    (row,) = _edges(conn, root.resolve(), "INJECTS")
    assert row[1] == "ember:service:shared"
    assert row[2]["ember_reason"] == "ambiguous"
    assert row[3] == "UNRESOLVED"


def test_nothing_from_values_or_template_text_reaches_the_graph(classic) -> None:
    root, conn = classic
    dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM nodes").fetchall()]
                      + [tuple(r) for r in conn.execute("SELECT * FROM edges").fetchall()])
    assert "classic-secret-label" not in dump
    assert "classic.title.secret" not in dump


def test_non_ember_repository_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.js").write_text("export function f() { return g(); }\nfunction g() {}\n")
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        from code_review_graph.ember_resolver import resolve_ember
        assert resolve_ember(store) == {"ember_files": 0}
    finally:
        store.close()


def test_rerunning_the_resolver_is_idempotent(tmp_path: Path) -> None:
    from code_review_graph.ember_resolver import resolve_ember

    root = tmp_path / "again"
    shutil.copytree(FIXTURES / "classic", root)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        before = store._conn.execute(
            "SELECT kind, source_qualified, target_qualified, extra FROM edges ORDER BY id").fetchall()
        resolve_ember(store)
        resolve_ember(store)
        after = store._conn.execute(
            "SELECT kind, source_qualified, target_qualified, extra FROM edges ORDER BY id").fetchall()
    finally:
        store.close()
    key = lambda rows: sorted(tuple(r) for r in rows)  # noqa: E731
    assert key(before) == key(after)


def test_deleting_a_service_unbinds_on_the_next_update(tmp_path: Path) -> None:
    from code_review_graph.incremental import incremental_update

    root = tmp_path / "gone"
    shutil.copytree(FIXTURES / "classic", root)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        (root / "app" / "services" / "retry-queue.js").unlink()
        incremental_update(root, store, changed_files=["app/services/retry-queue.js"])
        rows = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind = 'INJECTS' "
            "AND file_path LIKE '%routes/projects.js'").fetchall()
    finally:
        store.close()
    by_field = {json.loads(r["extra"])["field_name"]: r for r in rows}
    assert by_field["queue"]["target_qualified"] == "ember:service:retry-queue"
    assert json.loads(by_field["queue"]["extra"])["ember_reason"] == "no_module"


def _dump(conn) -> str:
    rows = [tuple(r) for r in conn.execute(
        "SELECT kind, source_qualified, target_qualified, file_path, line, extra, "
        "confidence_tier FROM edges ORDER BY kind, source_qualified, target_qualified, line, extra"
    ).fetchall()]
    rows += [tuple(r) for r in conn.execute(
        "SELECT kind, qualified_name, extra FROM nodes ORDER BY qualified_name").fetchall()]
    return json.dumps(rows, sort_keys=True)


def test_two_builds_of_the_same_tree_agree(tmp_path: Path) -> None:
    a_root, a = _build(tmp_path, "classic", "same")
    b_root, b = _build(tmp_path / "second", "classic", "same")
    norm = lambda text, root: text.replace(str(root), "<root>")  # noqa: E731
    assert hashlib.sha256(norm(_dump(a), a_root).encode()).hexdigest() == \
        hashlib.sha256(norm(_dump(b), b_root).encode()).hexdigest()
