# Service-Name Aliases and Infrastructure Joins

`cross_repo_requests_tool` joins a Spring Feign call to the repository that answers it by the
`@FeignClient` service name. That name is often not a registered repository name
(`billing-api-accounts`, `shared-catalog`). This page describes how such names reach a
repository, and how a code repository is related to the infrastructure declared for it. Both
are query-time joins over the finished graphs: nothing is stored and no graph is rebuilt.

## Service-name resolution

A Feign service name is resolved in this order. The first step that finds the repository wins.

1. **Explicit alias file** (optional). The path is in the environment variable
   `CRG_SERVICE_ALIASES_FILE`. `matched_via` is `explicit_alias`.
2. **Registered names**: a registry alias or folder name, the `repo-aliases.toml` file (see
   [REPO_ALIASES.md](REPO_ALIASES.md)), then a name that differs only in case, `-` versus `_`
   or repeated separators. `matched_via` is `repo_name`.
3. **Inference** from what each registered repository declares about itself. `matched_via` is
   the first that applies:
   - `spring_application_name`: `spring.application.name` from its `application` or
     `bootstrap` configuration (never test resources; `${VAR:default}` uses the default);
   - `ecs_service`: an `aws_ecs_service` `name` or an `aws_ecs_task_definition` `family` that
     the repository's own Terraform declares as a literal;
   - `ecr_repo`: an `aws_ecr_repository` `name` declared the same way.

   ECS and ECR names count only for a repository that declares at most three of them. A
   repository that declares more is an infrastructure repository, and the names it declares
   belong to other repositories.

Inferred names are compared after folding case, `-` and `_` runs, and one trailing environment
suffix (`-dev`, `-qa`, `-prod`, `-stage`, `-staging`, `-test`, `-uat`). Only equality of the
folded name matches: `orders` never equals `orders-api`. A name that reaches several
repositories is not picked: the row is `ambiguous` with reason `service_name_ambiguous` and
`candidate_repos` (with `candidate_repos_total`). Every resolved row carries `matched_via`.

### Alias file format

JSON or YAML (by extension; anything else is tried as JSON, then YAML). One flat mapping from a
service name to a registered repository alias or folder name:

```json
{
  "shared-catalog": "catalog-service",
  "prefix:billing-api-": "billing-api"
}
```

- A plain key is a service name. It matches exactly, or once case and `-`/`_` are folded.
- A `prefix:` key is a prefix rule: every service name that starts with the prefix (case and
  `-`/`_` folded) maps to the repository. The longest matching prefix wins.
- The value is looked up like every other repository name (registry alias, then folder name,
  then `repo-aliases.toml`); chains are not followed. A value that matches no registered
  repository is ignored, and the name falls through to the next step.
- A top-level `{"aliases": {...}}` wrapper is accepted. Keys use letters, digits, `.`, `_` and
  `-` (1 to 100 characters). At most 500 entries and 1 MB are read. A broken file or entry never
  raises: it is skipped with a warning.

The file is read on every call, so a change takes effect without a restart. An explicit rule
outranks the other sources, including a repository whose own name is the same.

## Infrastructure joined to code

`cross_repo_requests_tool` with `origin` `terraform`, `stepfunction` or `awssdk` adds an
`infrastructure` object to the reply. `infrastructure.repos` has one entry per selected
repository that has at least one match (`repos_total` counts them, `repos_checked` says how many
were examined, at most 20 per call). Each entry has `identity` (the names the repository is
known by, each with `matched_via`), one list per category, capped at 10 items, and `counts`
with the real totals:

| List | What matches |
|------|--------------|
| `ecr_repositories` | an `aws_ecr_repository` whose `name` equals a repository name or alias |
| `ecs_services` | an `aws_ecs_service` `name` equal to one |
| `task_definitions` | an `aws_ecs_task_definition` `family` equal to one |
| `stacks` | a Terragrunt stack or Terraform module directory whose name equals one (`stack_kind`: `terragrunt_stack` or `terraform_module`) |
| `resources` | a declared queue, topic, bucket, table, function ... whose name equals a name the repository's code uses, written out (`matched_via` `literal`) or read from Spring configuration (`resolved_name`, with `profiles`); and what the repository itself declares (`owned`, `declared_in_repo`) |

Every item has `kind`, `name`, `repo`, `file` (relative to that repository), `line` where there
is one, and `matched_via`: `explicit_alias`, `spring_application_name`, `repo_name`,
`declared_in_repo`, `literal` or `resolved_name`. A `prefix:` alias rule matches infrastructure
names that start with the prefix. Repository-derived strings are sanitised and whitespace is
folded.

A declaration in another registered repository is found even when that repository is not among
the selected `repos`. `origin="awssdk"` rows whose owner is a Terraform declaration carry
`declared_by` (`repo`, `file`, `line`, `name`) beside `target_repo`. `query_graph_tool`
`external_targets` marks an AWS target, or a name resolved from configuration, that a Terraform
`declare` edge names with `matched: true` and `declared_by`: the declaration in the graph itself,
else the first one in another registered repository.

Not covered: a Feign client with a fixed `url` is `unmatchable` (the host is not stored in the
graph), and an ECR, ECS or directory name that is built from variables is not a literal, so it is
not read.
