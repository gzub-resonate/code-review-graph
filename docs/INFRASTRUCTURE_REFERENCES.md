# Infrastructure References

The graph can answer "which files call this shared workflow, build from this base image or
use this module?" for GitHub Actions, Dockerfiles and Terragrunt. The parser stores the
strings that name another unit of code, and nothing else.

## What is stored, and what is not

The graph keeps configuration values out (`config_keys.py`, the value-free YAML, JSON and
Spring parsers). These parsers keep to that rule with an allowlist: one reference-shaped
string per construct, parsed into its parts. Everything else in the file is never read.

| File | Read | Never read |
|---|---|---|
| `.github/workflows/*.yml`, `.github/actions/**/action.yml` | `jobs.<id>.uses`, `steps[].uses` | `with`, `env`, `secrets`, `run`, `if`, every other key |
| `Dockerfile`, `Dockerfile.*`, `*.Dockerfile`, `Containerfile` | `FROM` image and stage alias, `COPY --from` | `ARG`, `ENV`, `LABEL`, `RUN`, build arguments and every other instruction |
| `*.hcl` (Terragrunt) | `terraform.source`, `include.path`, `dependency.config_path` | `inputs`, `locals`, `mock_outputs` and every other attribute |

A reference that is an expression (`${{ matrix.action }}`, `FROM ${BASE}`,
`find_in_parent_folders()`) is stored as written, marked `dynamic`, and never evaluated or
resolved. A string longer than 512 characters, with a line break or with a control character
is not a reference and is skipped. A file adds at most 500 references (`infra_references_truncated` on
its `File` node says when more were left out), and a Dockerfile is read up to 2,000 lines and
1 MB. A malformed file never fails a build; it simply adds no reference.

## Edges and targets

Each reference is an `IMPORTS_FROM` edge from the file to a target outside the repository.
The version, tag or ref is in `extra`, so every version of one action, image or module shares
one target and a query by target finds them all.

| Target | Meaning |
|---|---|
| `external:github-actions:<owner/repo[/path]>` | An action or reusable workflow. `owner/repo` is lower case. `path` is the part inside the repository, such as `.github/workflows/build.yml` or `init` in `github/codeql-action/init` |
| `external:github-actions:dynamic` | A `uses` whose repository part is an expression |
| `external:docker-image:<name>` | An image from a Dockerfile `FROM` or `COPY --from`, or from a workflow `docker://` reference. The name keeps the registry and a port (`registry.example.com:5000/base/python`) and is lower case. `python` and `docker.io/library/python` stay two names |
| `external:docker-image:dynamic` | An image that uses a build argument or variable |
| `external:terraform-module:<address>` | A remote Terragrunt source (git, registry or archive), without `//subdir` and without the query |
| `external:terragrunt:dynamic` | A Terragrunt source, include or dependency that is an expression |

A local reference points at the file it names when that file exists in the repository: a
workflow `uses: ./.github/workflows/x.yml`, a local action directory (its `action.yml`), a
Terragrunt `include` path, a `dependency` directory (its `terragrunt.hcl`) and a local module
source. When the file is missing the edge keeps the raw path. Local references are only resolved
inside the repository (no absolute paths, no `..` or symbolic link that leaves it) and never to an
excluded or ignored file; any other reference keeps its raw path.

`extra` on these edges:

| Key | Where | Value |
|---|---|---|
| `resolution` | all | `github_actions_uses`, `dockerfile_from`, `dockerfile_copy_from` or `terragrunt_reference` |
| `raw` | all | The reference as written |
| `dynamic` | all | True when the reference holds an expression |
| `kind` | GitHub Actions | `action`, `reusable_workflow`, `local` or `docker` |
| `owner_repo`, `path` | GitHub Actions | The parts of a remote reference |
| `ref`, `ref_type` | GitHub Actions | The text after `@`, and `sha` (40 hex digits), `ref` or `dynamic` |
| `scope`, `job` | GitHub Actions | `job` or `step`, and the job id |
| `name`, `registry`, `tag`, `digest` | Docker | The parts of an image reference (`name` is `None` when dynamic) |
| `via` | Docker | `from` or `copy_from` |
| `stage`, `stage_index` | Docker | The build stage the reference is in |
| `block`, `name` | Terragrunt | `terraform`, `include` or `dependency`, and the block label |
| `source_kind`, `ref`, `subdir` | Terragrunt `terraform` | `git`, `registry`, `archive`, `local` or `other`; the `ref` or `version` query value; the `//subdir` |

A workflow's `File` node also lists its references in `extra.github_actions_uses`.

A Dockerfile build stage is a `Class` node named `stage:<alias>` (`stage:<index>` without an
alias), with `extra.dockerfile_type = "stage"`. `FROM <earlier stage>` and
`COPY --from=<earlier stage or index>` are `DEPENDS_ON` edges between stages.

## Querying

`imports_of` on a file lists its references:

```text
query_graph_tool(pattern="imports_of", target="/repo/.github/workflows/ci.yml")
```

`importers_of` looks a target up as a node or a file path, and an external target has neither,
so it reports `not_found` for these. To list every file that uses one external target, read the
edges directly:

```bash
sqlite3 .code-review-graph/graph.db \
  "SELECT file_path, line, json_extract(extra, '$.ref') FROM edges
   WHERE kind = 'IMPORTS_FROM'
     AND target_qualified = 'external:github-actions:acme/shared-workflows/.github/workflows/build.yml'"
```

Use `target_qualified LIKE 'external:github-actions:acme/shared-workflows%'` for a whole
repository. Joining the graphs of several repositories is left to the caller.

## Upgrading

Files parsed by an earlier version have none of these edges. Run `code-review-graph build`
once to add them; an incremental `update` only reparses changed files.
