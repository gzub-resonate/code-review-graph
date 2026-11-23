# Ember.js support

Ember connects modules by naming convention, not by imports: `@service store` finds
`app/services/store.js`, `this.store.findRecord('post')` finds `app/models/post.js`, and
`{{task-card}}` in a template finds `app/components/task-card.js`. A graph built from imports alone
sees none of that. With this support those connections become edges, so `callers_of`,
`importers_of` and impact analysis follow them.

It is on whenever a repository has Ember evidence (a module that imports from `@ember/*`, `ember`,
`ember-data`, `@ember-data/*` or `@glimmer/component`). Nothing needs configuring. A JavaScript
repository without that evidence is not touched.

## What is indexed

| Source | Graph |
|---|---|
| `@service foo`, `@service('name') foo`, `foo: service()`, `foo: service('name')`, `Ember.inject.service()`, `inject.service()`, and the controller forms | `INJECTS` edge from the class (Octane) or module (classic) to the service or controller file. A module that defines exactly one class points at the class, otherwise at the file |
| `this.foo.bar()`, `this.get('foo').bar()`, `get(this, 'foo').bar()` | `CALLS` to method `bar` of the injected service, marked `extra.ember_resolved` (`INFERRED`) |
| `store.findRecord('post')`, `findAll`, `query`, `queryRecord`, `peekAll`, `peekRecord`, `createRecord`, `unloadAll`, and `this.get('store')` forms | `REFERENCES` to the model |
| `belongsTo('post')`, `hasMany('post')`, `@belongsTo`, `@hasMany`, `DS.belongsTo` | `REFERENCES` to the model, with `extra.ember_relationship` |
| `.hbs` templates (language `handlebars`) | `IMPORTS_FROM` from the template to each component, helper or modifier it invokes |
| a component class and its template | `IMPORTS_FROM` from the class to the template (classic `templates/components/x.hbs`, co-located `x.hbs`, pods `template.hbs`) |
| `router.js` | one `Endpoint` per route, with `HANDLES` edges from the route, controller and template modules |
| `import x from '<app>/services/x'` | the `IMPORTS_FROM` edge now names the file |
| `export { default } from '<addon>/services/x'` under `app/` | an `IMPORTS_FROM` edge, followed when an injection lands on that file |
| every module | `extra.ember_kind` on the File and Class nodes |

Layouts understood: classic (`app/services/x.js`), pods (`app/x/service.js`, `app/x/route.js`,
`app/components/x/component.js`), addon (`addon/...`, with the `app/` re-export followed) and a dummy
app (`tests/dummy/app`). `node_modules`, `bower_components` and `dist` are never read: the build
already leaves them out.

## Using it

```bash
code-review-graph build
code-review-graph query callers_of app/services/audit-log.js::record   # who calls the service method
```

In an assistant, `query_graph_tool(pattern="callers_of", target="record")` finds the calls made
through `this.get('auditLog').record()`, and `importers_of` on a component file lists the templates
that invoke it.

## How names are resolved

1. The lookup name is the explicit name (`service('retry-queue')`) or the property name, dasherized:
   `retryQueue` is `retry-queue`. Template names are dasherized the same way (`<Foo::Bar>` is
   `foo/bar`).
2. The module is searched in the consumer's own app root first (an app overrides the addon it
   merges with), then in every other root.
3. A file that only re-exports another module counts as that module.
4. Exactly one distinct module binds. Several do not: the edge stays unresolved with
   `ember_reason: "ambiguous"`. None records `"no_module"`, which is the usual outcome for built-in
   or addon services (`store`, `router`, `intl`) and for helpers from addons.
5. `<app>/...` imports bind only when at least two distinct imports under the same prefix name files
   in the repository, so `lodash/get` is never taken for `app/get.js`.
6. In a template `{{foo-bar}}` can be a component or a helper. When both exist, the edge stays
   unresolved (`ambiguous`) rather than guess.

Every pass restarts from the original placeholder (`ember:<kind>:<name>`), so adding or deleting a
module changes the answer on the next `update`.

## What it does not store or run

Only identifiers are read: property, service, model, route and component names, and the literal
that is itself a name (`service('retry')`, `this.route('posts', { path: '/p' })`). Template text,
attribute values and other string literals are never stored, and project code is never executed.
Template scanning is bounded (1 MB per file, a fixed work budget, 2,000 distinct names), as is the
router walk (2,000 routes, 24 levels).

## Limits

- Dynamic lookups are invisible: `owner.lookup('service:' + name)`, `{{component this.which}}`,
  `store.findRecord(modelName)`, and `path:` set from a variable (kept as a route with
  `path_dynamic`).
- A service injected in a base class is not seen from a subclass.
- A method the service inherits (from a mixin or `Evented`) is not found in its file. The call keeps
  the service as its claimed endpoint and stays unresolved.
- A route table built outside `router.js` (for example a separate `routes.js` configuration) is not
  read; engines (`this.mount`) get an Endpoint but nothing else.
- Services, components and helpers that live in an addon outside the repository stay unresolved.
- Block parameters (`as |foo|`) hide that name for the whole template file, not just the block.
- Ember Data adapters and serializers are not linked to their models.
