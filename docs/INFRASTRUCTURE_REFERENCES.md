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
| `*.hcl` (Terragrunt) | `terraform.source`, `include.path`, `dependency.config_path`, `dependencies.paths`; in `inputs`, only the keys listed under [Terragrunt inputs](#terragrunt-inputs) | `mock_outputs`, every other key of `inputs` and every other attribute. A `locals` value is read only to work out one of the references or allowlisted inputs (see [Expressions](#expressions)); it is never stored |
| `*.tf` | `module.source` | every other attribute |

A GitHub Actions or Dockerfile reference that is an expression (`${{ matrix.action }}`,
`FROM ${BASE}`) is marked `dynamic` and is never evaluated or resolved. Its text is not stored.
A Terragrunt expression is reduced from the files when that is possible and kept `dynamic`
otherwise (see [Expressions](#expressions)). A string longer than 512 characters, with a line break or with a control character
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

## Terragrunt inputs

`inputs` holds values, so it is read through an allowlist of keys and nothing else. An unlisted
key is skipped without its value being visited, in the unit and in the files it includes.

| Key | What is kept | What it is for |
|---|---|---|
| `step_function_file_path` | The Step Functions definition file it names, when that file is in the repository (the path is tried from the repository root, then from the unit) | Says which `.asl.json` the unit deploys |
| `replacement_vars` | For an entry whose value is an ARN of Lambda, Step Functions, ECS (task definition or cluster), SNS, SQS, DynamoDB, Batch (job definition), Glue (job) or CodeBuild (project): the entry key, the service and the resource name. The partition must be `aws`, `aws-us-gov` or `aws-cn`. The account id, the region and a version, alias or revision are dropped | Says what a `${key}` placeholder of the definition stands for |
| `lambda_replacement_vars`, `step_function_replacement_vars`, `sns_topic_replacement_vars` | The entry key and the plain name (letters, digits, `_`, `.`, `-`), with the service the map is for | The same, for the typed maps of a shared `step_function` module |
| `handler` | A `module.function` string (`pkg/module.function` and `pkg.module.function` too) | The code a Lambda unit runs |
| `function_name` | A plain function name | The name the Lambda is deployed under |
| `git_repo_sub_dir` | The directory it evaluates to, when that directory is in the repository | Where the handler's module is |

No raw value is stored. A value that is not of the shape above leaves nothing behind: a subnet
or security group id, an e-mail address, a bucket name, a URL (with or without credentials), a
password, an ARN of any other service. An entry whose key looks like a secret (`secret`,
`passw`, `token`, `credential`, `api_key`, `private_key`, `access_key`, in any case) is skipped
whatever its value. A value written as an expression is reduced as described under
[Expressions](#expressions), and one that cannot be reduced leaves no entry and no text. In an
ARN, the account id and the region may be expressions that do not reduce (they are dropped
anyway); the partition, the service and the name must be literal.

The reduced data is on the unit's `File` node as `extra.terragrunt_inputs`
(`state_machine_file`, `replacement_vars` as `{key, service, kind, name}`, `handler`,
`function_name`, `source_dir`). `handler`, `function_name` and `source_dir` are recorded for a
unit that can be deployed (it has an `include` or a `terraform` block), not for a base file that
other units include. A unit is looked at only when it, or a file it includes, defines
`step_function_file_path`, one of the replacement maps or `handler`. A unit with more than 3
`include` blocks is not read through its includes: it gets `dynamic_reason: "depth_limit"` and
nothing is guessed. The unit's own entries win over an included file's, and an included file
adds entries to a map only when the unit does not define it or the include sets
`merge_strategy = "deep"`.

`asl_resolver.py` turns this into edges after the build (see `docs/schema.md`, "Step Functions"):
a Task state whose target is `${key}` gets a `REFERENCES` edge to
`external:aws-<service>:<name>`, and a state that runs a Lambda of the repository gets one to the
handler function.

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
| `external:terraform-module:<address>` | A remote Terragrunt `source` or Terraform `module` source (git, registry or archive), without `//subdir` and without the query |
| `external:terraform-module:dynamic` | A remote `module` source in a `.tf` file that could not be reduced safely |
| `external:terragrunt:dynamic` | A Terragrunt source, include or dependency that could not be reduced to a file or an address |

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
| `block`, `name` | Terragrunt | `terraform`, `include`, `dependency` or `dependencies`, and the block label |
| `source_kind`, `ref`, `subdir` | Terragrunt `terraform`, `.tf` `module` | `git`, `registry`, `archive`, `local` or `other`; the `ref` or `version` query value; the `//subdir` |
| `resolved_statically` | Terragrunt | True when the target was worked out from an expression (see [Expressions](#expressions)) |
| `dynamic_reason` | Terragrunt | Why a `dynamic` reference was not resolved: `env`, `command`, `variable`, `unsupported_function`, `depth_limit` or `not_found`. Absent when the written-out string itself could not be reduced safely |

A `.tf` `module` source gets the same treatment as a Terragrunt source: a local path is linked
to the module's file after the build, and a registry, git or archive source is an
`external:terraform-module:` target with its `ref` in `extra`. Credentials and query values in
it are never stored. A `module` source that is an expression adds no edge.

A workflow's `File` node also lists its references in `extra.github_actions_uses`.

A Dockerfile build stage is a `Class` node named `stage:<alias>` (`stage:<index>` without an
alias), with `extra.dockerfile_type = "stage"`, `stage_index`, `stage_name` and `base_image`
(`name[:tag][@digest]` of the `FROM` image, without credentials; `None` when dynamic). `FROM <earlier stage>` and
`COPY --from=<earlier stage or index>` are `DEPENDS_ON` edges between stages.

## Expressions

Terragrunt writes most references as expressions. The parser reduces them to a path or an
address by reading the files, as a person would, and never by running anything. The result is
used once to find the target and is not stored.

Resolved:

- `find_in_parent_folders()` and `find_in_parent_folders("name")`: the nearest file of that name
  in a parent directory, searching up to the repository root only (the default name is
  `terragrunt.hcl`; the unit's own directory is not searched, as in Terragrunt).
- `get_terragrunt_dir()`, `get_original_terragrunt_dir()`, `get_parent_terragrunt_dir()` and
  `get_parent_terragrunt_dir("name")` (the directory of that `include`), `get_repo_root()`,
  `get_path_to_repo_root()`, `path_relative_to_include()`, `path_relative_from_include()` and
  `dirname()`. The functions that name the unit being run need a file called `terragrunt.hcl`.
  In any other `.hcl` file, which is only ever included, they stay `dynamic` because the answer
  depends on the file that includes it. When a unit's reference or allowlisted input is worked
  out through a file it includes, `path_relative_to_include()` in that file is the unit's path
  relative to it, as in Terragrunt.
- `split`, `join`, `slice`, `length`, `basename`, an index (`list[n]`) and `+` / `-` on whole
  numbers. They are pure functions of their arguments. A list or a number exists only between
  them: an expression whose result is not a string stays `dynamic`.
- Strings that combine these with literals (`"${get_repo_root()}/modules//app"`; the part after
  `//` is the module directory, as in Terragrunt).
- `local.<name>` from the same file, and `include.<name>.locals.<name>` when that `include` has
  `expose = true`. The included file is read, up to 3 files per reference. A local is used only to
  work out the target; its value is never stored, and a remote address is reduced exactly like a
  written-out one (no credentials, only `ref` and `version`).
- A directory that holds a module stands for its `main.tf`, else its first `*.tf` file by name,
  so the answer does not depend on the order the file system lists entries.

Everything else stays `dynamic` with a coarse `dynamic_reason` and no text:

| `dynamic_reason` | Meaning |
|---|---|
| `env` | `get_env(...)`. No environment variable is ever read |
| `command` | `run_cmd(...)`. Nothing is ever run |
| `variable` | `var.*`, `each.*`, `count.*`, `dependency` outputs, a conditional or any other value that the files do not hold |
| `unsupported_function` | Any other function (`sops_decrypt_file`, `read_terragrunt_config`, `format`, `try`, ...) or construct (a list as the result, a heredoc, a template directive) |
| `depth_limit` | A bound was reached or the expressions are cyclic: nesting depth 40, 400 nodes, 3 included files, 1 MB per included file, 4,096 characters in a value, 256 items in a list |
| `not_found` | A file, `include`, `local` or parent file it needs is missing, is outside the repository, is excluded, or the path it names does not exist |

Safety rules:

- Nothing is executed, nothing is read from the environment and nothing touches the network. A
  planted `run_cmd` or `get_env` is never run or read.
- A file is opened only when it resolves, symbolic links included, to a place inside the
  repository that is not excluded. A path that leaves the repository (`../../..`) is `not_found`.
- A value taken from `locals`, `inputs` or an included file is never stored as written,
  whatever it holds. Only the target is: a file in the repository, or an address reduced as
  above. For the allowlisted `inputs` keys, only the naming data described under
  [Terragrunt inputs](#terragrunt-inputs) is.
- Evaluation is bounded (see `depth_limit`), so a cyclic or hostile include chain ends at once
  as `dynamic`.
- The same files always give the same edges.

Not resolved, for lack of use in the repositories measured: `read_terragrunt_config(...)`,
`format`/`try`, locals more than one file away, and `${path.module}` in a `.tf`
`module` source.

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
