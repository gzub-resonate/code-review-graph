# ruff: noqa: E501
"""Ember modules at parse time: injections, routes, Ember Data names, roles."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return CodeParser(root).parse_file(path)


def _edges(edges, kind: str):
    return [e for e in edges if e.kind == kind]


def _injects(edges):
    return {(e.extra["field_name"], e.target, e.extra["ember_spelling"]) for e in _edges(edges, "INJECTS")}


def test_every_injection_spelling_is_recorded(tmp_path: Path) -> None:
    _, edges = _parse(tmp_path, "app/services/probe.js", """
import Service, { inject as service } from '@ember/service';
import Ember from 'ember';
import { inject as controller } from '@ember/controller';
export default Service.extend({
  a: service(),
  b: service('named-b'),
  c: Ember.inject.service('named-c'),
  d: controller('projects'),
  'quoted': service()
});
""")
    assert _injects(edges) == {
        ("a", "ember:service:a", "property"),
        ("b", "ember:service:named-b", "property"),
        ("c", "ember:service:named-c", "property"),
        ("d", "ember:controller:projects", "property"),
        ("quoted", "ember:service:quoted", "property"),
    }


def test_decorator_injections_belong_to_their_class(tmp_path: Path) -> None:
    nodes, edges = _parse(tmp_path, "app/routes/probe.js", """
import Route from '@ember/routing/route';
import { service } from '@ember/service';
export default class ProbeRoute extends Route {
  @service sessionState;
  @service('other-name') alias;
  @service() third
}
""")
    assert _injects(edges) == {
        ("sessionState", "ember:service:sessionState", "decorator"),
        ("alias", "ember:service:other-name", "decorator"),
        ("third", "ember:service:third", "decorator"),
    }
    assert {e.source.rsplit("::", 1)[-1] for e in _edges(edges, "INJECTS")} == {"ProbeRoute"}
    cls = next(n for n in nodes if n.kind == "Class")
    assert cls.extra["ember_kind"] == "route"


def test_typescript_decorator_injection(tmp_path: Path) -> None:
    _, edges = _parse(tmp_path, "app/services/probe.ts", """
import Service, { service } from '@ember/service';
export default class Probe extends Service {
  @service declare router: unknown;
  @service('x-y') private other!: unknown;
}
""")
    assert {t for _, t, _ in _injects(edges)} == {"ember:service:router", "ember:service:x-y"}


def test_no_ember_import_means_no_ember_edges(tmp_path: Path) -> None:
    nodes, edges = _parse(tmp_path, "app/services/redux-like.js", """
import { createStore } from 'redux';
const store = createStore();
export function load() { store.query('items'); this.store.findRecord('post', 1); }
const o = { a: service() };
""")
    assert not [e for e in edges if e.target.startswith("ember:")]
    assert "ember_kind" not in nodes[0].extra


def test_store_calls_and_relationships_name_models(tmp_path: Path) -> None:
    _, edges = _parse(tmp_path, "app/routes/probe.js", """
import Route from '@ember/routing/route';
import Model, { attr, belongsTo, hasMany } from '@ember-data/model';
export default Route.extend({
  model(params) {
    this.store.findRecord('post', params.id);
    this.get('store').query('comment', {});
    this.store.peekAll(dynamicName);
    this.store.push({});
    other.findRecord('not-the-store', 1);
  },
  author: belongsTo('person'),
  items: hasMany('line-item')
});
""")
    refs = {e.target for e in _edges(edges, "REFERENCES") if e.target.startswith("ember:")}
    assert refs == {
        "ember:model:post", "ember:model:comment", "ember:model:person", "ember:model:line-item",
    }


def test_relationship_decorators(tmp_path: Path) -> None:
    _, edges = _parse(tmp_path, "app/models/probe.js", """
import Model, { attr, belongsTo, hasMany } from '@ember-data/model';
export default class Probe extends Model {
  @attr title;
  @belongsTo('person', { async: false }) owner;
  @hasMany('tag') tags;
}
""")
    assert {e.target for e in _edges(edges, "REFERENCES")} == {
        "ember:model:person", "ember:model:tag",
    }


def test_chained_get_receiver_is_recorded_on_the_call(tmp_path: Path) -> None:
    _, edges = _parse(tmp_path, "app/routes/probe.js", """
import Route from '@ember/routing/route';
import { get } from '@ember/object';
export default Route.extend({
  go() { this.get('queue').run(); get(this, 'other').stop(); this.get(name).dyn(); }
});
""")
    seen = {(e.extra["ember_receiver"], e.extra["ember_method"])
            for e in _edges(edges, "CALLS") if "ember_receiver" in e.extra}
    assert seen == {("queue", "run"), ("other", "stop")}


def test_router_routes_nest_and_apply_path_and_reset_namespace(tmp_path: Path) -> None:
    nodes, _ = _parse(tmp_path, "app/router.js", """
import EmberRouter from '@ember/routing/router';
const Router = EmberRouter.extend({});
Router.map(function () {
  this.route('a', function () {
    this.route('b', { path: '/:id' });
    this.route('c', { resetNamespace: true });
  });
  this.route('d', { path: dynamicPath });
  this.resource('legacy', function () { this.route('x'); });
  this.mount('engine');
});
""")
    routes = {n.extra["route_name"]: (n.extra["route"], n.extra["ember_kind"])
              for n in nodes if n.kind == "Endpoint"}
    assert routes == {
        "a": ("/a", "route"),
        "a.b": ("/a/:id", "route"),
        "c": ("/a/c", "route"),
        "d": ("/d", "route"),
        "legacy": ("/legacy", "route"),
        "legacy.x": ("/legacy/x", "route"),
        "engine": ("/engine", "mount"),
    }
    d = next(n for n in nodes if n.name == "route:d")
    assert d.extra["path_dynamic"] is True


def test_duplicate_route_names_do_not_overwrite(tmp_path: Path) -> None:
    nodes, _ = _parse(tmp_path, "app/router.js", """
import EmberRouter from '@ember/routing/router';
const Router = EmberRouter.extend({});
Router.map(function () {
  this.route('same');
  this.route('same');
});
""")
    names = [n.name for n in nodes if n.kind == "Endpoint"]
    assert len(names) == len(set(names)) == 2


def test_route_count_and_depth_are_bounded(tmp_path: Path) -> None:
    from code_review_graph import ember_extract

    body = "".join(f"this.route('r{n}');" for n in range(ember_extract.MAX_ROUTES + 50))
    nodes, _ = _parse(tmp_path, "app/router.js", f"""
import EmberRouter from '@ember/routing/router';
const Router = EmberRouter.extend({{}});
Router.map(function () {{ {body} }});
""")
    assert len([n for n in nodes if n.kind == "Endpoint"]) == ember_extract.MAX_ROUTES
    depth = ember_extract.MAX_ROUTE_DEPTH + 10
    nest = "".join(f"this.route('n{n}', function () {{" for n in range(depth)) + "}); " * depth
    nodes, _ = _parse(tmp_path, "other/app/router.js", f"""
import EmberRouter from '@ember/routing/router';
Router.map(function () {{ {nest} }});
""")
    assert len([n for n in nodes if n.kind == "Endpoint"]) <= ember_extract.MAX_ROUTE_DEPTH + 1


def test_roles_come_from_the_path_then_the_base_class(tmp_path: Path) -> None:
    cases = {
        "app/services/a.js": ("import Service from '@ember/service'; export default Service.extend({});", "service"),
        "app/posts/route.js": ("import Route from '@ember/routing/route'; export default Route.extend({});", "route"),
        "app/components/x/component.js": ("import Component from '@glimmer/component'; export default class X extends Component {}", "component"),
        "app/mixins/m.js": ("import Mixin from '@ember/object/mixin'; export default Mixin.create({});", "mixin"),
        # No path convention: the base class decides.
        "app/utils/odd.js": ("import Service from '@ember/service'; export default Service.extend({});", "service"),
        # The path wins over the base class.
        "app/helpers/h.js": ("import Service from '@ember/service'; export default Service.extend({});", "helper"),
        # A test directory never makes a role.
        "tests/unit/services/a-test.js": ("import { module } from 'qunit'; import Service from '@ember/service'; module('x');", None),
    }
    for rel, (text, role) in cases.items():
        nodes, _ = _parse(tmp_path, rel, text)
        assert nodes[0].extra.get("ember_kind") == role, rel


def test_values_in_the_module_never_enter_the_graph(tmp_path: Path) -> None:
    nodes, edges = _parse(tmp_path, "app/services/probe.js", """
import Service, { inject as service } from '@ember/service';
export default Service.extend({
  token: 'sk-live-very-secret-token',
  url: 'https://internal.example/secret-path',
  queue: service('retry-queue'),
  go() { return this.store.findRecord('post', 'secret-id-123'); }
});
""")
    blob = repr([(n.name, n.extra) for n in nodes]) + repr([(e.target, e.extra) for e in edges])
    assert "very-secret" not in blob and "internal.example" not in blob
    assert "secret-id-123" not in blob


def test_hbs_edges_carry_names_only(tmp_path: Path) -> None:
    nodes, edges = _parse(tmp_path, "app/templates/p.hbs",
                          '<FooBar @title="leaky-label" class="leaky-class"/>{{x-y "leaky-arg"}}')
    blob = repr([(n.name, n.extra) for n in nodes]) + repr([(e.target, e.extra) for e in edges])
    assert "leaky" not in blob
    assert {e.target for e in edges} == {"ember:component:foo-bar", "ember:invocable:x-y"}
    assert nodes[0].language == "handlebars"
    assert nodes[0].extra["ember_kind"] == "template"
