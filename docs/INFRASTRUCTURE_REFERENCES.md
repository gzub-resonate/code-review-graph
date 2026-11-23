# Infrastructure References

The graph can answer "which files call this shared workflow or action?" for GitHub Actions.
The parser stores the strings that name another unit of code, and nothing else.

## What is stored, and what is not

The graph keeps configuration values out (`config_keys.py`, the value-free YAML, JSON and
Spring parsers). These parsers keep to that rule with an allowlist: one reference-shaped
string per construct, parsed into its parts. Everything else in the file is never read.

| File | Read | Never read |
|---|---|---|
| `.github/workflows/*.yml`, `.github/actions/**/action.yml` | `jobs.<id>.uses`, `steps[].uses` | `with`, `env`, `secrets`, `run`, `if`, every other key |

A reference that is an expression (`${{ matrix.action }}`) is marked `dynamic` and is never evaluated or resolved. Its text is
not stored. A string longer than 512 characters, with a line break or with a control character
is not a reference and is skipped. A file adds at most 500 references (`infra_references_truncated` on
its `File` node says when more were left out). A malformed file never fails a build; it simply adds no reference.

## Credentials and query strings are never stored

A reference can carry a secret inside the string itself, so each one is reduced to its parts
before anything is stored:

- URL credentials are dropped: the `user:password@` or `token@` of `https://` and
  `docker://` addresses, and of a registry reference such as
  `user:password@registry.example.com/image`.
- A fragment (`#...`) is dropped. A query string is dropped except `ref` and `version`, and only
  when the value looks like a ref or version (letters, digits and `_ . + / -`). Parameters such as
  `token`, `access_token`, `sshkey` or `X-Amz-Signature` are never stored, whatever they hold.
- The raw remote string is never stored. Only the normalised parts are: scheme or kind, host,
  path and ref. Only a plain relative local path (no `://`, `::`, `?`, `#` or `@`) is
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
| `external:docker-image:<name>` | An image from a workflow `docker://` reference. The name keeps the registry and a port (`registry.example.com:5000/base/python`) and is lower case. `python` and `docker.io/library/python` stay two names |

A local reference points at the file it names when that file exists in the repository: a
workflow `uses: ./.github/workflows/x.yml`, a local action directory (its `action.yml`). When the file is missing the edge keeps the relative path as written. Local references are only resolved
inside the repository (no absolute paths, no `..` or symbolic link that leaves it) and never to an
excluded or ignored file; any other plain local path is kept as written.

`extra` on these edges:

| Key | Where | Value |
|---|---|---|
| `resolution` | all | `github_actions_uses` |
| `dynamic` | all | True when the reference holds an expression or could not be reduced safely. No text is stored |
| `kind` | GitHub Actions | `action`, `reusable_workflow`, `local` or `docker` |
| `owner_repo`, `path` | GitHub Actions | The parts of a remote reference |
| `ref`, `ref_type` | GitHub Actions | The text after `@` when it is a plain ref, and `sha` (40 hex digits), `ref` or `dynamic` (no `ref` then) |
| `scope`, `job` | GitHub Actions | `job` or `step`, and the job id |
| `name`, `registry`, `tag`, `digest` | Docker | The parts of an image reference, without credentials or query (`name` is `None` when dynamic) |

A workflow's `File` node also lists its references in `extra.github_actions_uses`.

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
