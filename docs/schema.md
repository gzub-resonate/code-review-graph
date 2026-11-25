# Knowledge Graph Schema

The graph is one SQLite database, `.code-review-graph/graph.db`, opened in WAL mode.
The base tables and indexes come from `_SCHEMA_SQL` in `code_review_graph/graph.py`.
Everything else is added by the versioned migrations in `code_review_graph/migrations.py`.
The current schema version is 15, the highest key in `MIGRATIONS`. A database
below that is migrated on open, in order, by every migration above its own
version.

## Node Types

`nodes.kind` holds one of the values below.

### File
One row per parsed file.

| Column | Value |
|---|---|
| name | File path, as stored at build time (absolute) |
| file_path | Same as `name` |
| language | Detected language (`python`, `typescript`, `go`, ...) |
| line_start | 1 |
| line_end | Line count |
| file_hash | SHA-256 of the file bytes, used for change detection |

### Class
A class, struct, interface, enum or module definition.

| Column | Value |
|---|---|
| name | Class name |
| file_path | Containing file |
| line_start, line_end | Definition range |
| language | Source language |
| parent_name | Enclosing class, for nested classes |
| modifiers | Access modifiers (`public`, `abstract`, ...) where the grammar exposes them |

### Function
A function, method or constructor.

| Column | Value |
|---|---|
| name | Function name |
| file_path | Containing file |
| line_start, line_end | Definition range |
| language | Source language |
| parent_name | Enclosing class, for methods |
| params | Parameter list as source text |
| return_type | Return type annotation |
| signature | Signature text computed by post-processing and indexed by `nodes_fts` (added in v2) |
| is_test | 1 for test functions |

An XSLT template or `xsl:function` is a Function node too, named by its `name` or written as
`match=para mode=toc`. `extra.dynamic_apply_count` counts the `xsl:apply-templates` in it
that gave no edge, because neither a `select` step nor a `mode` narrows the choice.

### Test
Same columns as Function, with `kind = 'Test'` and `is_test = 1`. A function is a test when
any of these hold (`_is_test_function` in `parser.py`):

- Its name matches `^test_`, `^Test`, `_test$`, `_spec$`, `.test.` or `.spec.`.
- It is in a test file (`test_*.py`, `*_test.py`, `*.test.ts`, `*.spec.js`, `*_test.go`,
  `tests/`, `__tests__/`, `*Test.java`, `*Test.kt`, `*_test.dart`, R `testthat`, Julia
  `test/`, ReScript `*_test.res`) and is named like a test-runner call (`describe`, `it`,
  `test`, `beforeEach`, ...).
- It carries a test annotation: JUnit `@Test`, `@ParameterizedTest`, `@RepeatedTest`,
  `@TestFactory`, or Rust `#[test]`, `#[tokio::test]`, `#[async_std::test]`, `#[rstest]`,
  `#[proptest]`.
- It is a Ruby block in a test file, such as `it "adds" do`, `describe`, `test "..." do`,
  `before` or `let`.

### Type
A type alias, interface, enum or similar construct where the language parser emits one.
Columns as for Class.

### Endpoint
A synthesised routed entry point, emitted for Spring request mappings, WebFlux functional
routes, and static Laravel routes in `routes/*.php`. Metadata includes `http_method`, `route`,
and `handler`. Controller routes link to the handling method by a `HANDLES` edge. Methods of a
Spring Cloud OpenFeign `@FeignClient` interface emit client Endpoints instead: `role` is
`"client"`, `client` is `"feign"`, and `http_method` and `route` come from the mapping
annotations with the `@FeignClient` `path` prefix applied. Other extras are `client_method`,
`service` (the client `name`, or `service_unresolved` when it is a placeholder), `context_id`,
`route_dynamic` (a path piece could not be resolved, or the mapping is an absolute URL),
`url_literal`, and `config_keys` (`${...}` placeholders, recorded without values). A piece
of a Feign route that is not a string literal (a constant, a method call) is rendered as the marker
`<unresolved>`, and `route_literal_parts` lists the literal fragments in order, so a route is
never shown as fully literal when something was dropped. The marker is not `{...}`, so it cannot
be read as a path variable, and it has no `?`, so a query-string strip leaves it whole.
`${...}` placeholders stay verbatim in `route` and do not count as unresolved. A method with a
`java.net.URI` argument gets `url_from_argument`: Feign sends the request to that argument, so
`route` is not the target. A `REQUESTS` edge links the interface method to its
client Endpoint, never a `HANDLES` edge. Dead-code reports skip the interface. Flow detection does
not treat a method as an entry point on the strength of its mapping annotation (a method nothing
calls can still be one). Laravel
closures have an Endpoint without that edge; resource routes are not expanded yet, and a prefix
set outside the routes file (`bootstrap/app.php`, `RouteServiceProvider`) is not seen.

Known gaps in Feign support:

- Mappings inherited from a parent interface are not resolved at parse time. A marker interface
  such as `interface X extends Base {}` gets no client endpoints. A parent interface without
  `@FeignClient` that declares mapped methods is indexed like a controller, so those methods
  still get server-style `Endpoint` nodes and `HANDLES` edges.
- Query strings stay verbatim in `route` (`/items?active=true`); stripping is left to whoever
  matches routes (`cross_repo_requests_tool` strips them, and ignores a trailing slash and the
  names inside `{...}`, when it joins a call to a server endpoint in another registered repo).
- Feign's native `@RequestLine` contract and Kotlin clients are not supported.

JavaScript and TypeScript use the same node shape, written by `js_api_extract.py`. A client
`Endpoint` has `role: "client"`, `client: "js_http"`, `origin: "js"`, `client_kind` (`axios`,
`fetch`, `jquery`, `ember-ajax`, `ember-data`), `http_method`, `route` and `route_dynamic`;
a call that cannot be joined also has `method_dynamic` or `absolute_url`, and `base_url`,
`base_url_dynamic`, `instance`, `receiver_inferred` or `inferred` say how sure the route is.
A URL that is not statically known is `<unresolved>` with `route_dynamic: true`. A server
`Endpoint` has `framework` (`nextjs-app`, `nextjs-pages`, `express`), `origin: "js"` and, for a
rest-of-path route, `catch_all` (and `optional_catch_all`). Ember Data `REQUESTS` edges carry
`confidence: 0.6`, `confidence_tier: "INFERRED"`. `{param}` is the placeholder for a `${expr}`
piece of a call, `{...name}` marks a catch-all piece of a served route. An inline handler is a
Function node with `extra.inline_handler`, named `handler@L<line>C<column>`; its route's `HANDLES`
edge starts there.
- `requests_of` and `requesters_of` take a method or an endpoint qualified name. A class name
  returns nothing, as it does for `endpoints_for` and `handlers_of`.

### Ember roles
`ember_kind` in a File or Class node's `extra` names the role of an Ember module: `service`,
`route`, `controller`, `component`, `model`, `adapter`, `serializer`, `helper`, `modifier`,
`mixin`, `transform` or `template`. The module path decides first (`app/services/x.js`,
`addon/...`, pods such as `app/posts/route.js`, a dummy app under `tests/dummy/app`); the base
class (`Service.extend`, `extends Component`, imported from an Ember module) decides only where
the path says nothing. Files under a test directory never get a role. `.hbs` files are language
`handlebars`.

For an Ember application, `router.js` gives one Endpoint per `this.route(...)` (`this.resource`
and `this.mount` too), named `route:<full name>`. `extra` holds `framework: "ember"`,
`ember_kind` (`route` or `mount`), `route_name` (the dotted name, honouring `resetNamespace`),
`route` (the URL, honouring `path:`), and `path_override`, `path_dynamic` or `reset_namespace`
when they apply. Route, controller and template modules link to it by `HANDLES` edges
(`ember_resolver.py`); see [EMBER.md](EMBER.md).

### Scheduler
A synthesised node for a `@Scheduled` method. Linked to the method it fires by a `TRIGGERS`
edge.

### ConfigProperty
A configuration key parsed from Spring `application.properties` or `application.yml`.
Values are discarded; only the key is stored. Linked to the code that binds it by a
`DEPENDS_ON_CONFIG` edge.

### Event
A synthesised node for a Spring application event, created after the build by
`event_resolver.py` from `PUBLISHES` and `HANDLES` edges. Its `file_path` is the placeholder
`event` and `extra` carries `{"event_type": ..., "virtual": true}`.

### Dockerfile stage
A build stage in a `Dockerfile`, `Dockerfile.*`, `*.Dockerfile` or `Containerfile` is a `Class` node named `stage:<alias>`, or `stage:<index>` when it has no alias. `extra` holds `dockerfile_type: "stage"`, `stage_index`, `stage_name` and `base_image` (the `FROM` image as `name[:tag][@digest]`, without credentials or query; `None` when it uses a build argument). Only `FROM` and `COPY --from` are read; `ARG`, `ENV` and `LABEL` values never enter the graph.

### YamlPath
A path-addressable mapping key or sequence shape parsed from generic `.yml` / `.yaml` files. Repeated sequence entries are aggregated under `[*]` paths with bounded occurrence/range samples, allowing similarly shaped files to be compared without one graph node per list item. Scalar values are deliberately discarded.

The persisted `extra` JSON is the internal source of truth. Query and change-analysis responses expose its safe public subset under one nested `yaml` object:

| Property | Type | Description |
|----------|------|-------------|
| schema_path | string | Aggregated structural path, such as `$.services[*].metadata.tier` |
| example_path | string | One concrete occurrence used as a navigation example |
| value_type / value_types | string / string[] | Structural or scalar YAML tag types; never scalar values |
| document_index | int | Zero-based YAML document index |
| occurrence_count | int | Number of occurrences represented by this node |
| line_samples | int[] | Bounded occurrence-line samples |
| duplicate_key / duplicate_group_count | bool / int | Mapping-scoped duplicate-key diagnostics |
| is_alias / anchor_definition / yaml_merge | bool | Alias, anchor, and merge relationship metadata |

`CONTAINS` connects files and nested paths. Alias nodes use `REFERENCES` with canonical qualified endpoints. A workflow or composite action file also lists its `uses` references in `extra.github_actions_uses` (see [INFRASTRUCTURE_REFERENCES.md](INFRASTRUCTURE_REFERENCES.md)); these are the only YAML values stored. Generic YAML `File` responses use the same `yaml` object for document, duplicate-key, unsupported-key, truncation, and `values_indexed: false` metadata.

### DocSection
A `div`, `section` or `article` with an `id` in an HTML page (`.html`, `.htm`). Its name is the
anchor, such as `#cover`, and its lines are the element's. `extra` holds `html_id`, `html_tag`,
`html_id_occurrences` (a page can repeat an id; the first element is the one kept) and, when the
block has one, its first heading under `docstring`. Search finds a section by its anchor and that
heading. The body text is not indexed. `CONTAINS` connects the file to each section.

### Variable and Output
Nodes from Liquid templates (`.liquid`). An `assign` gives a `Variable` node named by the
variable, and each `{{ ... }}` gives an `Output` node named `output:` plus what it prints. Names
hold no line number, so a name used twice is one node, and `extra.lines` lists every line.
`CONTAINS` connects the file to each one. Search finds `Output` nodes only with `kind="Output"`.

### Variable (Python URL constants)
A top-level `UPPER_SNAKE` constant in a Python file whose value is an `http(s)` URL, or a set,
list, tuple, `frozenset(...)` or dict of them, such as `ALLOWED_REFRESH_URLS`. The `Variable` node
has `extra.constant`, `extra.url_values` (at most 20, each at most 300 characters),
`extra.value_count` and `extra.truncated`. A name that looks like a secret (`SECRET`, `PASSWORD`,
`TOKEN`, `API_KEY`, `CREDENTIAL`, `PRIVATE`), a URL with credentials or a query string, and any
other value are left out. `CONTAINS` connects the file to the constant, and a `REFERENCES` edge
(`extra.resolution = "module_constant"`) connects each function that names it, unless the function
takes or assigns the same name itself. Test files are skipped.

### JsonPath
A key or array shape parsed from a `.json` file (not a `*.asl.json` state machine, see Step Functions state machines below), in the same value-free shape as `YamlPath`: array items share one `[*]` path, an object with more than 200 keys is a table whose keys share one `.*` path, and values never enter the graph. Comments and trailing commas are accepted. Responses expose the same fields as `YamlPath`, without `document_index` or the YAML alias fields, under a nested `json` object.

Some JSON files keep only their `File` node, and its `json` object says why:

| Property | Files |
|----------|-------|
| locale_file | Translation files, by the same rule as YAML: a `locale`, `locales`, `i18n`, `l10n` or `translations` folder and a language code in the file or folder name |
| data_file | Test files and files under `docs`, `examples`, `samples`, `demo`, `data`, `fixtures`, `mocks`, `snapshots` or `testdata` folders |
| truncated | Files over 256 KB. A file cut at 1,000 paths or 32 levels keeps the paths before the cut and carries the same mark |

Lock files (`package-lock.json`, `npm-shrinkwrap.json`, `*.lock.json`, `project.assets.json`) and `*.min.json` are ignored by default. Search ranks JSON files and paths below code unless the query asks for them with `kind="JsonPath"`, a `$` path or a `.json` file name.

### Step Functions state machines
A file named `*.asl.json` or `*.asl` is an Amazon States Language definition and is read as a
state machine, never as generic JSON (language `asl`, no `JsonPath` nodes). Its `File` node has
`extra.config_format = "asl"`, `values_indexed: false` and `asl_problem`, which is `null`, or
`invalid_json`, `too_large` (over 1 MiB), `not_a_state_machine` (no `States` object) or
`truncated` (over 2,000 states, 8 levels of nested Parallel or Map, or a list or object over its
cap; the states read before the cut are kept). A definition with `${key}` template variables
outside quotes, which is not valid JSON until it is rendered, is read with those variables
quoted. A file marked `invalid_json`, `too_large` or `not_a_state_machine` keeps no state.

| Node | Kind | Name | `extra` |
|---|---|---|---|
| State machine | `Class` | The file name without `.asl.json` or `.asl` | `state_count`, `unresolved_transitions` (a `Next`, `Default`, `Choices` or `Catch` that names no state), `query_language` (`JSONPath` or `JSONata`, when the file sets it), `duplicate_keys` |
| State | `Function` | The state's name | `state_type` (`Task`, `Choice`, `Parallel`, `Map`, `Wait`, `Pass`, `Succeed`, `Fail`, or `Unknown`), `is_start`, `end` (true for `End: true`, `Succeed` and `Fail`), `has_retry` and `has_catch` (true when the state has a non-empty `Retry` or `Catch`; booleans only, never the parameters), `query_language` (a state that sets its own), and for a Task `resource_kind`, `resource_action`, `target_kind`, `target_dynamic` and `target_placeholders` |

A state's qualified name is `<file>::<machine>/<path>`. The path is the state name, preceded by
the enclosing state and the branch for a nested one: `orders/Fan Out[0]/Reserve Stock` for the
first Parallel branch of `Fan Out`, `orders/Process Items[map]/Price Item` for a Map processor.
`/`, `[`, `]` and `%` in a state name are written `%2F`, `%5B`, `%5D` and `%25`, so the same name
in two branches never collides. A state has no parent class and no `CALLS` edge: nothing calls a
state, so dead-code detection skips every `asl` node.

`resource_kind` is the service in an optimized integration (`lambda`, `states`, `ecs`, `sns`,
`sqs`, `dynamodb`, `batch`, `glue`, `codebuild`, `elasticmapreduce`, ..., or `aws-sdk:<service>`),
`lambda` for a Lambda function ARN used directly as the `Resource`, and `dynamic` for a
`${placeholder}` `Resource`. `resource_action` is the integration's action, such as
`startExecution.sync:2`. `target_kind` is `task-definition` or `cluster` for an ECS Task, which
can name either.

`REFERENCES` from a Task goes to `external:aws-<service>:<name>` (`aws-lambda`, `aws-states`,
`aws-ecs`, `aws-sns`, `aws-sqs`, `aws-dynamodb`, `aws-batch`, `aws-glue`, `aws-codebuild`) when
the name is a plain literal. The name is read from the ARN, the Lambda `FunctionName`, the
`StateMachineArn`, the ECS `TaskDefinition` (else `Cluster`), the `TopicArn`, the `QueueUrl`,
the `TableName`, the `JobDefinition`, the `JobName` or the `ProjectName`; the partition,
region, account ID, alias, version and revision are dropped. When the name is a `${key}` template
variable, an intrinsic function (`States.Format(...)`), a JSONPath (`Field.$`) or a JSONata
expression, there is no edge: the state gets `target_dynamic: true` and, for `${key}`, the key
names in `target_placeholders`. No other value from `Parameters`, `Arguments`, `InputPath`,
`OutputPath`, `ResultPath`, `Choices`, `Comment`, `Error` or `Cause` is read into the graph.
These targets name no node, so `target_resolution` is `unresolved` for them by construction.

When the same repository defines the Lambda function or ECS task definition in Terraform, a second
`REFERENCES` edge runs from the Task state to the `aws_lambda_function` or `aws_ecs_task_definition`
resource node (`asl_resolver.py`). The graph keeps values out, so this is a naming convention: the name
and the resource label are compared ignoring case and `-` / `_` (`order-validator` and
`order_validator`), and only when exactly one resource of that type has the label. The edge is
`INFERRED` at 0.6, with `extra.resolution: "asl_name_convention"` and `extra.external_target`
holding the external target it stands for. The external edge stays, so a function defined in another
repository, or one whose name does not match, keeps it. Every pass removes these links and makes
them again, so an update gives what a full build does.

Two more derived `REFERENCES` edges come from the Terragrunt unit that deploys the machine (the
allowlisted `inputs` keys in `docs/INFRASTRUCTURE_REFERENCES.md`, "Terragrunt inputs"):

- `extra.resolution: "asl_replacement_vars"`: a Task state whose target is a `${key}` placeholder
  gets an edge to `external:aws-<service>:<name>` when a unit whose `step_function_file_path`
  names this definition fills `key` from a replacement map. The name was read, so
  `extra.confidence_tier` is `EXTRACTED` (the edge's own `confidence_tier` column follows the
  rule for every target that names no node, and is `UNRESOLVED` like the parser's literal
  external edges); `extra.placeholder` is the key, with `resource_kind` and `target_kind`. A key that
  stands for different names in different units (one per environment) gets one edge per name. The
  state keeps `target_dynamic` and `target_placeholders`, which describe the definition file.
- `extra.resolution: "asl_lambda_handler"`: a state with an edge to `external:aws-lambda:<name>`
  gets an edge to the handler `Function` node of the Lambda unit deployed as `<name>`: the
  function `function` of `<source_dir>/<module>` (`.py`, `.js`, `.mjs`, `.cjs`, `.ts`) for a
  `handler` of `module.function`. It is `EXTRACTED` when the unit's `function_name` and
  `git_repo_sub_dir` were both evaluated statically. Otherwise a directory-name convention stands
  in (the directory above the unit names the function, and the module is looked for in a
  directory named after `function_name`, else after that directory) and the edge is `INFERRED`
  at 0.6. Units that disagree on the handler of one name give no edge, and Java
  `Class::method` handlers are not linked. `extra.external_target` holds the Lambda target.

Impact analysis walks `REFERENCES` backwards, so a changed handler reaches the states and the
machines that run it. Execution flows follow all three derived edges out of a state, so a flow
that starts at a machine continues into the handler's calls.

Machines and states keep the `Class` and `Function` kinds: a new kind would need every filter on
`kind` (impact radius, risk scoring, dead code, communities, flows, search) to learn it, and the
schema allows none without a migration. The role is in `extra` instead: `asl_role`
(`state_machine` or `state`), and for a state `state_machine`, `state_path` (`Fan Out[0]/Ship`),
`is_machine_start` (the machine's own `StartAt`, not a branch's) and `display_name`
(`<machine>/<path>`; `statemachine <machine>` for the machine). `docstring` names the machine and is
indexed, so a search for a state name finds the machine it is in. Responses add `display_name` for
`asl` nodes.

Flows: the `StartAt` state of each machine is a flow entry point, named by the machine, and the
flow follows `TRANSITIONS_TO` and the inferred Task-to-Terraform `REFERENCES` link. No other state
is an entry point. These edges are added to the flow's in-memory adjacency only; the store keeps
no `CALLS` edge for a state.

`CONTAINS` joins the file to the machine, the machine to its top-level states, and a Parallel or
Map state to the states of its branches and processor.

## Edge Types

Every edge has `source_qualified`, `target_qualified`, `file_path` (where the relationship
was seen), `line`, `extra` (JSON), `confidence`, `confidence_tier` and, for
`CALLS` and `REFERENCES`, `target_resolution`.

For `CALLS`, `REFERENCES` and `TESTED_BY`, the tier says how the endpoint meant to
name a node was bound, and is set after every resolver has run:

| Tier | Score | Means |
|---|---|---|
| `EXTRACTED` | 1.0 | The source names a symbol and the node exists |
| `INFERRED` | 0.8, or the resolver's own score | A resolver chose the node from other evidence |
| `AMBIGUOUS` | 0.4 | Several nodes fit; `extra.ambiguous_targets` lists them |
| `UNRESOLVED` | 0.2 | No node has this name, so it may be external or simply unknown |

No edge keeps `<path>::<symbol>` when no node has that name. The endpoint goes back to the
plain name, and `extra.claimed_endpoint` keeps the qualified form, so the edge returns to it
if that node appears. Until then, a name match on that edge counts only for a method of the
class the qualified form named.

A call to a method a class inherits goes to the ancestor that declares it. When the parser
wrote `<path>::Leaf.handle` and only `Alpha` declares `handle`, the target becomes
`Alpha.handle` and `extra.inherited_endpoint` keeps `<path>::Leaf.handle`, so every pass
checks the move again. When the bare-name resolver binds a call this way, `extra.inherited_through`
names the class the call went through. Both are `INFERRED`.

A plain Java call with no receiver, `helper()`, is read as Java reads it: the calling class
first, then what that class inherits. The nearest class that declares `helper` answers, so a
test class calling `givenAuthenticated()` from an abstract base in another package reaches
`BaseApiTest.givenAuthenticated`. Overloads of that name stay `AMBIGUOUS`. A class that
extends a type outside the graph, or that nothing in its chain declares the name for, is left
to the usual evidence, because the call may be a static import or an enclosing class's method.

Scala calls and imports are bound across files by `scala_resolver.py`, from the package
clauses, imports and declared types the parser recorded. A bound call is `INFERRED` and
carries `extra.scala_resolved` (the rule: `object_member`, `constructor`, `apply`,
`case_class_constructor`, `typed_receiver`, `this_member`, `super_member`, `enclosing_member`,
`imported_member` or `wildcard_member`), `extra.scala_via` (the declaring type or types) and
`extra.scala_name` (the name as written, which restores the edge when its target goes away). A
call several declarations could mean (overloads, an object defined twice, a case class with
an explicit `apply`) stays `AMBIGUOUS`: `extra.scala_ambiguous` names the rule and
`extra.ambiguous_targets` lists them. A name that is not declared in the repository is never
linked. The parser also leaves what the resolver reads: on a `Class` node `extra.scala_kind`
(`class`, `trait`, `object`, `enum` or the `case_` form), `scala_package`,
`scala_package_scopes` (when package clauses are chained or braced) and `scala_parents` (the
`extends` clause as written, `?` for a parent that is no plain name); on a `Function` node
`scala_private` and `scala_override`; on an `IMPORTS_FROM` edge `scala_import` (the dotted
name), `scala_alias` (a rename), `scala_wildcard` and `scala_given`; on a `CALLS` edge
`scala_form` (`bare`, `member`, `new` or `other`), `scala_receiver`, `scala_receiver_type`,
`scala_receiver_new`, `scala_receiver_apply`, `scala_receiver_expr_new`,
`scala_receiver_local`, `scala_shadowed`, `scala_type` and `scala_argc`. An `IMPORTS_FROM`
edge whose import names a type or an object (or one of its members) in the repository points
at that file and has `extra.import_resolution = "scala_scope"`; an import of a whole package
keeps its dotted name, because a package has no single file.

| Kind | Source -> target | Notes |
|---|---|---|
| CALLS | caller -> called function | A call through an injected Ember service property carries `extra.ember_resolved`, `ember_property` and `ember_method` (`ember_receiver` for `this.get('x').m()`); a method the service file does not define keeps the service as its claimed endpoint. Target may be a bare name until a resolver qualifies it; `target_resolution` records which. Spring AOP advice gets a call to each method its pointcut matches, with `extra.pointcut_kind` and `extra.advice_kind` (`aop_resolver.py`). A call through an injected interface with several implementations is bound to one class and records `extra.resolution` (`spring_qualifier`, `spring_primary` or `spring_bean_name`) plus the `extra.qualifier` or `extra.bean_name` it used; an undecidable call stays on the interface method |
| IMPORTS_FROM | importing file -> imported module, file or package directory | `file_path` equals the source. `extra.import_scope` marks a DIRECTORY target: `package` (a Go import names a directory of files) or `tree` (a Ruby `require_all` names everything below one). The read path expands a directory to its members; see `import_scope_ancestors` in `graph.py`. `extra.import_context` marks an import that does not run as the file loads: `type_only` (never runs), `deferred` (runs late, inside a function) or `parent_package` (Python `from . import sibling`: the edge to the file's own package). An HTML `<script src>`, a Liquid `render` or `include` and an XSLT `xsl:import` or `xsl:include` are imports too. In an Ember app a template's component, helper or modifier invocation is an import (`extra.ember_ref` is `component`, `invocable` for a name that may be either, `helper` or `modifier`; `ember_syntax`; `occurrences` when repeated), a component class imports its template (`extra.ember_link: "colocated_template"`), an import written as `<app>/services/x` is bound to its file (`extra.ember_import_spec` keeps the original), and an addon's `export { default } from '...'` under `app/` is an import (`extra.ember_reexport`) |
| IMPORTS_FROM (infrastructure) | file -> `external:github-actions:<owner/repo[/path]>`, `external:docker-image:<name>`, `external:terraform-module:<address>` | Workflow `uses`, Dockerfile `FROM` and `COPY --from`, Terragrunt `source`, `include`, `dependency` and `dependencies`, and Terraform `module` sources. Terragrunt `extra` also holds `resolved_statically` and, for a `dynamic` reference, `dynamic_reason` (`env`, `command`, `variable`, `unsupported_function`, `depth_limit` or `not_found`). Not a node: the target is a name outside the repository, and the version, tag or ref is in `extra`. `dynamic` targets (`external:github-actions:dynamic`, `external:docker-image:dynamic`, `external:terragrunt:dynamic`, `external:terraform-module:dynamic`) hold an expression or a reference that could not be resolved or reduced safely; no text is stored. URL credentials, fragments and query strings other than `ref` and `version` are never stored. See [INFRASTRUCTURE_REFERENCES.md](INFRASTRUCTURE_REFERENCES.md) |
| INHERITS | child class -> parent class | |
| IMPLEMENTS | implementing class -> interface | |
| CONTAINS | file -> class or function; class -> method | Structural containment |
| TESTED_BY | function -> test function | |
| REFERENCES | node -> symbol used as a value | Callback maps, arrays, assignment. An Ember `store.findRecord('post')`, `belongsTo('post')` or `hasMany('post')` references the model: `extra.ember_ref` is `model`, with `ember_store_method` or `ember_relationship`. A Step Functions Task state points at `external:aws-<service>:<name>` for what it calls |
| DEPENDS_ON | general dependency | Ansible role `meta` dependencies, Solidity `using` directives, Dockerfile stage to the earlier stage it starts from or copies from |
| INJECTS | Spring bean -> injected field or constructor parameter type (`extra.qualifier` holds a literal `@Qualifier` value; class nodes carry `spring_bean_name` and `spring_qualifiers`); Ember class or module -> injected service or controller | Spring enrichment; Ember `extra`: `ember_inject` (`service`, `controller`), `ember_name`, `ember_explicit`, `field_name`, `ember_spelling` (`decorator`, `property`), then `ember_resolution` (`bound`, `unresolved`) and `ember_reason` (`no_module`, `ambiguous`). An unbound target stays `ember:<kind>:<name>` |
| TRANSITIONS_TO | Step Functions state -> the state that runs next, or a Parallel or Map state -> the first state of a branch or processor | `extra.via` is `next`, `default`, `choice`, `catch`, `branch` or `map`; `extra.index` is the position in `Choices`, `Catch` or `Branches`; a `catch` edge also has `extra.error_equals`, the first five `ErrorEquals` names that are `States.*` or plain identifiers of at most 64 characters (never `Cause` text); the `default` edge has `extra.default: true`. Choice conditions, variables and values are never stored. `query_graph_tool` `next_states_of` / `previous_states_of` repeat `via`, `index`, `error_equals` and `default` on each edge row, and add a Choice rule's test (`condition`, or `conditions` when several rules lead to one state) read from the file when the query runs, never from the graph. The file must lie inside the repository and hash to what was indexed; otherwise the row has `condition_unavailable` and no test. A Parallel or Map state's own `Next` and `Catch` carry the flow after its branches finish |
| CONSUMES | `@KafkaListener` / `@KafkaHandler` method -> `kafka:<topic>` | Spring Kafka enrichment |
| PRODUCES | Kafka producer -> `kafka:<topic>` | Spring Kafka enrichment |
| TEMPORAL_STUB | class -> declared Temporal workflow or activity interface of a stub field | Used by `temporal_resolver.py` to resolve calls made through the stub |
| DEPENDS_ON_CONFIG | `@ConfigurationProperties` class -> `ConfigProperty` | Spring enrichment |
| HANDLES | handler -> `Endpoint`; listener -> event | Spring and Laravel enrichment. Ember: route, controller and template module -> route `Endpoint`, `extra.ember_role` says which |
| REQUESTS | `@FeignClient` interface method -> client `Endpoint` | Spring Cloud OpenFeign enrichment; read with `requests_of` / `requesters_of` |
| TRIGGERS | `Scheduler` -> `@Scheduled` method | Spring enrichment |
| PUBLISHES | method -> Spring application event | Spring enrichment |

`OVERRIDES` has an impact weight in `constants.py` but no parser emits it.

## Qualified Name Format

```text
/absolute/path/to/file.py                                      # File
/absolute/path/to/file.py::function_name                       # top-level function
/absolute/path/to/file.py::ClassName.method_name               # method
/absolute/path/to/file.py::OuterClass.InnerClass.method        # nested class method
/absolute/path/to/Handlers.cs::App.Report.ExportHandler.Run    # C#: namespace, then types
/absolute/path/to/card.liquid::output:product.title            # Liquid output
/absolute/path/to/file.yml::yaml:0:$.services[*].metadata.tier # aggregated YAML path in document 0
/absolute/path/to/file.yml::yaml:0:alias:$.service["<<"]@12    # alias or anchor identity
/absolute/path/to/package.json::json:$.scripts.build            # JSON path
/absolute/path/to/orders.asl.json::orders/Fan Out[0]/Reserve Stock # Step Functions state
/absolute/path/to/routes.ts::handle:S1a2b3c4d5e6f              # second same-named definition in a file
```

### Same-named definitions in one file

Two definitions in one file can get the same name, such as two object-literal
methods called `handle`. The first keeps the plain name. Each other one gets
`:S<hash>`: the first 12 hex characters of a SHA-256 of its kind, name, parent,
language, parameters, return type, modifiers and test flag. Line numbers are left
out, so the name stays the same when code moves. If two are the same in all of
those, the later ones also get `:2`, `:3` and so on.

On a re-index each definition keeps its old name, matched first by line and then
by those fields, so its edges stay with it. A call from another file that names
only the plain name does not say which one it means, so it lists them all as
ambiguous. C++ overloads carry their parameter types instead, as in
`::Workspace.remove(DataFile*,bool)`.

`nodes.symbol` stores the part after the first `::` (or the whole name when there is none)
so that dotted-tail lookups are an indexed equality test.

## SQLite Tables

Base tables from `graph.py`. Columns marked with a version are added by that migration on
existing databases.

```sql
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    symbol TEXT,                 -- v10
    docstring TEXT,              -- v13
    name_tokens TEXT,            -- v13
    updated_at REAL NOT NULL,
    signature TEXT,              -- v2
    community_id INTEGER         -- v4
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,              -- v9
    confidence_tier TEXT DEFAULT 'EXTRACTED', -- v9; see "Edge Types"
    target_resolution TEXT,                   -- v11; 'direct' | 'unresolved' | NULL
    updated_at REAL NOT NULL
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

The `endpoint_index` key holds a JSON document (`v`, `stamp`, `clients`, `servers`) with the repository's HTTP client
calls and server endpoints, written by every build and by an update that changed the graph (`endpoint_index.py`).
`stamp` digests the `Endpoint` rows it was made from; a reader whose stamp differs, or that does not know `v`, ignores
the key and reads the nodes. A graph with no `Endpoint` node has no such key.

Tables added by migrations:

```sql
-- v3
CREATE TABLE flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entry_point_id INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    node_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    criticality REAL NOT NULL DEFAULT 0.0,
    path_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE flow_memberships (
    flow_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (flow_id, node_id)
);

-- v4
CREATE TABLE communities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER,
    cohesion REAL NOT NULL DEFAULT 0.0,
    size INTEGER NOT NULL DEFAULT 0,
    dominant_language TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v5, widened by v13 (rebuilt by search.rebuild_fts_index from the same
-- migrations.NODES_FTS_DDL, so the two cannot drift)
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, qualified_name, file_path, signature, docstring, name_tokens,
    content='nodes', content_rowid='rowid',
    tokenize='porter unicode61'
);

-- v12, widened by v13 to mirror every nodes_fts column
CREATE TABLE nodes_fts_state (
    node_id INTEGER PRIMARY KEY,
    name TEXT,
    qualified_name TEXT,
    file_path TEXT,
    signature TEXT,
    docstring TEXT,      -- v13
    name_tokens TEXT     -- v13
);
CREATE INDEX idx_nodes_fts_state_file ON nodes_fts_state(file_path);
```

`nodes_fts` is an external content table: it holds the inverted index but reads column
values from `nodes`. Removing one of its entries therefore needs the values that were
indexed, and those are gone once the node row is deleted. `nodes_fts_state` mirrors what
the index currently holds so `search.update_fts_index` can rewrite just the rows an
update touched instead of dropping and repopulating the whole index. It starts empty
after the migration; the first index sync fills it with one full rebuild. Its columns
have to be exactly `migrations.NODES_FTS_COLUMNS`, because an external-content delete
replays every indexed value; the `fts_state_synced` metadata key records the mirror
shape, and a mirror written under an older value forces one rebuild.

### Embeddings

`EmbeddingStore` in `code_review_graph/embeddings.py` creates this table in the same
`graph.db` when embeddings are first generated. It is not part of the migration chain; a
missing `provider` column is added when the store opens.

```sql
CREATE TABLE embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,            -- float32 array
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);
```

### Metadata keys

| Key | Set by |
|---|---|
| `schema_version` | `migrations.py`; `15` on a current database |
| `fts_state_synced` | `search.rebuild_fts_index`; mirror-shape version |
| `last_updated` | Full and incremental builds |
| `last_build_type` | Full and incremental builds |
| `git_head_sha`, `git_branch` | Builds in a git checkout |
| `svn_branch`, `svn_revision` | Builds in an SVN working copy |
| `postprocess_level` | Post-processing |
| `community_detector` | Community detection: `leiden`, `file`, or both joined by `+` |
| `last_postprocessed_at` | Post-processing; when it last finished |
| `repo_root` | Builds; the repository the graph belongs to |
| `build_state` | Builds and post-processing: `in-progress`, `postprocess-pending` or `complete`; `status` reads it to report an unfinished build |
| `verified_file_count` | Builds; how many files a complete graph holds (`0` until checked) |
| `intentionally_incomplete` | `forget` sets `1`; a full build sets `0` |
| `symlink_followed_files`, `symlink_followed_dirs` | Builds that follow symlinked folders (`--follow-symlinks` or `CRG_FOLLOW_SYMLINKS`): files and outside folders reached through a link |
| `dependents_reparse_pending` | Incremental updates; importers whose re-parse failed, to retry. A full build clears it |
| `cpp_identity_version`, `cpp_identity_pending`, `csharp_identity_version`, `csharp_identity_pending_files`, `nested_scope_identity_version`, `nested_scope_identity_pending_files` | Builds and updates; force one re-parse when node names change shape |
| `csharp_flows_dirty`, `scala_flows_dirty` | C# and Scala call resolution set `1` when they rebind calls in files that were not reparsed; rebuilding flows clears both |

### Indexes

| Index | Columns | Added |
|---|---|---|
| `idx_nodes_file` | `nodes(file_path)` | base |
| `idx_nodes_kind` | `nodes(kind)` | base |
| `idx_nodes_name` | `nodes(name)` | base |
| `idx_edges_kind` | `edges(kind)` | base |
| `idx_edges_file` | `edges(file_path)` | base |
| `idx_edges_target_kind` | `edges(target_qualified, kind)` | base, v7 |
| `idx_edges_source_kind` | `edges(source_qualified, kind)` | base, v7 |
| `idx_flows_criticality` | `flows(criticality DESC)` | v3 |
| `idx_flows_entry` | `flows(entry_point_id)` | v3 |
| `idx_flow_memberships_node` | `flow_memberships(node_id)` | v3 |
| `idx_nodes_community` | `nodes(community_id)` | v4 |
| `idx_communities_parent` | `communities(parent_id)` | v4 |
| `idx_communities_cohesion` | `communities(cohesion DESC)` | v4 |
| `idx_edges_composite` | `edges(kind, source_qualified, target_qualified, file_path, line)` | v8 |
| `idx_nodes_symbol` | `nodes(symbol)` | v10 |
| `idx_edges_kind_target_resolution` | `edges(kind, target_resolution)` | v11 |
| `idx_nodes_fts_state_file` | `nodes_fts_state(file_path)` | v12 |

### Migrations

Each migration runs in its own transaction and updates `schema_version` on success.

| Version | Change |
|---|---|
| 2 | `nodes.signature` |
| 3 | `flows`, `flow_memberships` and their indexes |
| 4 | `communities`, `nodes.community_id` and their indexes |
| 5 | `nodes_fts` FTS5 table |
| 6 | `community_summaries`, `flow_snapshots`, `risk_index`, `idx_risk_index_score` |
| 7 | `idx_edges_target_kind`, `idx_edges_source_kind` |
| 8 | `idx_edges_composite` |
| 9 | `edges.confidence`, `edges.confidence_tier` |
| 10 | `nodes.symbol`, back-filled from `qualified_name`, and `idx_nodes_symbol` |
| 11 | `edges.target_resolution`, back-filled for `CALLS`/`REFERENCES`, and `idx_edges_kind_target_resolution` |
| 12 | `nodes_fts_state`, the mirror of the FTS index, and `idx_nodes_fts_state_file` |
| 13 | `nodes.docstring`, `nodes.name_tokens`, both back-filled; `nodes_fts` and `nodes_fts_state` widened to carry them |
| 14 | No schema change: `file_hash` cleared on Ruby `File` nodes so the next update re-parses them under the new namespaced class identities |
| 15 | Dropped `community_summaries`, `flow_snapshots` and `risk_index` with `idx_risk_index_score`, as nothing read them; dropped `idx_nodes_qualified`, `idx_edges_source` and `idx_edges_target`, as other indexes cover them. The file keeps its size until `VACUUM`; later writes reuse the freed pages |

## Terraform block types

HCL nodes keep the kinds `Class` (`resource`, `data`, `module`) and `Function` (`variable`, `output`,
`local`, `provider`). The block type is in `extra.hcl_type`, and tools return it as `hcl_type`. A
node's `name` also starts with it (`resource.aws_s3_bucket.logs`, `var.region`). Flows start at a
root module's `main.tf` (else `terragrunt.hcl`) `File` node and are named `terraform:<dir>`.
