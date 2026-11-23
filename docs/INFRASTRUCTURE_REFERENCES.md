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
`find_in_parent_folders()`) is marked `dynamic` and is never evaluated or resolved. Its text is
not stored. A string longer than 512 characters, with a line break or with a control character
is not a reference and is skipped. A file adds at most 500 references (`infra_references_truncated` on
its `File` node says when more were left out), and a Dockerfile is read up to 2,000 lines and
1 MB. A malformed file never fails a build; it simply adds no reference.

## Credentials and query strings are never stored

A reference can carry a secret inside the string itself, so each one is reduced to its parts
before anything is stored:

- URL credentials are dropped: the `user:password@` or `token@` of `https://`, `ssh://`,
  `git::https://` and `docker://` addresses, and of a registry reference such as
  `user:password@registry.example.com/image`.
- For an scp-like address (`user@host:org/repo.git`) the user is dropped too, including the
  conventional `git@`. The address is stored as `host/org/repo`, the same as the shorthand
  `host/org/repo`. The user name is not a secret, but it is not needed to say which module it is.
- A fragment (`#...`) is dropped. A query string is dropped except `ref` and `version`, and only
  when the value looks like a ref or version (letters, digits and `_ . + / -`). Parameters such as
  `token`, `access_token`, `sshkey` or `X-Amz-Signature` are never stored, whatever they hold.
- The raw remote string is never stored. Only the normalised parts are: scheme or kind, host,
  path, subdir and ref. Only a plain relative local path (no `://`, `::`, `?`, `#` or `@`) is
  kept as written.
- A reference that cannot be reduced safely (for example a `?` before the `@`, so the end of the
  credentials is unclear, or an unexpected character in a ref) is recorded as `dynamic` with no
  text, and its edge points at the `dynamic` target.

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
source. When the file is missing the edge keeps the relative path as written. Local references are only resolved
inside the repository (no absolute paths, no `..` or symbolic link that leaves it) and never to an
excluded or ignored file; any other plain local path is kept as written.

`extra` on these edges:

| Key | Where | Value |
|---|---|---|
| `resolution` | all | `github_actions_uses`, `dockerfile_from`, `dockerfile_copy_from` or `terragrunt_reference` |
| `dynamic` | all | True when the reference holds an expression or could not be reduced safely. No text is stored |
| `kind` | GitHub Actions | `action`, `reusable_workflow`, `local` or `docker` |
| `owner_repo`, `path` | GitHub Actions | The parts of a remote reference |
| `ref`, `ref_type` | GitHub Actions | The text after `@` when it is a plain ref, and `sha` (40 hex digits), `ref` or `dynamic` (no `ref` then) |
| `scope`, `job` | GitHub Actions | `job` or `step`, and the job id |
| `name`, `registry`, `tag`, `digest` | Docker | The parts of an image reference, without credentials or query (`name` is `None` when dynamic) |
| `via` | Docker | `from` or `copy_from` |
| `stage`, `stage_index` | Docker | The build stage the reference is in |
| `block`, `name` | Terragrunt | `terraform`, `include` or `dependency`, and the block label |
| `source_kind`, `ref`, `subdir` | Terragrunt `terraform` | `git`, `registry`, `archive`, `local` or `other`; the `ref` or `version` query value; the `//subdir` |

A workflow's `File` node also lists its references in `extra.github_actions_uses`.

A Dockerfile build stage is a `Class` node named `stage:<alias>` (`stage:<index>` without an
alias), with `extra.dockerfile_type = "stage"`, `stage_index`, `stage_name` and `base_image`
(`name[:tag][@digest]` of the `FROM` image, without credentials; `None` when dynamic). `FROM <earlier stage>` and
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
