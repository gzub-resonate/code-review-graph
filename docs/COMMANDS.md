# All Available Commands

- [CLI commands](#cli-commands)
  - [Exit codes](#exit-codes)
  - [Notes](#notes)
- [Standalone daemon CLI (`crg-daemon`)](#standalone-daemon-cli-crg-daemon)
- [MCP tools](#mcp-tools)
- [Result bounds](#result-bounds)
- [MCP prompts](#mcp-prompts-5-workflow-templates)
- [Skills and slash commands](#skills-and-slash-commands)

## CLI Commands

Start with `code-review-graph install && code-review-graph build`, then
`code-review-graph detect-changes --brief`. `code-review-graph` on its own
prints every command in groups. Run `code-review-graph <command> --help` for the
full option list. A mistyped command exits 2 and suggests the closest one:

```
code-review-graph: error: unknown command 'biuld'. Did you mean build? Run code-review-graph --help to see every command.
```

```bash
# Setup
code-review-graph --version         # Show the version (or -v)
code-review-graph install           # Set up the AI coding tools found on this machine (alias: init)
code-review-graph install --dry-run # Show each file's diff; write nothing
code-review-graph install --gitignore  # Also add the files this run creates to .gitignore (off by default)
code-review-graph install --platform codex  # Set up one tool, found here or not: Codex MCP, hooks, and global Skill
code-review-graph install --no-hooks        # Skip hooks (also --no-skills, --no-instructions)
code-review-graph install -y                # Answer yes to the question about instruction files
code-review-graph install --steering        # Opt in: graph context beside Claude Code searches and reads
code-review-graph install --no-steering     # Remove only that hook
code-review-graph doctor                    # Check every copy, the MCP entries, the graph and the hooks (read-only)
code-review-graph doctor --json             # The same report as one JSON object (--repo PATH for another repo)
code-review-graph uninstall                 # Remove CRG configs, hooks, skills, and data
code-review-graph uninstall --dry-run       # List what it would remove; change nothing
code-review-graph uninstall --keep-data     # Remove integrations, keep graph databases
code-review-graph uninstall --platform codex  # Unbind one platform (keeps graph data and others)
code-review-graph uninstall --all-repos        # Also clean every repository in the registry
code-review-graph uninstall --keep-user-configs  # Clean repositories only; leave files in your home folder
code-review-graph uninstall -y                 # Apply without asking

# Build and update
code-review-graph build                        # Full build
code-review-graph build --data-dir /mnt/graphs/myrepo  # Keep graph.db outside the repo (or set CRG_DATA_DIR); also on update,
                                                #   postprocess, embed, watch, status, context-history, forget, visualize, wiki, dead-code
code-review-graph build --skip-flows           # Parse + signatures + FTS only
code-review-graph build --skip-postprocess     # Raw parse only
code-review-graph build -q                     # No output (-q/--quiet also on update and status)
code-review-graph build --enrich               # Also resolve Python calls with Jedi (needs [enrichment]);
                                                #   also on update, or CRG_ENABLE_ENRICHMENT=1. Off by default
code-review-graph update                       # Incremental update from the last-built commit
code-review-graph update --base origin/main    # Custom base ref
code-review-graph update --brief               # Update graph, then show the risk panel
code-review-graph update --brief --verify      # ...and cross-check against tiktoken
code-review-graph update --wait                # Wait for a running build to finish, then update
code-review-graph build --follow-symlinks      # Also follow symlinked folders, inside or outside the repo
                                                #   (off by default; also on update and watch, or CRG_FOLLOW_SYMLINKS=1)
code-review-graph postprocess                  # Re-run flows, communities, FTS
code-review-graph postprocess --no-flows       # Skip a step: --no-flows, --no-communities, --no-fts
code-review-graph forget PATH [PATH ...]       # Drop parsed files from the graph (no full rebuild)
code-review-graph forget src/legacy --dry-run  # Preview which files would be forgotten
code-review-graph forget PATH --wait           # Wait for a running write to finish, then forget
code-review-graph embed --provider local       # Compute vector embeddings for semantic search
code-review-graph embed --wait                 # Wait for a running write to finish, then embed
code-review-graph update --embedding-provider local --embedding-model all-MiniLM-L6-v2
                                                # Refresh an existing embedding index (default: off)

# Monitor and inspect
code-review-graph status                       # Graph statistics (no graph: exit 1, no DB created)
code-review-graph status --json                # One JSON object
code-review-graph context-history              # Per-day trend of the opt-in context savings log
code-review-graph context-history --by tool --days 7  # Per tool, last 7 UTC days (--json for JSON)
code-review-graph watch                        # Auto-update on file changes (needs an existing graph)
code-review-graph visualize                    # Interactive HTML graph (needs an existing graph)
code-review-graph visualize --format graphml   # Formats: html, json, graphml, cypher, obsidian, svg
                                                #   Written to the data folder (.code-review-graph/ by default) as
                                                #   graph.<ext>, or obsidian/. svg needs matplotlib:
                                                #   pip install "code-review-graph[eval]" (without it: exit 1)
code-review-graph visualize --mode community   # Modes: auto (default), full, community, file
code-review-graph visualize --serve            # Serve graph.html on localhost:8765

# Neighbourhood view: draw a symbol's surroundings instead of the repository.
# The whole-repo page collapses to one bubble per community past 3000 nodes or
# 9000 edges; a seeded page ships only the nodes within --depth hops.
code-review-graph visualize --seed-symbol login             # depth 2 by default
code-review-graph visualize --seed-symbol login --depth 3   # more context
code-review-graph visualize --seed-file src/auth.py         # a file and its symbols
code-review-graph visualize --seed-changed                  # the files in this review
code-review-graph visualize --seed-changed --seed-changed-base origin/main
code-review-graph visualize --seed-flow "login request"     # an execution flow
code-review-graph visualize --path-from login --path-to audit_log
code-review-graph visualize --seed-symbol login --render-depth 0  # expand on click
code-review-graph visualize --seed-symbol login --max-nodes 300  # hard cap
code-review-graph visualize --seed-symbol login --sidecar   # payload in graph.data.js

# Analysis
code-review-graph detect-changes               # Risk-scored change analysis (read-only): the last commit
                                                #   (HEAD~1) plus uncommitted and new files
code-review-graph detect-changes --base HEAD~3 # Custom base revision (one Git does not know: exit 1)
code-review-graph detect-changes --base origin/main # Branch refs use their merge base with HEAD
code-review-graph detect-changes --brief       # Summary, the top three things to review, and the token-savings panel
code-review-graph detect-changes --brief --verify  # ...and cross-check against tiktoken
code-review-graph detect-changes --churn       # Add opt-in change-frequency risk (CRG_CHURN_WINDOW_DAYS, default 90)
                                                #   The MCP tools (detect_changes_tool, get_minimal_context_tool) always include it.
                                                #   Cached per commit; CRG_CHURN_TIMEOUT (5s) and CRG_CHURN_MAX_COMMITS
                                                #   (2000) bound the git log. A repository that cannot answer in time is
                                                #   recorded as unavailable for the life of the process, so the timeout is
                                                #   paid once, and the response says so: `churn_status` is "unavailable"
                                                #   and the summary carries a "Degraded" line.
code-review-graph detect-changes --details all # Add bounded graph context under "details" (JSON only):
                                                #   risk, communities, hubs, coupling, impact, gaps, graph.
                                                #   Off by default; the GitHub Action's `details` input uses it.
code-review-graph dead-code                    # Functions/classes with no callers or test references
                                                #   A bare name passed as a value, a type-position use, a Class.MEMBER
                                                #   read, a re-export (__init__.py, export ... from), a Scala main or
                                                #   `extends App` object, an inline handler string (onclick="fn()") and
                                                #   Terraform resource/module/output/provider blocks all count as used.
code-review-graph dead-code --kind Function --file-pattern src/ --json
code-review-graph dead-code --limit 20         # 0, the default, means no limit

# Read-only graph queries (CLI mirrors of the MCP tools)
code-review-graph query callers_of <target>    # Takes every query_graph_tool pattern: callers_of,
                                                #   references_to, callees_of, imports_of, importers_of,
                                                #   children_of, tests_for, inheritors_of, next_states_of,
                                                #   previous_states_of, triggers_of, triggered_by, publishers_of,
                                                #   listeners_of, advises, advised_by, handlers_of, endpoints_for,
                                                #   requests_of, requesters_of, consumers_of, file_summary, path_to,
                                                #   external_targets, endpoints
code-review-graph query path_to <a> --to <b>  # How a reaches b, hop by hop
code-review-graph impact [--files F ...] [--depth N] [--max-results N] [--base REF] [--detail-level compact] [--max-files N]
code-review-graph search <query> [--kind Function] [--limit N]
code-review-graph flows [--sort criticality] [--limit N] [--kind KIND]
code-review-graph flow --id ID | --name NAME [--source]
code-review-graph communities [--sort size] [--min-size N]
code-review-graph community --id ID | --name NAME [--members]
code-review-graph architecture [--detail-level minimal|standard]
code-review-graph large-functions [--min-lines N] [--kind Function] [--path SUBSTR] [--limit N] [--include-tests]
code-review-graph refactor rename --old-name A --new-name B
code-review-graph refactor dead_code [--kind Function] [--path SUBSTR]
code-review-graph refactor suggest

# Wiki
code-review-graph wiki                         # Markdown wiki in .code-review-graph/wiki/ (needs an existing graph)
code-review-graph wiki --force                 # Rewrite every page, even unchanged ones

# Multi-repo
code-review-graph register <path> [--alias name]  # Register a repository
code-review-graph unregister <path_or_alias>       # Remove from registry
code-review-graph repos                            # List registered repositories

# Daemon (multi-repo watcher), installed with the package
code-review-graph daemon start [--foreground]       # Start the watch daemon
code-review-graph daemon stop                       # Stop the daemon
code-review-graph daemon restart [--foreground]     # Restart the daemon
code-review-graph daemon status                     # Daemon status and repos
code-review-graph daemon logs [--repo ALIAS] [--follow] [--lines N]  # Daemon or per-repo logs (default 50 lines)
code-review-graph daemon add <path> [--alias NAME]  # Add a repo to the daemon config
code-review-graph daemon remove <path_or_alias>     # Remove a repo from the daemon config

# Evaluation
code-review-graph eval --all                   # Run every benchmark on every repo config (bare `eval` runs nothing)
code-review-graph eval --benchmark token_efficiency --repo flask  # Pick benchmarks and repos (comma-separated); --repo takes config names, not paths
code-review-graph eval --report                # Write evaluate/reports/summary.md from evaluate/results and print README tables
                                                #   (only each repo's newest run of each benchmark)
                                                #   (--output-dir sets the results folder that runs write and --report reads)
code-review-graph eval --benchmark search_quality --embed [--embed-provider local|openai|google|minimax|voyage] [--embed-model M]  # --embed builds vectors; agent_baseline,
                                                #   search_quality and multi_hop_retrieval need it

# Hook entry points (the hooks install writes run these; they read the hook event JSON on stdin).
# --help and the command list leave them out, but they still run.
code-review-graph hook-update                  # Update the graph of the repo that holds the edited file
code-review-graph hook-update --existing-only  # The same, but never build a new graph (the Codex hook)
code-review-graph hook-status                  # Graph status for the event's cwd
code-review-graph enrich                       # Graph context for a search or read (the --steering hook)

# Server
code-review-graph serve                        # Start MCP server (stdio)
code-review-graph serve --auto-watch           # Also watch for file changes while the server runs
code-review-graph serve --http                 # Streamable HTTP on 127.0.0.1:5555 (--host, --port)
code-review-graph serve --http --multi-repo    # One HTTP server for every registered repository
code-review-graph serve --tools query_graph_tool,detect_changes_tool  # Tool allowlist (or CRG_TOOLS)
code-review-graph serve --multi-worktree --repo /path/to/repo  # One stdio session, one child per linked worktree
code-review-graph mcp                          # Alias for serve; accepts --repo, --auto-watch and --multi-worktree
```

`--multi-repo` lets one `serve --http` process serve several repositories, so
the embedding model is loaded once. It serves the repositories in the registry
(`code-review-graph register <path>`), read again on every call, so `register`
and `unregister` take effect without a restart. Every tool call must pass
`repo_root`, the absolute path of a registered repository; `list_repos_tool`
lists them. A call with no `repo_root`, a relative path or a path that is not
registered is refused with a message saying so. It never falls back to `--repo`
or the server's working directory. The usual project-root check still applies.
`--multi-repo` needs `--http` and cannot be combined with `--repo`,
`--auto-watch`, `--multi-worktree` or `CRG_DATA_DIR`. To keep several graphs
current, run the daemon (`code-review-graph daemon`). Without `--multi-repo`,
`serve --http` keeps the old fallback.

`--multi-worktree` is an opt-in stdio mode for parallel reviews. The server
starts one regular `serve --repo <root>` child lazily for the primary checkout
and each linked worktree requested through `repo_root`. Only paths reported by
`git worktree list` for the same Git common directory are accepted. Each child
uses its worktree's normal `.code-review-graph/graph.db` or its per-repository
Registry `data_dir`, so graphs from different commits cannot be mixed.

The mode rejects a process-wide `CRG_DATA_DIR`, because that would force all
worktrees into one database. Configure external storage with a separate,
non-overlapping Registry `data_dir` entry per worktree instead; the router
rejects two worktrees that resolve to the same graph database. `--multi-worktree`
is stdio only and cannot be combined with `--http`.

### Exit codes

| Code | Meaning | Examples |
|------|---------|----------|
| `0` | Done, including when there was nothing to do. | `No changes detected.`; `uninstall` answered no (`Aborted.`) |
| `1` | It failed, and one `Error:` line (or a line saying what is missing) on stderr says why. | no graph yet; an unusable `graph.db`; git missing or too slow; a `--base` Git does not know; `--repo` that is not a project root; `doctor` found a problem |
| `2` | The command line is wrong, so nothing ran. | an unknown command (with "Did you mean ...?"); an unknown flag; flags that cannot go together, such as `detect-changes --details all --brief`; a `visualize` seed it cannot find |
| `75` | Another write to this repository's graph is running, so this one did nothing. | `build`, `update`, `postprocess`, `forget` or `embed` without `--wait` |
| `130` | Stopped with Ctrl-C. | Ctrl-C at `install`'s question; nothing was written |

A hook or CI job can tell these apart without reading the message. `75` is
"skipped", not "failed": the running write covers the same work.

### Notes

- `update` and `detect-changes` need a Git repository or an SVN working copy.
  In SVN, `--base` takes a revision or range such as `r100:HEAD`; with no
  revision, both commands use the working-copy edits from `svn status`. In Git,
  `update` with no `--base` diffs from the commit the graph was last built at; when that commit is
  missing (fresh graph, rewritten history, shallow clone) it falls back to a
  full rebuild.
- `update` prints a `Warning:` line on stderr naming files that failed to
  parse. Those files keep their previous graph rows.
- `detect-changes --brief` is read-only against the existing graph and takes
  about a second. `update --brief` re-parses changed files first, then prints
  the same summary and panel. Use `update --brief` after a rebase or a large
  change set, or when the graph may be stale.
- `detect-changes --brief` scores risk from 0.00 to 1.00 (high above 0.70,
  medium above 0.40) and lists up to three things to review first, each with
  its file and line, its callers and its tests:

  ```
    - Overall risk score: 0.80 of 1.00 (high)

  Review first (risk out of 1.00):
    1. check_password  app/auth.py:1  risk 0.80 (high)  called by login  no tests found
    2. login  app/auth.py:7  risk 0.80 (high)  called by handle_login  tested by test_login_ok
  ```
- With no `--base`, `detect-changes` diffs against `HEAD~1`, so it reviews the
  last commit plus uncommitted and new files. A new file Git does not track is
  reviewed whole. In a repository with one commit, `HEAD~1` does not exist, so
  it prints this on stderr and reviews uncommitted and new files:

  ```
  Note: this repository has one commit; reviewing uncommitted and new files instead.
  ```

  With no commits at all, the note says `this repository has no commits yet`.

  In a shallow clone, as many CI jobs make, the note says the clone is shallow
  and that `git fetch --deepen=1` brings the last commit back.
- A `--base` Git does not know exits 1 instead of reviewing the working tree.
  The MCP review tools answer `status: "error"` with the same words:

  ```
  Error: base 'nope' is not a known revision. Check it with `git branch -a`, or run `git fetch origin <branch>` in a shallow clone.
  ```
- `status`, `detect-changes`, `visualize`, `wiki`, `watch`, `forget`,
  `dead-code` and the read-only graph queries (`query` to `refactor` above)
  exit 1 when no graph exists and do not create one. `status`,
  `detect-changes`, `visualize`, `wiki`, `watch`, `forget` and `dead-code`
  still move a legacy top-level `.code-review-graph.db` into
  `.code-review-graph/graph.db` before running.
- One write runs per repository at a time. `build`, `update`, `postprocess`,
  `forget` and `embed` take a lock on `write.lock` next to `graph.db`; if
  another one holds it, they say so, do nothing and exit 75. Pass `--wait` to
  wait for it instead. `watch` takes the lock for each write and keeps its
  changes to retry if the lock is held.
- `install` with no `--platform` (the same as `--platform all`) writes MCP
  entries, skills, hooks and instruction files only for the tools it finds on
  this machine, then names the rest:

  ```
  Not found here, so not set up: cursor, windsurf, zed, ...
  Add one anyway with: code-review-graph install --platform NAME
  ```

  `install --platform NAME` sets that tool up whether it is found or not.
- `install` lists the files it will change and asks about the instruction
  files (`CLAUDE.md`, `AGENTS.md` and the rest) before it writes anything;
  `-y` answers yes. Ctrl-C at the question stops with nothing written (exit
  130). With no terminal, as when an AI agent runs it, it takes the default
  and says so; `uninstall` with no terminal takes no:

  ```
  No terminal: taking the default (yes). Use --no-instructions to skip them.
  No terminal: taking the default (no). Use --yes to go ahead.
  ```
- The MCP entry and every hook run the copy of code-review-graph that ran
  `install`: `poetry run code-review-graph` or `uv run code-review-graph`
  inside a Poetry or uv project, `uvx code-review-graph` only when `install`
  itself ran from `uvx` or a `uv tool` install, and otherwise
  `<python> -m code_review_graph`, with the full path of the Python that ran
  `install`. Hooks name that Python (or `uvx`) and fall back to
  `code-review-graph` on PATH when it is gone, as on a teammate's machine.
  Run `install` again after you move or upgrade the package.
- `install` keeps a record of the files it writes inside the repository in
  `.code-review-graph/install-manifest.json`, also with `--data-dir` or
  `CRG_DATA_DIR`. `build` and every review leave out the listed files while
  Git does not track them, so your first review shows your change, not
  `.mcp.json` and hook scripts. Once you commit one, it counts like any other
  file. `uninstall` takes each file it removes or edits off the record, and
  deletes the record when it is empty. A listed file Git does not track that
  holds nothing once our entries are gone, such as `{"mcpServers": {}}`, is
  removed with the empty folders `install` made, not left behind.
- `doctor` changes nothing. It checks every `code-review-graph` on PATH and its
  version, the version each MCP entry in this repository's config files runs
  (read from a `uvx` pin or `uv tool list`, never by starting `uvx`), the
  graph's schema, whether it was built at HEAD and whether the last build
  finished, and the git and editor hooks and the copy each one runs. Each
  failed check has a `Fix:` line. It ends with `All good.` (exit 0) or a count
  such as `1 problem found.` (exit 1), so it also fails when there is no graph
  yet.
  See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `build` ends with a next step. A build that finds no files prints a warning
  on stderr instead:

  ```
  Next: code-review-graph detect-changes --brief, or ask your assistant "Review my changes".
  Warning: no source files found under <root>. Is this the project root?
  ```

  A new graph logs one line, `Created graph database (schema v15).`, not
  every migration step. Upgrading an older graph logs each step, and one
  coming from below schema 15 then logs one line asking for one `build`.
- `visualize` without a seed exports the whole repository, and falls back to
  community (or file) bubbles once the rendered graph passes 3000 nodes or
  9000 edges. With `--seed-symbol`, `--seed-file`, `--seed-changed`,
  `--seed-flow` or `--path-from/--path-to` it exports a neighbourhood instead:
  the nodes within `--depth` hops of the seed and the edges among them, with
  everything else left out of the payload rather than drawn dimmed. A seeded
  page always uses the full renderer and never aggregates.
- A hop is any edge other than `CONTAINS`, such as `CALLS`, `IMPORTS_FROM`,
  `INHERITS`, `REFERENCES` or `TESTED_BY`. `CONTAINS` is structural and is not a hop, so a
  depth-2 neighbourhood of one function does not swallow its whole module; the
  containing `File` nodes are still shipped so clustering and collapse work.
- `--render-depth` (default 1) is how many hops are drawn on open. Clicking a
  node on the edge of what is drawn reveals its next ring, `+1 hop` reveals a
  whole ring, and both read from the payload already in the page.
- `--path-from A --path-to B` highlights the shortest path through `CALLS`,
  `IMPORTS_FROM` and `INHERITS`, following edge direction where one exists and
  falling back to an undirected route otherwise. The page says which it used.
- The default output is still a single self-contained `graph.html` you can
  email. `--sidecar` moves the payload into `graph.data.js` next to it, which
  is only worth it for a large neighbourhood.
- `--max-nodes` (default 1500) is a hard cap on the nodes in the payload, not
  a hint. The outermost hop goes first; once the outer hops are gone the seed
  set itself is trimmed, best-connected first by whole-graph degree, ties
  broken by name so two runs of the same command agree. A `--seed-changed`
  run over a large review is exactly the case where the seed set is the large
  thing, so the command prints how many seed nodes it dropped and the page
  says so too. The nodes on a `--path-from/--path-to` answer are placed
  first and are never dropped; a `--max-nodes` below the path length is
  refused rather than half-answered.
- Flag combinations that cannot mean anything are refused, not ignored:
  `--seed-changed-base` without `--seed-changed`; `--depth`, `--render-depth`
  or `--max-nodes` without a seed; any seed or tuning flag with a
  non-`html` `--format`; `--sidecar` with a non-`html` `--format`;
  `--mode community` or `--mode file` with a seed (they aggregate, which is
  the opposite of a neighbourhood); and a `--render-depth` above `--depth`.
- `install` (with `--platform all`, the default, or `claude-code`, `codex` or
  `qoder`) appends a Git `pre-commit` hook that runs `update --quiet` and then
  `detect-changes --brief` before each commit, so a commit shows only the risk
  summary. The hook skips linked worktrees unless
  `CRG_HOOK_WORKTREES=1` is set, so a worktree does not build a second graph
  for another branch.
- `install` also appends `post-checkout`, `post-merge` and `post-rewrite` Git
  hooks that run `update --quiet` after a branch switch, a merge, a rebase and
  `commit --amend`, with the same worktree rule. An existing hook written in a
  language other than shell is left alone, `--no-hooks` skips all four, and
  `uninstall` removes them.
- For an empty or incomplete graph, run `code-review-graph build`. See
  docs/TROUBLESHOOTING.md, "Empty or incomplete graph".
- `context-history` reads a log that is off by default. Set
  `CRG_CONTEXT_HISTORY=1` where the MCP server or the CLI runs to turn it on.
  See [USAGE.md](USAGE.md#context-history).

## Standalone Daemon CLI (`crg-daemon`)

`crg-daemon` is installed with `code-review-graph` and mirrors the
`code-review-graph daemon` subcommands:

```bash
crg-daemon start [--foreground]       # Start the multi-repo watch daemon
crg-daemon stop                       # Stop the daemon and all watcher processes
crg-daemon restart [--foreground]     # Restart (stop + start)
crg-daemon status                     # Daemon status, repos, and process liveness
crg-daemon logs [--repo ALIAS] [-f] [-n N]  # Tail daemon or per-repo log files
crg-daemon add <path> [--alias NAME]  # Add a repository to watch.toml
crg-daemon remove <path_or_alias>     # Remove a repository from watch.toml
```

### Configuration

The daemon reads `~/.code-review-graph/watch.toml` (or `$CRG_HOME/watch.toml`):

```toml
[daemon]
session_name = "crg-watch"   # logical daemon name
log_dir = "/home/user/.code-review-graph/logs"   # absolute path; ~ is not expanded (default: ~/.code-review-graph/logs, or $CRG_HOME/logs)
poll_interval = 2            # seconds between config file polls

[[repos]]
path = "/home/user/project-a"
alias = "project-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

The daemon spawns one `code-review-graph watch` child process per repo with
`subprocess.Popen`. It polls the config file and starts or stops children as
repos are added or removed. A health check every 30 seconds restarts dead
watchers. No tmux or screen is needed.

## MCP Tools

Every `repo_root` parameter is optional and auto-detected from the current
directory when omitted. The one exception is `serve --http --multi-repo`, where
every call must pass the absolute path of a registered repository (see Server
below).

Every tool carries `readOnlyHint: true` except the five that write:
`build_or_update_graph_tool`, `run_postprocess_tool`, `embed_graph_tool`,
`apply_refactor_tool` and `generate_wiki_tool`. A client can use the hint to
skip its approval prompt for the read-only ones.

Where a tool takes `base`, a local or remote branch ref (for example
`origin/main`) resolves to its merge base with `HEAD`, so the diff covers only
this branch's own commits. Commit ids and revision expressions such as `HEAD~3`
are used as given. If Git cannot find the merge base (shallow clone), the ref
is used as given.

### Core Tools

#### `build_or_update_graph_tool`
```
full_rebuild: bool = False           # True re-parses every file
repo_root: str | None
base: str | None = None              # Diff base; None means the commit the graph was last built at
postprocess: str = "full"            # "full", "minimal" (signatures + FTS), or "none"
recurse_submodules: bool | None      # None falls back to CRG_RECURSE_SUBMODULES
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None          # Exact model for embedding_provider
```
If some files fail to parse, `status` is `"partial"` and the `summary` names
them. Their previous graph rows are kept.

One write runs per repository at a time. When another write to the graph is
running, this tool, `run_postprocess_tool` and `embed_graph_tool`
return at once with `status: "contended"`, `skipped: true` and the `holder_pid`;
retry once it ends. An embedding refresh on a graph with no embeddings yet embeds
nothing and sets `embeddings_refresh_skipped: true`; run `embed_graph_tool` once
first.

#### `run_postprocess_tool`
```
flows: bool = True
communities: bool = True
fts: bool = True
repo_root: str | None
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None
```

#### `get_minimal_context_tool`
```
task: str = ""                       # What you are doing
changed_files: list[str] | None      # Auto-detected from VCS when omitted
repo_root: str | None
base: str = "HEAD~1"
```
Answers `status: "not_ready"` with `reason` `missing_graph`, `empty_graph` or
`stale_graph`, and suggests `build_or_update_graph_tool`. `stale_graph` means
the graph was built at another commit and differs from it. A graph the
pre-commit hook updated just before a commit counts as current: when every file
changed since the graph's commit has the same content in the graph, nothing is
stale.

Every `next_tool_suggestions` list, here and in the other tools, names tools as
the server registers them, ending in `_tool`.

#### Scope of the change tools

`detect_changes_tool`, `get_review_context_tool`, `get_impact_radius_tool`,
`get_affected_flows_tool` and `pr_blast_radius_tool` follow one rule. A call that names no `pr`,
`head`, `base` or `changed_files` reviews the **last commit**: `HEAD~1..HEAD`, or the whole commit
(from the empty tree) in a one-commit repository. This is also what a shared `serve --multi-repo`
server, which holds only each repository's default branch, answers. The reply says so:

```json
{
  "scope": {"kind": "last_commit", "base": "<sha>", "head": "<sha>", "files_considered": 33,
            "reason": "no pr/head/base/changed_files given; reviewing the last commit",
            "hint": "To widen or change the scope, pass pr=<number>, ..."},
  "note": "Scope: last commit (<sha>..<sha>, 33 file(s)); no pr/head/base/changed_files given; ..."
}
```

`files_considered` shows the size of a large last commit; the usual `max_results`, `max_files`,
`max_flows` and `detail_level` limits still bound the payload. To widen the scope pass `pr`, `head`
(with `base` for a stacked pull request), `base` for a larger range such as `base="HEAD~20"`, or
`changed_files`. A named change reports `scope.kind: "explicit"` with a `source` (`changed_files`,
`base`, `pull_request` or `head`) and no hint. When no commit range can be resolved (no git
history, or no commit yet) the reply is `status: "error"` and the message carries the same hint.

#### `get_impact_radius_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
max_depth: int = 2               # Hops in graph
max_results: int                 # Nodes to traverse to; default CRG_MAX_IMPACT_NODES, or 500
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard", "minimal", or "compact"
max_files: int = 25             # Compact-mode display limit (hard ceiling 200)
resolution: str = "all"          # "all" or "direct" (only calls bound to a node)
cursor: str | None               # next_cursor of the previous reply (see Paging)
```
An impacted node that calls or references the changed code in one hop carries
`call_site` (`line`, plus `file` only when the call is written outside the node's
own `file_path`) and `call_site_count` when there is more than one.
`unresolved_call_sites` counts call sites that name a changed symbol but were
never bound to it, so an empty radius is not read as proof of absence.
Changed files the graph has no nodes for are listed in `unmatched_files`, and
when none of the changed files is in the graph, `risk` is `"unknown"`.
Responses may include estimated `context_savings` metadata.

Use `detail_level="compact"` when the next step is opening affected files rather
than inspecting individual call sites. It returns ranked repository-relative
`changed_files` and `impacted_files`, with `changed_file_count`,
`changed_files_omitted`, `impacted_file_count`, `impacted_files_omitted`,
`total_impacted`, `truncated`, and `unresolved_call_sites`. `unmatched_files`
(with `unmatched_files_total`) lists the changed files the graph has no nodes for.
Paths outside the repository keep their absolute identity.
Files follow the graph's best-first node ranking. Counts describe the traversed
radius; `truncated` remains true when traversal or a file list was bounded.
The mode omits node/edge records and repeated qualified names. `max_files` limits
each displayed file list, independently of the `max_results` traversal budget.
Standard output and its existing limits are unchanged; minimal output remains a
summary without file paths.

The CLI exposes the same controls:

```bash
code-review-graph impact --files src/auth.py --detail-level compact --max-files 20
code-review-graph impact --files src/auth.py --detail-level minimal
```

#### `query_graph_tool`
```
pattern: str    # callers_of, references_to, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, next_states_of, previous_states_of,
                # triggers_of, triggered_by, publishers_of, listeners_of, advises, advised_by,
                # handlers_of, endpoints_for, requests_of, requesters_of, consumers_of,
                # file_summary, path_to, external_targets, endpoints
target: str     # Node name, qualified name, or file path (handlers_of: also a route;
                # endpoints: an optional substring of "<METHOD> <route>")
repo_root: str | None
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int | None          # Default 100, or 5 in minimal mode; an explicit value is honoured in both
limit: int | None                # Alias of max_results; wins when both are given
resolution: str | None           # "all", "direct", or "unresolved"; omitted: callees_of "direct",
                                 # callers_of / references_to "all"
hide_unresolved: bool = False    # callees_of: also hide bare-name and JDK/stream/builtin calls
to: str | None                   # path_to only: the node or file to reach
cursor: str | None               # next_cursor of the previous reply (see Paging)
direction: str | None            # endpoints only: "inbound", "outbound" or "simulator"
strict: bool = False             # handlers_of / references_to: keep the "ambiguous" answer
```
**Step Functions patterns.** `next_states_of` and `previous_states_of` take a state. With
`detail_level="minimal"` the names come with a compact `edges` list, one `{target, via, condition,
line}` per state (`conditions` when several Choice rules reach it); `standard` keeps the full edge
rows. `children_of` on a `*.asl.json` file lists the states of its machine (nested ones too, in
file order; several machines are listed with `state_count`) and says so in `note`;
`children_of` on `<file>::<machine>` still lists the top-level states.

**`endpoints`** lists `Endpoint` nodes as one line each,
`METHOD route owner file:line dir` (space separated, `dir` is `in`, `out` or `sim`), sorted
by route, with `direction_counts` over everything the `target` substring matches, whatever its
direction, and `files_under`, the directory the page's files share (the rows give paths below it).
It pages with `max_results` and `cursor` like the other patterns; 100 rows are about 7 KB. The
direction is not stored in the graph. It is derived at query time and the reply's
`direction_signal` repeats how: `outbound` is an endpoint whose `role` is `client` (a Feign or
JavaScript client declaration) or the target of a `REQUESTS` edge; `simulator` is a server
route with a `__` or `sim`-prefixed path segment, or `simulator` in its class name or path;
every other server route is `inbound`. `CRG_SIMULATOR_PATHS` (comma separated, case-insensitive
substrings of the route) replaces the path-segment test. `RestClient`, `WebClient` and
`@HttpExchange` call sites are not stored as endpoints, so they never appear.

**Names that match several nodes.** For `handlers_of` and `references_to`, a name that is the
exact name of several nodes no longer stops at `ambiguous`. The best match answers (exact
qualified name, then the `file_hint` file, then non-test code, then kind: `Class` over `Function` for
`references_to`, a node that serves an endpoint for `handlers_of`, then the higher in-degree) with its
rows, and the reply adds `ambiguous: true`, `resolved_to` and up to ten `alternatives`, each with
`qualified_name`, `kind`, `file` and a one-line `reason` it ranks lower. A name that matches only
fuzzily is never promoted, and `strict=true` returns the `ambiguous` candidate list as before. A
name that exactly one node has resolves directly, as it always did. `handlers_of` also takes a
route: `POST /v1/items`, `/v1/items/{id}`, `GET /items/{itemId}`. Path variables (`{id}`, `:id`,
`<id>`) are equal, case and a trailing slash do not matter, a route that ends with the given path
on a segment boundary matches when the full one does not, and the reply lists
`matched_endpoints`.
`requesters_of` takes the same route targets and matches Endpoint nodes only (outbound client endpoints
included), so a route no longer resolves to classes or methods that share its words; with several matching
endpoints each row names its `endpoint`, and a route no endpoint has is `not_found`.

**`callers_of` on a shared name.** A name several nodes have (two classes called
`VerticalRefreshMgr`, a method every subclass defines) still answers `ambiguous` with no rows, but
the answer can no longer be read as "no callers": the summary starts `AMBIGUOUS, not 'no callers'`,
the summary gives the `direct_callers` of the top candidates (distinct callers bound to it by a call
edge; bare-name matches are not counted), standard-mode candidates carry `direct_callers` and the
reply adds `total_direct_callers`. In minimal mode the rows stay two keys, with the counts in the
summary.
`file_hint` may also be written inside `target` as `path/File.java::name` when no node has that
exact qualified name: the part before `::` is used as the file hint when it looks like a path (a
separator or an extension), so `Foo::bar` is untouched.

**A route needs no `repo_root`.** `handlers_of` with a route (`POST /v2/respondents/avmap`)
names an endpoint, not a repository. On a `--multi-repo` server `repo_root` may be omitted, and
anywhere `repo_root="*"` asks every registered repo that has a graph. The reply lists
`repos_matched` (repo, handler count, `matched_endpoints`), tags each row with its `repo`, caps rows
at `max_results` (default 20, at most ten per repo; `results_omitted` counts the rest) and counts
`repos_searched`, `repos_without_graph`, `repos_unreadable` and `repos_skipped` (call budget). It
has no `edges` and no cursor; pass `repo_root` for one repo's full answer. A name that is not a
route, and every other pattern, still needs one repo and keeps the `repo_root is required` error.

**Java importers.** Java imports are by class, so `importers_of` on a Java file path reads the
classes the file declares: importers the build bound to the file, imports that kept the
dotted class name (another source root, a nested or static member import), and `pkg.*` wildcards.
Rows carry `import_level` (`class`, or `package_wildcard`), and the reply adds
`importers_level: "class"`, `java_classes` and a `note`. Same-package users write no import and
are not importers.

`detail_level="minimal"` drops `edges` and shows 5 rows when `max_results` is omitted; an explicit
`max_results` is honoured in minimal mode too. Whenever rows are cut, `results_omitted` counts them
and `paging_hint` says how to read the rest (`next_cursor`, or a larger `max_results`). In standard
mode `edges` are capped with the rows, and for `callees_of` only the first edge of each target
keeps its `ambiguous_targets` / `unresolved_targets` list, as its row does (the `*_target_count`
fields stay on every edge).

`callers_of`, `callees_of` and `references_to` return one row per call site, not
one per node. Each row carries `call_site` (`line`, plus `file` only when the
call is written outside the row's own `file_path`), and rows whose target was
matched by bare name alone carry `target_resolution: "unresolved"`. The response
adds `distinct_nodes` and a `resolution_split` of the whole answer. When some of
the rows are only name matches and `resolution` is `"all"`, a short `confidence`
sentence says how many. `callees_of` answers `"direct"` when `resolution` is omitted, because a
callee list is mostly bare-name noise (`map`, `stream`, `get`; on Python nearly every call is only
name-matched). The reply never hides that silently: the summary says how many name-matched call
sites were hidden (and how many of them are JDK, stream or builtin names), `hidden_call_sites`
(`unresolved`, `well_known`) carries the counts, and a zero-row answer says it is not "no calls".
Pass `resolution="all"` for the earlier behaviour. `hide_unresolved=true` hides every bare-name
call site and every bare call to a well-known name even under `resolution="all"`. Call sites
are ordered so every distinct node appears before any node's second call site,
so truncation never costs a caller. Within that order, calls bound to a node come
before bare-name guesses.

`publishers_of` and `listeners_of` rows carry `call_site` (`line`, plus `file` when it differs from the row's
`file_path`) in every detail level, `minimal` included.

A target written `external:<kind>:<name>`, such as `external:aws-lambda:order-validator`
or `external:github-actions:actions/checkout`, names code outside the repository. No node
has that name, so `callers_of`, `references_to` and `importers_of` read the edges that end
there (calls, references and imports respectively) and list who points at it. A target
nothing points at gives an empty answer, not `not_found`. The other patterns do not read
external targets.

A full name that one real symbol has resolves to it, even when tests or test
helpers share the name; the summary says how many it skipped. When two real
symbols share the name, `status` is `"ambiguous"` and the response lists
qualified names to pick from (five short rows in minimal mode).

`path_to` answers how `target` reaches `to`, with one row per hop through calls,
references, inheritance, imports and containment. It picks the route whose weakest
hop is strongest, and the shortest of those. Each hop's `certainty` says how it was
bound: `direct` to that node, `unique_name` through a bare name only one symbol
has, or `shared_name` through a name several share. `certainty` and `weakest_hop`
on the response give the weakest one, which is the hop to check first. How a hop
was bound ranks first and the edge's stored `confidence` only breaks ties, since
graphs built before #984 was fixed store 1.0 even on an edge that names no node.
In standard mode a hop shows `confidence` when it is below 1.0. When there is no
route, `results` is empty, and `reverse_route_exists` is set if one runs the other
way. If the search stops at its visit limit first, `search_incomplete` is set
instead, as a route may still exist. Callbacks and dynamic dispatch are not in the
graph, so no route does not prove there is none. An unclear end comes back as
`not_found` or `ambiguous`, with `endpoint` saying which. Minimal mode names each
hop by its qualified name from the repository root, and adds `file` only when the
call is written outside the source's own file.

`tests_for` rows that are guesses carry `inferred_by`, in minimal mode too:
`naming_convention` for a test found by its name, and `ambiguous_call` for a PHP
test whose call could mean this method or a same-named one in another class the
test imports, as with `app(Service::class)->run()`. The summary says how many
rows are guesses. A guess is never counted as coverage.

`tests_for` lists direct tests first, then indirect ones by path length. Each
indirect row carries `hops` (CALLS hops on the route), and a test of something
the target merely calls carries `inferred_by: "callee_test"` and `via`, the callee
it covers: that shows the target runs tested code, not that it is tested. A test
that reaches the target through test helpers is not marked. Only resolved edges are
followed; a bare name no node answers to is never walked, and the walk is at most
one CALLS hop for callee tests.

#### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200    # Capped at 500
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int = 100           # Graph nodes per list (max 100) and edges (max 150)
max_files: int = 25              # Files listed and given snippets (max 200)
cursor: str | None               # `context.next_cursor` of the previous reply
```
`changed_files` is ordered by risk score (highest first, per file in
`context.file_risk`), and both `max_files` and the shared 800-line snippet
budget are spent in that order. The budget buys whole changed regions -- the
diff hunks, each widened to its enclosing definition when that definition is
short enough to read whole -- granted round-robin across the ranked files, so
a file with one small change costs one small grant and a file with six hunks
gets six turns. No single file may hold more than 40% of the budget.
`context.source_regions` reports `shown`, `total`, and an `incomplete` map of
the files whose regions did not all fit. Each list reports its untruncated
`*_total`, and `context.truncated` / `context.source_truncated` mark any cut.
Changed files the graph has no nodes for are listed in `unmatched_files`, and
when none of the changed files is in the graph, `risk` is `"unknown"`.
Responses may include estimated `context_savings` metadata.
Only the risk-ranked `changed_files` are paged (see Paging): each page brings its
files with their `file_risk` and snippets. `impacted_files`, the `graph` lists and
`review_guidance` describe the whole change, so they are on the first page only.

#### `traverse_graph_tool`
```
query: str
depth: int = 3                  # Clamped to 1-6
mode: str = "bfs"               # "bfs" or "dfs"
token_budget: int = 2000        # At most 10000 approximate tokens; the walk stops there
repo_root: str | None
cursor: str | None              # next_cursor of the previous reply (see Paging)
```
`truncated` means the budget ran out before the walk did.

#### `semantic_search_nodes_tool`
```
query: str           # Search string
kind: str | None     # File, Class, Function, Type, Test, YamlPath, DocSection,
                     # Variable, Output, JsonPath
limit: int = 20      # At most 100; at most 5 with detail_level "minimal"
repo_root: str | None
model: str | None    # Embedding model (falls back to provider-specific env vars)
provider: str | None # local, openai, google, minimax, voyage
detail_level: str = "standard"
include_source: bool = False     # Attach each hit's source lines
expand_neighbors: bool = False   # With include_source: also the top hits' callers and callees
max_source_lines: int = 60       # Lines per snippet (1-60)
cursor: str | None               # next_cursor of the previous reply (see Paging)
```
Search ranks three times what it shows. YamlPath, JsonPath and DocSection hits score half
(`_CONFIG_RANK_FACTOR`) unless the query names configuration: a `$` path, a file name such
as `.yml` or `.json`, a dotted key such as `spring.datasource.url`, or one of those kinds in `kind`. `total` and `result_count` count the
matches it ranked, `results_omitted` those not shown, and `total_is_lower_bound`
says there may be more.

A `ConfigProperty` hit (standard detail) carries `value`, plus `value_truncated` and `doc_profiles` when present: the
literal stored at index time, at most 200 characters. Values of secret-looking keys, credentials, tokens and long
random strings are dropped at parse time and filtered again on the way out, so they never appear. `query_graph_tool`
`file_summary` rows of a configuration file carry the same fields.

`include_source=True` lets you answer from the reply instead of opening the
files. All snippets share one budget of 240 lines, or 700 with
`expand_neighbors=True`, which adds up to four callers and callees for each of
the top three hits. A cut snippet carries `source_truncated`. Source is not
attached when `detail_level="minimal"`.

`kind` is applied before the hits are cut. Test code ranks below production
code unless the query asks about tests (`kind="Test"`, or a word such as
"test", "spec", "fixture" or "mock"). JSON ranks below code unless you ask for
it (`kind="JsonPath"`, a `$` path or a `.json` name). Liquid `Output` nodes are
found only with `kind="Output"`.

#### `embed_graph_tool`
```
repo_root: str | None
model: str | None    # Embedding model name
provider: str | None # local, openai, google, minimax, voyage
```
Local embeddings need `pip install "code-review-graph[embeddings]"`, and Google
needs `pip install "code-review-graph[google-embeddings]"`. The OpenAI-compatible,
MiniMax and Voyage providers use the standard library HTTP client. Every cloud
provider reads its key from environment variables (see the README).

#### `list_graph_stats_tool`
```
repo_root: str | None
```

#### `find_large_functions_tool`
```
min_lines: int = 50                # Minimum line count
kind: str | None                   # Default Function. One kind or a comma list: Function, Class,
                                   # File, Test, YamlPath, DocSection, JsonPath
file_path_pattern: str | None      # File path substring
limit: int = 50                    # At most 100; at most 25 with detail_level "minimal"
repo_root: str | None
detail_level: str = "standard"     # "minimal" gives fewer, smaller rows
cursor: str | None                 # next_cursor of the previous reply (see Paging)
include_tests: bool = False        # Also rank Test nodes and test, mock, story, generated files
```
`total` and `result_count` count every match before the cut. Without `kind` only functions and
methods are ranked: a File node is only its file's size and a Class that spans its file repeats
it. With `kind="File,Class"` a class that spans its whole file is dropped in favour of the File
node. Unless `include_tests` is true, Test nodes and nodes in test files, `mocks/`, `msw/`,
`fixtures/` and generated directories, `.stories.` files and baseline `.sql` dumps are left out.
With `detail_level="minimal"` the summary is one line; the hits are in `results` only.

#### `get_docs_section_tool`
```
section_name: str    # usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting
repo_root: str | None
```

### Flow Tools

#### `list_flows_tool`
```
sort_by: str = "criticality"  # criticality, depth, node_count, file_count, name
limit: int = 50               # Flows per page (max 200)
kind: str | None              # Entry point kind (e.g. "Test", "Function")
repo_root: str | None
detail_level: str = "standard"   # "minimal": name, qualified_name, file, criticality, node_count,
                                 # entry_kind, group_size
cursor: str | None            # next_cursor of the previous reply (see Result Bounds)
entry_kind: str | None        # http, scheduler, queue_listener, event, main or other
group_duplicates: bool = True # merge flows with the same call tree below the entry
```
Each flow row carries `qualified_name` and `file` of its entry point (relative to
the repository root), so two flows with the same name can be told apart.

Flow ids are row numbers: a reindex renumbers them (a flow that was #323 became #1). Every flow in
`list_flows_tool`, `get_flow_tool` and `get_affected_flows_tool` (both detail levels) therefore also
carries `flow_key`, its entry node's qualified name relative to the repository
(`src/Foo.java::Foo.bar`), and `file`; `get_flow_tool(flow_key=...)` finds the flow by it. Both are
read from the entry node at query time, so they need no rebuild and the index fingerprint is unchanged.

A Java flow is named `Class.method` (`OrderController.list`), not by the bare method name.
Flow ids are unchanged; `get_flow_tool` still finds a flow by its bare method name. Each flow
has an `entry_kind`: `http` (a route handler; `route` lists `GET /path`), `scheduler`
(`@Scheduled`; `schedule` lists the timing attributes as written, and for a property
placeholder the key and default, never the configured value), `queue_listener` (a listener
annotation, or a worker that polls a queue through an SDK receive call; `queues` lists the
targets, each with `via`: `listener` or `poll`), `event` (`@EventListener`), `main` or `other`.
Flows whose call tree below the entry is the same set of nodes (a public and an admin
controller calling one service method) are one row: the most critical stands for the group and
`entry_points` lists every entry (`flow_id`, `name`, `entry_kind`, `route`), `group_size` counts
them, and `total` counts groups. When merging removed flows, `total_before_grouping` and the summary
(`329 execution flow(s) (594 before merging duplicates; ...)`) give the unmerged count. Pass
`group_duplicates=false` for one row per entry.
`get_flow_tool` lists the other flows with the same call tree under `same_call_tree_as`.

#### `get_flow_tool`
```
flow_id: int | None          # Database ID from list_flows_tool
flow_key: str | None         # Stable key from any flow row (entry node qualified name); used when flow_id is not given
flow_name: str | None        # Name to search (partial match); ignored when flow_id or flow_key is given
include_source: bool = False # Source snippet per step
repo_root: str | None
max_steps: int = 50          # Steps per page (max 200); flow.total_steps reports the full count
max_source_lines: int = 400  # Per reply, shared across its steps; capped at 2000
cursor: str | None           # next_cursor of the previous reply (see Paging)
```

#### `get_affected_flows_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
base: str = "HEAD~1"
repo_root: str | None
detail_level: str = "standard"   # "standard" full step details, "minimal" metadata only
max_flows: int = 50
```
Standard mode carries a full `steps` list per flow, so it caps visible flows
at 25 and spends a shared 400-step budget across them; minimal mode caps at
500. `total` reports the untruncated flow count. See #849.

### Community Tools

#### `list_communities_tool`
```
sort_by: str = "size"    # size, cohesion, name
min_size: int = 0
repo_root: str | None
detail_level: str = "standard"
max_results: int = 50    # Communities per page (max 200)
max_members: int = 10    # Member names per community in standard mode (max 25)
cursor: str | None       # next_cursor of the previous reply (see Result Bounds)
include_config: bool = False   # also list communities made only of JSON/YAML documents
```
`size` reports the true member count; `members_truncated` marks a cut member list.
Communities made only of JSON and YAML document nodes are left out unless
`include_config` is set, before ranking, `total` and paging; `config_communities_hidden`
reports how many were left out. A large group of data files is cut into slices named
after what they hold (`votes-json-early-vote-k0-to-k199`, `taskdefs-json-api-to-worker`).

#### `get_community_tool`
```
community_name: str | None   # Name to search (partial match); ignored when community_id is given
community_id: int | None     # Database ID
include_members: bool = False
repo_root: str | None
max_members: int = 25        # Member entries per page (max 25)
cursor: str | None           # next_cursor of the previous reply (see Paging)
```

#### `get_architecture_overview_tool`
```
repo_root: str | None
detail_level: str = "minimal"    # "minimal" (default) or "standard"
max_results: int = 100           # Cross-community rows and warnings (max 200)
max_members: int = 10            # Member names per community in standard mode (max 25)
cursor: str | None               # next_cursor of the previous reply (see Paging)
include_config: bool = False     # also include JSON/YAML-only communities and their coupling
```
`cross_community_edges_total` reports the untruncated row count. Without
`include_config`, communities made only of JSON and YAML documents, and the coupling
rows and warnings about them, are left out; `config_communities_hidden` reports how
many.
Minimal responses may include estimated `context_savings` metadata.

### Graph Health and Architecture Tools

#### `get_hub_nodes_tool`
```
top_n: int = 10                  # Hubs per page (max 100)
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns name, kind, total_degree
cursor: str | None               # next_cursor of the previous reply (see Result Bounds)
include_tests: bool = False       # Tests are left out before ranking, totals and paging
rank_by: str = "resolved"        # "resolved" orders by resolved_degree, "total" by ranking_degree
exclude_leaf_primitives: bool = False  # skip nodes that build on no other node
include_config: bool = False     # JSON/YAML/output/property nodes are left out before ranking
```
Config document nodes (the `$` root of a JSON file touches every key beneath it) are
left out of the ranking unless `include_config` is set. A cursor made with one value
is refused for the other. Degree counts only edges that name a node. A call or reference whose target is a
bare name no node answers to (`get`, `parse`, `main`) is left out, as the
`unresolved` calls of `query_graph_tool` are, so a symbol does not rank for the
calls it makes to names it could not pin. Each row's `resolution_split` gives
`direct` (the edges counted) and `bare_name` (the edges left out).

Each row also carries `resolved_degree` (counted edges that are not name matches),
`unresolved_degree` (bare-name edges plus calls a resolver bound by name alone to a name many
symbols define) and `resolved_share`, `resolved / (resolved + unresolved)`. By default
(`rank_by="resolved"`) hubs are ordered by `resolved_degree`, so a generic name such as `insert`,
`put` or `execute` that only looks hot through name collisions drops below a symbol with fewer,
traced edges. `rank_by="total"` keeps the older order by `ranking_degree` (name-matched edges
damped). A cursor made with one value is refused for the other.

`exclude_leaf_primitives` skips nodes with no outgoing `CALLS` or `REFERENCES` to
an indexed non-test node. A design-system component that hundreds of files use
resolves correctly and still tells a reviewer little; the default keeps it.

#### `get_bridge_nodes_tool`
```
top_n: int = 10                  # Bridges per page (max 100)
repo_root: str | None
detail_level: str = "standard"   # "minimal": name, qualified_name, file, kind, betweenness
cursor: str | None               # next_cursor of the previous reply (see Paging)
include_tests: bool = False       # Tests are left out before ranking, totals and paging
include_config: bool = False      # JSON/YAML/output/property nodes are left out before ranking
```
Config document nodes are left out unless `include_config` is set; a repository with
nothing else gets them back with a `note`. `get_minimal_context_tool` names code
communities before config ones, and `get_suggested_questions_tool` asks about code
bridges and hubs only. Minimal rows give `qualified_name` and `file` relative to the repository root.
Calls to a bare name no node answers to are not paths, so they do not carry
betweenness.

#### `get_knowledge_gaps_tool`
```
repo_root: str | None
max_per_category: int = 15       # Entries per gap category per page (max 50)
detail_level: str = "standard"   # "minimal" drops file paths
cursor: str | None               # next_cursor of the previous reply (see Paging)
include_tests: bool = False       # Tests are left out before ranking, totals and paging
include_non_code: bool = False   # also report docs, lockfiles, tooling as gaps
exclude_data_types: bool = False # leave enums, records, Lombok/accessor-only classes out of untested_hotspots
```
Documentation sections (`DocSection`), lockfile entries (`package-lock.json`,
`.terraform.lock.hcl`, `uv.lock` and the like), Dockerfile stages and anything
under `.storybook` or `.github` are expected to be isolated or unreferenced, so
`isolated_nodes` and `untested_hotspots` leave them out unless `include_non_code`
is set. Config document keys and Step Functions (ASL) states and machines are left out of
`untested_hotspots` too (no test covers them by design), and the dunder methods of a class
(`__init__`, `__repr__`) out of `isolated_nodes`, the interpreter being what calls them. So are
constructors (a function named for the class that holds it): a
framework such as Guice or Spring calls them, so no call edge names them. A
constructor with no `CONTAINS` edge is still reported. A hotspot's `degree` counts only edges that name a node, as hub nodes do.
`summary` maps each category to its untruncated count. The categories are
`isolated_nodes`, `thin_communities`, `untested_hotspots`,
`single_file_communities` and `import_cycles`. Test nodes (kind `Test`, `is_test`, or
a symbol in a test file) are left out of every category unless `include_tests=true`, and a
community made only of tests is left out with them. Configuration-document nodes
(`JsonPath`, `YamlPath`, `Output`, `ConfigProperty`) are never listed as isolated nodes.

An `untested_hotspots` row is a node with at least 5 connections that `tests_for`
shows no covering test for: no direct test and none reaching it through test
helpers. A test of something it only calls does not count, so the two tools
agree. A community made only of JSON or YAML paths is a config document and is
not reported as `thin_communities` or `single_file_communities`.

`exclude_data_types=true` leaves data carriers (enums, records, Lombok or accessor-only Java classes, dataclasses,
data-only TS types) out of `untested_hotspots` before ranking, with the same meaning and the same default (`false`) as
on `get_hub_nodes_tool`; `data_types_excluded` counts them. A cursor made with one value is refused for the other.

An `import_cycles` row is one group of files that import each other: `size` is
how many files, `name` and `cycle` spell out one loop with its import lines, and
`load_time` says whether that loop can fail as the files load. `false` means every
loop has an import that runs late: inside a function, a dynamic `import()` or a
Ruby `autoload`. Imports that never run are left out: `if TYPE_CHECKING:` in
Python and `import type` in TypeScript, and `import { type X }` unless
`verbatimModuleSyntax` keeps it. Only imports that
name a file are followed, so Go packages and C# namespaces are not. Graphs built
before this release need `code-review-graph build` to tell these imports apart.

#### `get_surprising_connections_tool`
```
top_n: int = 15                  # Surprises per page (max 100)
repo_root: str | None
detail_level: str = "standard"   # "minimal": source and target (with qualified name and file), kind, score
cursor: str | None               # next_cursor of the previous reply (see Paging)
include_tests: bool = False       # Tests are left out before ranking, totals and paging
```
Structural containment is never a surprising connection: a CONTAINS edge, or any
edge between a node and a node that contains it, is left out of the rows and of
`total`. Rows carry `source_file` and `target_file`. A call made in several places
is one row per (source, target, edge kind), and a call to a bare name no node
answers to is not a connection. Degrees, which decide "peripheral-to-hub", count
only edges that name a node.

#### `get_suggested_questions_tool`
```
repo_root: str | None
include_tests: bool = False      # Ask about production code only unless true
```

`include_tests` applies to the five tools above. With the default `false`, test
nodes are removed from the candidate rows before ranking, so `total`, `next_cursor`
and the summary describe the filtered list; `get_surprising_connections_tool` drops an
edge when either end is a test. The flag is part of a cursor's scope: a cursor made with
one value is refused for the other.

### Change Analysis and Refactoring Tools

#### `detect_changes_tool`
```
base: str = "HEAD~1"
changed_files: list[str] | None
include_source: bool = False # Snippets share a 600-line budget
max_depth: int = 2
repo_root: str | None
detail_level: str = "standard"
max_results: int = 25        # Changed functions, test gaps, changed files (max 100)
max_flows: int = 20          # Affected flows embedded (max 200)
cursor: str | None           # `next_cursor` of the previous reply
pr: int = None               # Score a pull request's files (read from refs/pull/<pr>/head), new files included
head: str = None             # Or a ref/commit to read the changed files from; base then names the target branch
                             # Reply gains `pull_request`: commits, symbol totals, and (not in minimal) the added and
                             # removed names, `max_results` rows a page (max 100), with `symbols_truncated` set
                             # while rows remain. The same `cursor` reads the next page of functions and symbols.
                             # `pull_request.new_code` scores the production symbols the pull request adds (totals; and
                             # beyond minimal, scored rows with `coverage`, `reasons` and linked `tests`), and the
                             # overall `risk_score` becomes the higher of that and the changed symbols' (`risk_breakdown`)
                             # Config paths the pull request adds, changes or removes (YAML, JSON, Spring properties) are
                             # in `changed_yaml_paths` / `changed_json_paths`, each with `change`. A pull request already
                             # merged (head is an ancestor of the base) says so and sets `already_merged`.
```
Main tool for code review. Maps changed files to affected functions, YAML and JSON
paths, flows, communities and test coverage gaps, returning `changed_yaml_paths` and
`changed_json_paths` separately from function risk/test-gap scoring, plus risk scores
and review priorities.
Embedded flows carry per-flow metadata only; use `get_affected_flows_tool` for
step detail. `changed_functions_total`, `test_gaps_total` and `affected_flows_total`
report the untruncated counts. Responses may include estimated `context_savings` metadata.
Only `changed_functions` is paged (see Paging); a later page holds just those rows,
`risk_score` and the totals. The other lists are on the first page and stay bounded by
`max_results` and `max_flows`, so the first page can be `truncated` without a `next_cursor`.

When the diff only deletes lines, the code around the gap did not change, so it
is not listed as changed. The old version of the file is read instead: a
function or class that lost some lines is listed as changed, and one that is
gone is listed in `removed_symbols` (at most 25; `removed_symbols_total` has the
full count) with up to three of the callers, subclasses, importers or tests that
still use it (`used_by_total` has the full count). A removed symbol that is still
used breaks its users, so it gets a `risk_score` on the same scale as a changed
one (the flows it was part of, callers from other communities, test coverage,
security names, caller count) and counts towards the overall `risk_score`; one
that nothing uses scores 0. The summary names the ones still in use, and minimal
mode reports `removed_symbol_count`. Up to 100 files
are read per call (`CRG_MAX_DELETION_FILES`); the summary says when a file could
not be read. A deleted file is not covered.

#### `refactor_tool`
```
mode: str = "rename"         # "rename", "dead_code", or "suggest"
old_name: str | None         # (rename) Current symbol name
new_name: str | None         # (rename) New name
kind: str | None             # (dead_code) Function or Class
file_pattern: str | None     # (dead_code) File path substring
repo_root: str | None
max_results: int = 50        # Edits/symbols/suggestions returned (max 150)
detail_level: str = "standard"  # "minimal" keeps identifying fields only
cursor: str | None           # next_cursor of the previous reply (see Paging)
```
Truncating a rename preview truncates only the response. The stored preview
keeps every edit, so `apply_refactor_tool` applies the full set. Paging a rename
preview only reads: a later page issues no new `refactor_id` and never applies an
edit, so apply with the id the first page returned, before it expires.

`old_name` is matched exactly: a name, `Class.method` or a qualified name such as
`pkg/util.py::helper`. When a bare name belongs to several symbols, the first by
file and line is renamed and the preview's `qualified_name` says which; pass a
qualified name to pick another. A name that matches no symbol gives `not_found`.
The preview also edits the import and export lines that name the symbol. Each
edit carries the `column` where the old name starts and its `site` (`definition`,
`call`, `reference`, `import`, `export`, `attribute` or `__all__`), and applying
replaces the name only there. An edit that no longer matches the file is skipped
and counted in `edits_skipped`.

#### `apply_refactor_tool`
```
refactor_id: str             # ID from a prior refactor_tool call
repo_root: str | None
dry_run: bool = False        # Return a diff without writing files
max_diff_files: int = 25     # Per-file diffs in a dry run (max 150)
```

### Wiki Tools

#### `generate_wiki_tool`
```
repo_root: str | None
force: bool = False          # Regenerate all pages even if unchanged
```

#### `get_wiki_page_tool`
```
community_name: str
repo_root: str | None
max_chars: int = 20000       # Page content returned (max 80000)
```
`total_chars` reports the real page length; `truncated` marks a cut.

### Multi-Repo Tools

#### `list_repos_tool`
```
detail_level: str = "standard"  # Accepted for parity; every value returns the same payload
limit: int = 50              # Repositories per page (max 100)
cursor: str | None           # next_cursor of the previous reply, to read the next page
```

`total` counts every registered repository, in registry order. When more remain the reply carries
`next_cursor` (a later page has `offset`). A cursor is refused once the registry has changed.

#### `cross_repo_search_tool`
```
query: str
kind: str | None
limit: int = 20              # Results per repo
max_results: int = 50        # Merged results across searched repos (max 100)
repos: list[str] | None      # Aliases or folder names; default: every registered repo
cursor: str | None           # next_cursor of the previous reply (see Paging)
include_repo_list: bool = False  # Return repos_searched (names) even for more than 10 repos
include_fuzzy: bool = False  # Keep fuzzy/tokens rows (hidden when an exact hit exists; capped when none does)
match: str = "fuzzy"         # fuzzy | exact (exact: only exact, exact_ci and qualified rows)
```

The reply's `exact` is `"found"` or `"none"`, and the summary says `exact: none` when no row has the query as
its name. With `match="fuzzy"` (default) and no exact hit, `fuzzy` rows that share fewer than half of the
query's words are dropped and at most 10 are kept (an unmatched hyphenated name returned 600 rows, ~25 KB);
`fuzzy_dropped` and the summary say how many were hidden, and `include_fuzzy=true` returns them all.
`match="exact"` returns only exact rows and counts the rest in `fuzzy_dropped`.

`repos_searched_total` is the number of repos searched. `repos_searched` (their names) is returned only
for 10 repos or fewer, or with `include_repo_list=true`; it was ~2.5 KB on a 122-repo registry.

`repos` limits the search to named registry entries. Matching is exact and
case-sensitive; paths are not accepted. An alias match wins over a folder-name
match.

`limit` caps the rows of each repo. A repo that has more matches than `limit` is listed in `repos_capped`
(`repos_capped_total`); `total` is then a lower bound (`total_is_lower_bound: true`, `Found N+` in the summary) and
`truncated` is true even when there is no `next_cursor`: raise `limit` to see the rest. `max_results` still caps the
rows returned.

Scores are not comparable between repos, so results are ordered by match tier first,
then by each repo's local rank, with registry order as the tie-breaker. Every row has a
`match` field: `exact` (the name equals the query, case-sensitive), `exact_ci` (equal
ignoring case), `qualified` (the qualified name ends with the query, as in
`Class.method`), `prefix` (the name starts with the query), `tokens` (every word of the
query, split on non-alphanumerics and camelCase, is in the name) or `fuzzy`. A repo's
exact-name nodes are always kept, whatever `limit` is; those the ranked search did not
return carry `score` 0.

The call has a time budget, `CRG_CROSS_REPO_BUDGET_SECONDS` (default 300, `0` = no limit).
When it is spent no further repo is started: the ones not searched are listed in
`skipped_repos` (always listed in full; `skipped_repos_total` is the count and
`skipped_repos_truncated` is always false), `truncated` is set and the summary says so. The first repo always runs.
Up to `CRG_CROSS_REPO_WORKERS` repos (default 4, `1` = one at a time, at most 32) are
searched at once, each on its own graph connection; the rows are ordered the same way
whichever search finishes first.
The repos not searched still get an exact-name lookup (an indexed query, bounded to 30
seconds in all), so an `exact`, `exact_ci` or `qualified` match in one of them is returned;
only their `prefix`, `tokens` and `fuzzy` matches are missing. `exact_lookup_repos_total`
counts the repos that lookup reached.
A `cursor` made on such a partial answer is only accepted while the same repos are
skipped; otherwise it is refused and the call must start over.

Names that match no entry are returned in `unknown`. A name that matches
several entries selects all of them and is listed in `ambiguous`. Both lists
are capped at 20 entries; `unknown_total` / `ambiguous_total` and
`unknown_truncated` / `ambiguous_truncated` report the real counts, and the
summary reports the counts rather than echoing the names.

#### `cross_repo_requests_tool`
```
repos: list[str] | None      # Calling repos (aliases or folder names); default: every registered repo
service: str | None          # Only calls to this Feign service name (exact)
route: str | None            # Only calls whose route equals this or lies below it
status: str | None           # matched | ambiguous | case_mismatch | method_not_served | prefix_mismatch | no_route_match | no_target_repo | unmatchable
limit: int = 20              # Rows per calling repo
max_results: int = 50        # Rows overall (max 100)
origin: str | None           # feign | js | terraform | workflow | stepfunction | awssdk; default: feign and js
cursor: str | None           # next_cursor of the previous reply (see Result Bounds)
repo_root: str | None        # Shorthand for repos=[<folder name>] when repos is not given
include_repo_list: bool = False  # Feign/js: return repos_searched (names) even for more than 10 repos
path_prefix: str | None      # awssdk, stepfunction: only rows whose file is this repo-relative path or below it
source: str | None           # awssdk: only rows whose source symbol equals this, ends with it after "." or lies below it
machine: str | None          # stepfunction: only rows of this state machine (exact name)
include_tests: bool = True   # awssdk: False hides rows from test files; every row has is_test
```

`awssdk` rows carry `is_test` (a test directory or test file name). `include_tests` defaults to True, so counts
are as before; with `include_tests=false` the reply gives `tests_hidden` and the summary says how many rows were
hidden. `path_prefix`, `source`, `machine` and `include_tests` filter before `counts`, `total` and paging (they
reach rows a path-sorted page of 100 never got to) and a cursor is refused for different values. Each is refused
for an origin it does not apply to.

For the Feign and JS/TS origins `repos_searched_total` is the number of repos read and `repos_searched`
(the names) is returned only for 10 repos or fewer, or with `include_repo_list=true`. `skipped_repos` is
always listed in full.

A `matched` row whose `candidates_total` is above 1 also has `multiple_candidates: true` (the status value is
unchanged); the reply's `multiple_candidates_total` and its summary count those rows.

`origin="terraform"` lists the remote Terraform module sources (`git::...//modules/x`) the repos name and the
registered repo and folder each one resolves to. `service` filters by module repo name, `route` by module folder
prefix, and `status` is `matched`, `module_not_found`, `ambiguous` or `repo_not_registered`. Run it with
`service=<module repo>` to see which stacks use a module.

`origin="terraform"`, `"stepfunction"` and `"awssdk"` also return an `infrastructure` object (first page only; a page reached with `cursor` carries `infrastructure_note` instead) that relates each
selected repository to the infrastructure declared for it in any registered repository: ECR repositories, ECS
services and task-definition families, Terragrunt stack and Terraform module directories whose name equals the
repository's name or alias, and declared queues, buckets, topics and tables its code names (written out or
resolved from Spring configuration). Items carry the declaring repo, file, line and `matched_via`. `awssdk` rows
matched to a Terraform declaration carry `declared_by`. See [SERVICE_ALIASES.md](SERVICE_ALIASES.md).

`origin="awssdk"` names a call whose target is a Spring configuration key from the value on the repo's
`ConfigProperty` nodes (`${KEY:default}` and `${ENV_VAR}` placeholders that refer to another property of the same
repo are expanded; relaxed binding, so `queue-name`, `queueName`, `queue_name` and `QUEUE_NAME` are one key). The
row says `target_source: "config"`, `config_profile` (the default profile is preferred), `via` (the property and each
placeholder read), and `config_alternatives` (other profiles' names when they differ). A key that cannot be named
stays unresolved with `target_source: "config_key_unresolved"`, `placeholder` (the key), `config_defined` and
`unresolved_reason` (`not_defined`, `no_value`, `env_placeholder`, `profiles_differ`, `ambiguous`). A key that is
defined only in profile-bound documents whose values differ (no default profile) keeps the row unresolved by name
but is reported as `target_source: "config"` with `config_profile: "differs"`, `unresolved_reason: "profiles_differ"`
and the per-profile names in `config_alternatives`; a name already resolved per profile at build time
(`resolved_names`) also reports `target_source: "config"`. The Java extractors reach the key through one same-file
hop: a local variable, a no-argument method of the same class that only returns the getter or `@Value` field, a
resource-lookup wrapper such as `helper.getQueueUrl(props.getQueueName())`, a request variable built earlier in the
method, or a chained getter (`props.getDynamo().getTableName()`, kept only when the repo's configuration defines
`prefix.dynamo.tableName`). Method parameters and anything ambiguous yield no key. Values of
secret-looking keys, private keys, JWTs, tokens and long random strings are never stored. Config values appear on
nodes only after the repo is re-indexed (a full reindex follows from the new index fingerprint).

`origin="awssdk"` also lists RDS (`rds`), load balancers and target groups (`elasticloadbalancing`), ElastiCache
(`elasticache`) and OpenSearch/Elasticsearch (`es`), from SDK operations and from client connections that name a
host (JDBC/R2DBC URLs, Spring `application*` keys, Python, Java and JavaScript database, Redis and search clients,
AWS endpoint hosts in code). A row has `resource_kind`; a connection row has `operation` `connect`, `access`
`connect` and `host`, `port`, `database`, `engine`, `client`. The name is the identifier an AWS endpoint host
spells (`mydb.abc.us-east-1.rds.amazonaws.com` is `mydb`, `search-mydomain-<id>...es.amazonaws.com` is `mydomain`,
`name-123456.us-east-1.elb.amazonaws.com` is `name`); a custom DNS name, an `internal-` load balancer host, a
custom cluster endpoint or a variable gives an `unresolved` row (`unresolved_reason`, `placeholder`). A row is
`matched` only when a Terraform declaration of the same kind has exactly that name. Credentials in a URL are never
stored. A connection row has `evidence` (`client_call`, `connection_url`, `config_key`, or `hostname_only` for a bare
host with no client or endpoint key around it, which also has `confidence` 0.4); a host in a comment, docstring, log
call, sample config, test, fixture, mock or docs path makes no row.

A Python call whose client comes from a factory in another file of the repo (`_sfn.get_client().start_execution(...)`)
is listed when the factory returns, on every path and directly or through a module-level cache, a client of one literal
service (`boto3.client("stepfunctions")`, `boto3.Session(...).client("sfn")`, `make_client("stepfunctions")`). The row
has `client_inferred: factory`, `client_factory` (`<file>::<function>`) and `confidence` at most 0.5; a factory that
cannot be found or whose service is not a literal makes no row. When the resource argument of such a call is a plain
parameter of its function, the row is `unresolved` with `parameter`, `parameter_function` and `unresolved_reason:
name_is_parameter`, and each call of that function (same file, or a file that imports it) that passes the parameter by
keyword (`*_arn`, `arn`, `*state_machine*` names) gets a row of its own at its own line with `target_source: parameter`,
`parameter`, `bound_via` (`<file>::<function>(<parameter>)`), `arg_form` (`literal`, `constant`, `default_function`, `or`
or `conditional`), `confidence` 0.4 (0.3 for a default, `or` or conditional) and, as they apply, `default_from`,
`override_possible` (a side of an `or`/conditional was not readable, so the call may name another machine) and
`target_alternatives` (the branches name different machines; one row each, at most four). A development and a production
ARN of one machine give one target. One hop only: the callers of a function that itself passes a parameter on stay
`argument_not_static`.

When a state machine has two candidate owners (`ambiguous`) and exactly one of them deploys it (a Terraform resource or
declaration, or a Terragrunt unit naming it) while the others only hold its definition file (`*.asl.json`), the row is
`matched` to the deploying repo with `tie_break: deployed_declaration_over_definition_file`, `also_defined_in` (the
other repos) and `also_defined_via` (`{repo: how it defines it}`). Two deploying repos, or none, stay `ambiguous`; the
account id inside an ARN is not used. This is read-side only and does not change what a build writes.

Reasons on `origin="awssdk"` rows (status is never changed by them; `status=<reason>` filters and the reply has
`counts_by_reason` and a summary clause). A `not_found` row has `not_found_reason`: `no_owner_lookup` (the service has
no owner lookup: secretsmanager, cloudwatch; an SSM parameter or IAM role is looked up by its Terraform
`aws_ssm_parameter` / `aws_iam_role`, so a miss there is `not_declared_in_registry`), `test_double` (a mock-like name, a test file, or a `mock`/`mocks`/`msw`/
`__mocks__`/`fixtures`/`stubs`/`fakes` folder), `declared_name_dynamic` or `not_declared_in_registry`.
`declared_name_dynamic` describes the registry, not the row: the row's name can be a plain literal. It means some
registered repo declares a resource of that service's Terraform type (or an S3 wrapper module) whose own name could not
be read as a literal, so that unreadable declaration might be this name; `dynamic_declared_in` lists up to five such
repos. A literal that no registered declaration names and no unreadable declaration exists for is
`not_declared_in_registry`. An `unresolved` row always has `unresolved_reason`: `not_defined`, `no_value`,
`env_placeholder`, `profiles_differ`, `ambiguous` (configuration join), `non_aws_host`, `unrecognised_endpoint`,
`custom_endpoint`, `proxy_endpoint`, `serverless_cache`, `internal_prefix` (connection hosts), `no_resource_param`
(the call names no resource parameter), `name_is_parameter` (the resource argument is a parameter of the calling
function; its callers get their own rows), `argument_not_static` (a caller's argument for such a parameter cannot be
reduced to a name), and, when none of those applies, one derived from the row: `env_placeholder`
(an environment variable stands for the name), `config_key_not_resolved` (a configuration key does and no value could
be read), `constant_not_resolved` (a constant or field does), `host_not_literal` (a connection whose host is not
spelled out) or `name_not_literal` (a variable, parameter, call result or concatenation). `origin="stepfunction"`
`not_found_reason` values: `data_source_only`, `declared_via_module`, `source_without_unit`, `environment_cluster`,
`not_declared_in_registry`.

`origin="workflow"` does the same for GitHub Actions: each `uses: org/repo/path@ref` of a workflow or composite
action (a reusable workflow at job level, an action at step level) that names another repo is joined to the
registered repo called `repo` (the part after `org/`) and the file or folder `path` in its graph. Rows carry the
using repo, file and line, `target_repo`, `target_path`, `ref` and `kind` (`reusable_workflow` or `action`).
`service` filters by target repo name, `route` by path prefix, and `status` is `matched`, `path_not_found`,
`ambiguous` or `repo_not_registered`. Local `./` and `docker://` references and expression-valued ones are not
listed. An action at a repo's root has an empty path and is `matched` once the repo is registered. A path the
target graph does not index (an action folder outside `.github`) reads as `path_not_found`.

`origin="stepfunction"` lists what the Step Functions Task states of the repos call and the registered repo that
defines it. A target is a `REFERENCES` edge from a state to `external:aws-<service>:<name>`: a name written in the
definition, or one the deploying Terragrunt unit's replacement maps give a `${placeholder}` (`named_by` is
`definition` or `replacement_vars`). Rows carry the calling repo, file and line, `machine`, `state`, `target_kind`
(`lambda`, `ecs`, `sns`, `states`, ...), `target_name`, `target` (`kind/name`) and, when matched, `target_repo`.
A repo defines a target when it has a Lambda Terragrunt unit deployed under that name (exact), a Terraform
resource of the matching type labelled with it (`aws_lambda_function`, `aws_ecs_task_definition`, `aws_sns_topic`,
`aws_sfn_state_machine`, ...; case, `-` and `_` ignored), or, for a state machine, a definition file of that name.
`status` is `matched` (exactly one registered repo defines it, possibly the calling repo), `ambiguous` (several
do; they are in `candidates`) or `not_found` (no registered repo does, or it names it another way). `service`
filters by target name and `route` by kind (`lambda`) or `kind/name`. Run it with `service=<function name>` to see
which state machines run a Lambda. A placeholder that stands for a different name in each environment gives one
row per name.

Joins each outbound Spring Feign call, and each JavaScript or TypeScript HTTP
call, to the server endpoint it reaches in another registered repository. The join is computed on every call from the
graphs that already exist, opened read-only. Each repo's side of it (its client calls and its server endpoints, with
FastAPI and Flask router prefixes already joined) is worked out when its graph is built and kept in the graph's
`metadata` table under `endpoint_index`, so a call does not re-read every `Endpoint` node. A graph built before that, or
one whose `Endpoint` rows changed since, is read the old way and gives the same answer; the reply's `endpoint_index`
field says how many repos came from each (`stored`, `computed`), and an `update` or `build` refreshes a repo's index.
Each row has the
calling repo, file and line, the HTTP method, route and service name, a
`status`, and `candidates` (repo, file, line, method, route; at most five,
with `candidates_total`).

A page takes up to `limit` rows from each calling repo and keeps the first
`max_results` of them; `next_cursor` then continues each repo where it stopped,
so every call can be read. `counts` and `total` always cover every call. A
cursor is refused if the filters or any of the rows changed since it was made.

How a call is classified:

- `unmatchable`, with a `reason`, when the target cannot be known from the
  code: `service_unresolved` (the `@FeignClient` name is a `${...}`
  placeholder or absent), `url_from_argument` (a `java.net.URI` argument
  receives the request), `url_literal` (the client has a fixed `url`),
  `route_dynamic` or `placeholder_in_route` (part of the route could not be
  resolved). These are never guessed.
- The service name picks the target repository: the optional service alias file
  (`CRG_SERVICE_ALIASES_FILE`) first, then a registry alias or folder name, then a
  name in `repo-aliases.toml`, then a name that differs only in case, `-` versus
  `_` or repeated separators. That last step must reach exactly one repository.
  When none of these finds it, the names the repositories declare about themselves
  (Spring `spring.application.name`, ECS service, ECR repository) are compared
  after folding case, separators and one environment suffix. Every resolved row
  has `matched_via` (`explicit_alias`, `spring_application_name`, `ecs_service`,
  `ecr_repo` or `repo_name`). A name that reaches several repositories is
  `ambiguous` (`service_name_ambiguous`) with `candidate_repos`, never picked;
  otherwise the status is `no_target_repo` with `service_not_registered`,
  `service_name_ambiguous` or `target_unreadable`. Sibling checkouts that share
  a folder name are all searched. See [SERVICE_ALIASES.md](SERVICE_ALIASES.md).
  A `no_target_repo` row also has `no_target_reason` (`external_vendor`, `repo_not_registered` or
  `target_unreadable`), the reply has `counts_by_reason`, and `status` accepts those values too.
- Within the target, an endpoint matches when its HTTP method is the same (an
  endpoint mapped for every method matches any) and its route is the same
  after dropping the query string, collapsing repeated slashes, dropping a
  trailing slash and treating every `{name}` as one wildcard segment. Case is
  kept. A `{name}` in the call does not match a fixed segment on the server.
  One endpoint is `matched`, several are `ambiguous`, none is `no_route_match`.
- A call with no exact match whose route equals a server route once case is
  ignored is `case_mismatch` (reason `route_differs_by_case`, `confidence`
  `low`), with those endpoints as `candidates`. It is a lead to check, not a
  match: a case-sensitive server does not serve it. Only a call that would
  otherwise be `no_route_match` can become one.
- For a JavaScript or TypeScript call that still has no match, a `{name}` in the call may
  stand against a fixed server segment (`/agents/{name}/threads` reaches
  `/agents/planner/threads`): reason `variable_matches_literal`, `confidence` `low`.
  The call needs the same segment count, an equal method and at least two segments it
  spells out, and more than 10 matching server routes is too open to report. Only a
  call that would otherwise be `no_route_match` can change.
- For a JavaScript or TypeScript call that is still `no_route_match`, two more leads are
  looked for (the caller's own repository first, then the others; the first beats the second).
  `method_not_served`: the normalised route exists on a registered server but only with other
  HTTP methods. The row's `candidates` are those endpoints (repo, file, line, method) and
  `served_methods` lists the methods that are served; `reason` is
  `route_served_with_other_method`. The Ember Data parser infers all five CRUD verbs per
  model, so many of these are calls the client never makes. `prefix_mismatch`: no route of the
  right method fits, but one has the same last two path segments under a different prefix
  (client `/v1/admin/projects/{id}`, server `/admin/projects/{id}`). It is a lead, not a match:
  `confidence` is `low`, `reason` is `same_last_two_segments_under_other_prefix`, `candidates`
  are the routes that share the tail, and more than 10 distinct routes is too open to report.
  Both `status` values filter, are counted in `counts` and the summary, and `no_route_match`
  remains for a call nothing fits. Only a call that would otherwise be `no_route_match` can change.

Reasons for rows with no match (statuses never change; `status=<reason>` filters; `counts_by_reason` tallies them,
together with `no_target_reason`): a JavaScript `unmatchable` row is tallied under its `reason` (above; Feign `unmatchable` rows keep theirs on the row but are not tallied). A JavaScript `no_route_match` row has
a `reason`: `no_servers_indexed` (no registered repo has an endpoint), `no_endpoint_under_base_path` (no endpoint of any
repo starts with the call's first path segment, so the serving repo is probably not registered or not indexed),
`variable_segments_unmatched` (the route has `{}` pieces and no endpoint shape fits) or
`route_missing_under_served_base` (that first segment is served, this route is not). A Feign `no_route_match` row has
`reason: "served_by_caller_repo"` when the calling repo itself has an inbound endpoint with the same method (or any
method) and the same normalised route: the target repo named by the client does not serve it, the caller does, so the
`@FeignClient` name is probably wrong. Other Feign `no_route_match` rows have no reason.

Every row has an `origin`: `feign` for a Spring client method, `js` for a
JavaScript or TypeScript call. Feign rows are classified exactly as above.

JavaScript and TypeScript calls (`origin: "js"`) have no service name, so they
are matched by HTTP method and normalised route. A relative URL goes to the
origin that served the page, so the server endpoints of the caller's own
repository are searched first (its Next.js `app/api` routes, an Express server
beside the client); only when they have no match does the search widen to every
other registered repository. A row has a `client_kind`
(`axios`, `fetch`, `jquery`, `ember-ajax` or `ember-data`). One matching
location, or several in one repository (`reason: "several_handlers_in_repo"`,
for example handlers of one path that differ by `params=`), is `matched`;
several repositories are `ambiguous` with the candidate repositories; none is
`no_route_match`. A call
is `unmatchable` with one of these reasons: `non_literal_url` (a variable or
computed URL, or a template or concatenation that starts with an unknown
piece), `absolute_url` (a full `https://...` URL or an absolute `baseURL`),
`method_dynamic` (the method is not a literal) or `placeholder_in_route`.
A match carries `confidence` and `confidence_notes` when it rests on a guess.
`low`: `ember_data_convention` (the route was derived from a model name),
`base_url_not_literal`, `catch_all_route` or `variable_matches_literal`.
`receiver_inferred` (an instance imported from another file, recognised by its
name) is `medium`, and `high` when one server location answers the call by exact
HTTP method and a literal route with no `{...}` piece. Several handlers of one
path (`several_handlers_in_repo`) are `medium` at best. Several handlers of one
path stay `matched` (with every handler in `candidates`); `ambiguous` is for
several repositories. When the call's URL spells its query keys out
(`/v1/accounts?onlyActiveAccounts=true`), the Spring `params` conditions of the
handlers narrow the candidates: a handler whose `params` cannot hold is dropped, and the
one with the most conditions wins, as in Spring. A URL with no query, or a computed one,
narrows nothing (axios `params` objects are not read). `headers` conditions are indexed
but not compared. A server route that takes
the rest of the path (a Next.js `[...path]` route, an Express `*`) matches only
a call with the same HTTP method and a literal prefix that is a whole leading
path, with at least one more segment (none for `[[...path]]`), and only when no
exact route fits; a catch-all at the root matches nothing. Its candidate has
`catch_all: true`.

Python servers are indexed too, so a frontend or another service can reach
them. FastAPI and Starlette-style routers (`APIRouter`, `FastAPI`) and Flask
(`Flask`, `Blueprint`) routes are read from `@router.get/post/put/patch/delete/head/options`,
`@router.websocket(path)` (method `WEBSOCKET`), `@app.api_route(path, methods=[...])` and
`@app.route(path, methods=[...])` (default `GET`), and from the decorator-free
`router.add_api_route(path, handler)`, `add_api_websocket_route` and
`add_url_rule(rule, view_func=...)` (a `View.as_view(...)` handler is the class) when the handler is
defined in the same file. The receiver must be a router: one built by `APIRouter`, `FastAPI`,
`Blueprint` or `Flask` in the file, or an imported name that reads as one (`app`, `router`,
`oauth_router`, `bp`), so `@cache.get(...)` is not a route. `handlers_of` takes an endpoint; given a
handler it returns the endpoints it serves and says so in `confidence`. An empty `endpoints_for` for a
decorated or handler-like function says the route may not be statically visible rather than a real
absence. Flask `<int:id>` turned into `{id}` and a trailing
`{name:path}` or `<path:name>` read as a catch-all. A route's full path is the
prefix of every router above it plus its own path: `APIRouter(prefix=...)`,
`Blueprint(url_prefix=...)` and the `prefix=` or `url_prefix=` of
`include_router` and `register_blueprint`, followed across files through
`import` and `from ... import` (relative ones too). A router mounted twice
gives a route for each mount. A prefix or path that is not a string literal is
never guessed: the route is left out. Routes in test, fixture and example
directories are ignored; class-based Flask views and `add_url_rule` are not
read.

What the parser records for JavaScript and TypeScript (all of it is a plain
`Endpoint` node: client ones have `role: "client"` and a `REQUESTS` edge from
the enclosing function, server ones a `HANDLES` edge from the handler):

- Calls: `axios.get/post/put/patch/delete/head/options(url)`,
  `axios({method, url})`, `axios.request`, instances from `axios.create`
  (their `baseURL` is applied only when it is a string literal in the same
  file, or set by `instance.defaults.baseURL = '...'`), `fetch(url, {method})`,
  `$.ajax`, `$.getJSON`, `$.get`, `$.post`, and Ember `this.ajax`,
  `this.ajax.request` and `ajax`. The URL may be a string, a template literal
  (each `${expr}` becomes `{param}`), a `+` concatenation, or a same-file
  top-level `const` string; a leading unknown piece is never guessed. A query
  string glued to the path (`` `/feeds${query}` `` where `query` is
  `cond ? `?${q}` : ''`) is dropped with the rest of the query, but only when
  every declaration of that name in the file is a `const` holding an empty or
  `?...` string; a parameter or any other binding of the same name keeps it a
  path parameter. A call whose URL cannot be read is recorded as unmatchable
  only for a certain HTTP client (axios, `$.ajax`, Ember ajax, `fetch`), never
  for an imported instance. Test files are skipped.
- Ember Data: a model under `app/models` (or `addon/models`, or a pods
  `model.js`) becomes `GET /<ns>/<types>`, `GET /<ns>/<types>/{id}`,
  `POST /<ns>/<types>`, `PUT /<ns>/<types>/{id}` and `DELETE /<ns>/<types>/{id}`,
  from the literal `namespace` of `adapters/application` and the model's own
  adapter (which may also set a literal `pathForType`). The edges are
  `INFERRED` at 0.6. A `RESTAdapter` camelizes the plural (`blogPosts`) and
  updates with `PUT`; a `JSONAPIAdapter` dasherizes (`blog-posts`) and
  updates with `PATCH`. A `host` is recorded but not used; `urlFor*` hooks are
  ignored; a non-literal `namespace` or `pathForType` makes the model
  unmatchable. Plurals follow the regular rules (`y` to `ies`, `s`/`x`/`z`/`ch`/`sh`
  to `es`, otherwise `s`) with a short irregular list (`person`, `child`, `man`,
  `woman`, `mouse`, `foot`, `tooth`, `goose`, `ox`) and uncountables such as
  `sheep` and `series`; any other irregular noun is wrong. The adapter files are
  read from disk when the model is parsed, so an adapter edit shows after the
  model is parsed again. An Ember Data model that extends `Fragment` from
  `ember-data-model-fragments` is skipped, because a fragment lives inside its
  owner record and is never requested; a model that only uses `fragment(...)`
  attributes still is a resource.
- Servers: Next.js app router `route.ts|js` exports (`GET`, `POST`, `PUT`,
  `PATCH`, `DELETE`, `HEAD`, `OPTIONS`; the path comes from the directory,
  `[id]` becomes `{id}`, `(group)` and `@slot` directories are dropped,
  `[...path]` and `[[...path]]` are catch-alls), the pages router under
  `pages/api` (methods read from `req.method` comparisons, otherwise `ANY`), and
  `app.get('/x', handler)` style calls in files that load Express, Koa, Fastify,
  Hono, restify, Hapi, polka or connect (also `router.route('/x').get(...)`
  and `fastify.route({method, url})`). `:id` becomes `{id}`. `app.use('/prefix',
  router)` mounting is not followed, so a mounted router's routes are
  recorded without the prefix. A handler written inline, `app.get('/x', (req,
  res) => ...)`, becomes a Function node named by where it sits
  (`handler@L12C20`), so every route has its own handler; the calls in its body
  belong to it, except those on its first line, which stay with the code that
  registered the route.

`repos` selects the calling repositories only; targets are always looked up
across the whole registry. Names that match no entry are returned in
`unknown`, ambiguous ones in `ambiguous`, as for `cross_repo_search_tool`.
`counts` and `total` cover every call that passed the filters; `results` is
capped by `limit` and `max_results`, and `truncated` says so. A repository whose
graph is missing or unreadable is listed in `unreadable` (with its role,
`client` or `target`, and a short reason) and does not stop the call.

The call has the same time budget (`CRG_CROSS_REPO_BUDGET_SECONDS`, default 300, `0` = no
limit). When it is spent no further client repo is read; those left are listed in
`skipped_repos` / `skipped_repos_total`, `truncated` is set, and `counts` and `total` leave
them out. A cursor made on such a partial answer is refused unless the same repos are
skipped again.

AMQP list mode. `origin: "amqp"` with no `service` lists every exchange instead of one
exchange's rows: one row per exchange, sorted by name, with `publisher_count`, `listener_count`,
`publisher_repos`, `listener_repos`, the `routing_keys` publishers use and the `binding_keys`
listeners bind with (each list cut at 20, with a `*_total` when longer) and a `status`:
`unlistened` (publishers, no listener), `unpublished` (listeners, no publisher) or `matched`
(both; the keys are not compared). `status` filters on those three values (`publisher` and
`listener` belong to the one-exchange form and are refused), `route` keeps the exchanges with a
routing or binding key containing it as a substring, and `repos` limits the repos read, so the
roles are those of the selected repos. `counts` covers every exchange that passed `route`. It
pages like the other origins. Names used only as a queue are listed apart in `queues`; they page
with the exchanges under the same cursor, each list by at most `max_results` rows a page, with
`queues_total` (the full count) and `queues_truncated` on every page. A `status` filter leaves
`queues` out. With `service` the reply is unchanged.

### `origin="trigger"`: what invokes a resource

Answers "what invokes this Lambda / queue / state machine / topic / bucket handler?" as a
query-time join over the Terraform already in each graph; no rebuild and no parser change.
`service` keeps rows whose invoked side (target) is that name, `service/name` (`lambda/worker`) or
Terraform label; `route` keeps rows whose firing side (source) is that rule, queue, topic, bucket
or route key. "What triggers X" is `service=X`; "what does X fire" is `route=X`. `status` is
`resolved`, `source_unresolved` or `target_unresolved`. Pages like the other origins.

| `trigger_kind` | Read from | Source | Target |
| --- | --- | --- | --- |
| `eventbridge_rule` | `aws_cloudwatch_event_rule` + `aws_cloudwatch_event_target` | rule, with its literal `schedule` and `event_pattern_sources` | lambda, sqs, sns, ecs cluster, state machine |
| `eventbridge_scheduler` | `aws_scheduler_schedule` | schedule, with `schedule` | as above |
| `event_source_mapping` | `aws_lambda_event_source_mapping` | sqs / kinesis / dynamodb | lambda |
| `sns_subscription` | `aws_sns_topic_subscription` | topic | lambda, sqs, or the protocol (`https`, `email`) |
| `s3_notification` | `aws_s3_bucket_notification` | bucket | lambda, sqs, sns |
| `api_gateway` | `aws_apigatewayv2_route` + integration; `aws_api_gateway_integration` | route key (v2) or REST API label (v1) | lambda |
| `lambda_permission` | `aws_lambda_permission` | rule / topic / bucket / API, with inferred `principal` | lambda |
| `eventbridge_pipe` | `aws_pipes_pipe` | queue / stream / table | lambda, state machine |

Each row has `source`, `source_service`, `target` (`service/name`), the declaring `repo`, `file`,
`line`, `status` and `via` (`terraform` when read from the attributes the indexer recorded,
`reference` when inferred from which resources a block mentions).

Shapes it cannot resolve (these never get a guessed name):

- A name built from a variable, local, `${...}` or `module.*` output: the row is
  `source_unresolved` / `target_unresolved` and carries the Terraform label
  (`aws_api_gateway_rest_api.public`). A reference to a resource in another directory of the
  module is not followed.
- State machines, REST APIs, Kinesis streams and ECS clusters are not named by the indexer, so a
  reference to one is a label, not a deployed name.
- `aws_lambda_permission`, `aws_pipes_pipe` and `aws_api_gateway_integration` are not recorded
  attribute by attribute: the index holds only which resources the block references. The
  `principal` is inferred from the referenced resource type; `source_arn` is not stored. A pipe
  with zero or several candidate sources gets an unresolved source.
- API Gateway v1 route paths and methods are not recorded; the row has no `route`.
- Event patterns: only literal `source` values are kept, not `detail-type` or `detail`. Bucket
  notification event types and filters are not kept. An SNS endpoint URL or address is a value
  and is not stored.
- Other triggers (CloudWatch Logs subscriptions, Cognito, Alexa, Config, DynamoDB Streams enabled
  by `aws_lambda_event_source_mapping` with a computed ARN, ALB target groups) are not read.
- A rule declared inside a shared module and wired in a stack shows only the half each repo
  declares. Such a module row, whose firing side is only a Terraform label, is marked
  `template: true`: the name comes from a variable the caller sets.

Rows with `via: terragrunt_inputs` come from a Terragrunt Lambda unit's `inputs`: each item of
`aws_cloudwatch_event_rules` / `aws_cloudwatch_event_rules_ignore_changes` (a `rule` with its
`schedule`, `event_pattern_sources` when the pattern is a plain JSON string, `disabled: true`
for `is_enabled = false`), `existing_sns_topics` (an `sns_subscription` to the function, or to
the queue when `include_sqs = true`) and `include_sqs` with `sqs_name` (an `event_source_mapping`
from the queue). The target is the unit's `function_name` (`target_from_directory: true` when
the name had to be taken from the directory above the unit). Only names, `rate(...)`/`cron(...)`
expressions and event-pattern `source` strings are read: the rule's `input` document, description
and every other entry are never visited, and a name that looks like a secret drops the rule. The
index must be rebuilt for a unit to carry these facts.

`origin="external"` lists the HTTP calls that reach no registered service, grouped by host, to
show which third-party vendors each repo depends on. One row per (host group, repo), the
busiest host first: `host`, `vendor` and `vendor_basis` (`host_table` or `service_name`; an
unknown host has none), `status` (`known_vendor`, `unknown_vendor`, `host_unresolved`), `calls`,
`host_calls`, `files` (up to 5), `samples` (up to 3 `method`/`path`/`file`/`line`), `sources`
(counts by how the host was found) and, for a collapsed vendor, `hosts_seen`. Sources:
JavaScript/TypeScript calls with an absolute URL, `baseURL` or Ember adapter `host`
(`js_absolute_url`, `js_base_url`, `ember_adapter_host`), a route that starts with a `${KEY}`
placeholder (`host_unresolved`, with `unresolved_key`), and Feign clients whose service name
resolves to no registered repo (`feign_service`, grouped as `service:<name>`; a literal `url=` in
the annotation gives the host, read from that annotation's lines). A `${...}` service name is
`host_unresolved` with the first configuration key. Hosts are lower-cased; scheme, credentials,
port, query, fragment and a leading `www.` are dropped; query strings and credentials never
appear. A vendor entry such as `*.auth0.com` or `*.salesforce.com` collapses tenant subdomains.
Python `requests` / `httpx` calls (`requests.get/post/...`, `requests.request`, a `Session()` or
`httpx.Client()` object; only in a module that imports them) are listed too, by host only
(`python_http_literal`: a literal URL or one with a literal `https://host/` start). The path and
query of a Python URL are never stored, so a Slack webhook or a token in a query string shows as
its host; `path` is always `/`. A URL that is not a literal is `host_unresolved`, grouped as
`${variable}` (the variable, attribute or environment variable name; `python_http_variable`).
`service` filters by host, domain, vendor or Feign service name, `route` by path prefix. Limits:
the graph keeps a Feign service name and the keys of its placeholders, never the value, so a
Feign client configured by property shows the service name, not a host; Python and Java clients
(`RestTemplate`, `HttpClient`) leave no `Endpoint` node and are not seen; an absolute
URL to the company's own domain is listed like any other host.

`origin="consumers"` is the reverse lookup of a named resource. `service` is a queue, bucket,
SSM parameter, table, topic or secret name. Rows are the Terraform declarations (`role`
`declaration`: `aws_sqs_queue`, `aws_sns_topic`, `aws_s3_bucket`, `aws_dynamodb_table`,
`aws_lambda_function`, and `aws_ssm_parameter` / `aws_secretsmanager_secret`, whose `name` is read
from the block's lines; a `${...}` name is a template, `declared_as: template`) and every repo
that uses it (`role` `consumer`) with `match`: `exact` (a name in code, a Spring property
resolved in a profile with `via: resolved_name`, or Terraform wiring of another repo), `prefix`
(an SSM `GetParametersByPath` path above the parameter) or `config_key` (a property or
environment variable called as asked). `route` filters by AWS service or kind (`sqs`, `queue`,
`ssm`), `status` by `declaration`, `consumer`, `exact`, `prefix` or `config_key`. Matching is
case-sensitive equality; an SSM name matches with or without its leading `/`. False positives: a
name shorter than 4 characters, or a generic one (`logs`, `test`, `events`, `prod` ...), is
refused (`refused`, `min_name_length` in the reply) and a `GetParametersByPath` path that short
or generic is not used for prefix matching; equal names in different accounts or regions are not
told apart. Not joined, because the graphs hold no values: environment variable and task
definition values, and string literals other than AWS call targets (`not_joined` in the reply).
With no `service` it lists the SSM reads (`role` `undeclared_read`) that match no declared
parameter of any registered repo; "undeclared" means no registered Terraform declares it by a
literal or templated name, not that it is missing in AWS. Reads whose name is unknown are counted
in `unresolved_ssm_reads`, and SSM/secret blocks whose name is computed in
`declarations_with_computed_name`. The existing `terraform` origin is unrelated: it joins
remote module sources to repos and has no resource names.

Paging. The `terraform`, `workflow`, `stepfunction`, `awssdk`, `amqp`, `trigger`, `external`
and `consumers` origins return at most
`max_results` rows (at most 100) per call. When rows remain, `truncated` is set and
`next_cursor` is returned; pass it back as `cursor`, with the same other arguments, to read
the next page. `limit` is not used by these origins. A page after the first has `offset`
and names its range in `summary`, for example `showing 51-100 of 133`. A cursor is refused when the
query, the repos left out by the time budget, or the rows themselves changed since it was
made; call again without a cursor to start over. A call that takes
`CRG_CROSS_REPO_SLOW_SECONDS` (default 10) or longer logs its timing line as a warning.

The `infrastructure` block covers at most 20 selected repos. When it stops early,
`infrastructure.repos_not_checked` names the repos left out (up to 50; the full count is
`repos_not_checked_total`); call again with `repos` set to them to read the rest.

Speed. The `stepfunction` and `awssdk` origins, and the service-identity facts the Feign join
reads from every repo, are read on up to `CRG_CROSS_REPO_WORKERS` threads (default 4, `1` = one at
a time, at most 32), each on its own graph connection. What is read about a repo is kept while
that repo's graph file is unchanged (same file, modification time and size). A long-lived server
therefore reads a repo again only after the indexer publishes a rebuild of it. A value that failed
to read is not kept. `CRG_REPO_MEMO_ENTRIES` (default 4096) bounds how many results are kept and
`0` turns the cache off. With a `service` or `route` filter on `stepfunction` or `awssdk`, a row
the filter drops gets no owner lookup; the rows returned are the same as without the early check.
When the time budget is checked with more than one worker, up to `workers - 1` repos may start
after it ran out. The server log has one `cross-repo <origin>:` line per call with the seconds each
phase took (`read`, `context`, `prefetch`, `resolve`, `infrastructure`).

#### `transitions_of_tool`
```
machine: str                 # Machine name, <file>::<machine>, or the *.asl.json file
repo_root: str | None        # Repository root; auto-detected
detail_level: str = "standard"   # "standard" rows are dicts; "minimal" rows are one string each
max_results: int = 200       # Rows per page (at most 500)
cursor: str | None           # next_cursor of the previous reply (see Paging)
```

Every transition of one Step Functions state machine in one reply, instead of one `next_states_of`
call per state. Rows follow the file order and cover nested Parallel and Map states. A standard row
is `{from, to, kind, condition?, line, errors?}`; a minimal row is
`"from -> to | kind | condition | Lline"`. `kind` is `Next`, `Choice`, `Default`, `Catch`, `Branch`
or `Map`. A Choice `condition` is read from the file while it is the one that was indexed (the graph
never stores it); otherwise `note` counts the conditions left out. The reply carries `state_count`
and `transition_count` for the whole machine, and pages with `next_cursor`. A name that matches
several machines answers `ambiguous` with their qualified names. Query-time only: no index change.

#### `pr_blast_radius_tool`
```
pr: int | None               # Pull request number; the server fetches refs/pull/<pr>/head from origin
head: str | None             # A ref or commit to read the new files from, when no pr is given
base: str | None             # The branch the PR merges into; default: the remote's default branch
depth: int = 3               # Hops the reach walk follows from a changed symbol (max 6)
include_external: bool = True  # Join affected endpoints to client calls in the other registered repos
repo_root: str | None        # Repository root; auto-detected
files_limit: int = 50        # Rows per list per page (max 200): files and each capped list
cursor: str | None           # next_cursor of the previous reply, to read the next page of every list
```

The graph holds the default branch, so a pull request's new files are not in it. This tool reads only the files the
pull request touches, straight from Git, parses each in memory with the parser a build uses and compares the result with
the base graph. Nothing is written to the graph. What is fetched goes into a temporary repository that is removed when the call ends, so
the checkout gains no branch, ref, object or working-tree change.

Per file it reports the symbols **changed** (a symbol the base graph has, overlapped by a changed line), **added** (only the
new file has it) and **removed** (only the base graph has it), `reached` (what depends on the changed and removed symbols,
over the same edge kinds the impact radius follows, `depth` hops out; `reached_tests` counts the tests among them
separately), `same_repo_files` and the changed members no test
names (`untested`). `contract_changes` lists routes the pull request adds or removes. `endpoints` lists the endpoints it
adds, removes or reaches, or whose handler it adds or changes, each with `external_callers`; `same_file_endpoints` lists
the other endpoints of the changed files, which are not affected and whose callers are not counted. `config_changes`
lists the YAML, JSON and Spring `.properties` paths the pull request adds, changes or removes (`change`, `kind`, `path`,
`line`, `value_type`; values are compared by hash and never returned; at most 200 per file, 1000 in all, 50 files, with
`config_changes_truncated`). A pull request whose head is already an ancestor of the base answers `already_merged: true`
and a summary saying the graph already includes it. `external_callers` lists the client calls in the other registered
repos (Feign, JavaScript and TypeScript) that reach them, read from each repo's stored endpoint index. `schema_changes`
lists the DDL in migration files the pull request adds or changes. `notes` states what the answer rests on: a stacked
pull request (pass its parent as `base`; the reply then also says how many files differ from the default branch), a shallow
clone without a merge base, Python routes whose router prefix sits in a file the pull request does not touch, and files
that could not be parsed. At most 300 files are parsed per request. `files`, `contract_changes`, `endpoints`,
`same_file_endpoints`, `config_changes`, `external_callers` and `schema_changes` page together, one cursor for all, each
by at most `files_limit` rows a page: every list has a `*_total` (its full size) and a `*_truncated` flag, and while any
list has more rows `next_cursor` returns the next page of all of them (a later page has `offset`, the index of its first
file row). A cursor is refused for another `pr`, `head`, `base`, `depth` or `include_external`, or when the rows differ
now. A later page says so in `continuation_note` and leaves out `external_info`.

## Result Bounds

Every tool that returns a list is bounded, so one MCP response cannot fill a
context window (see #849). The contract is the same everywhere:

- Defaults are small. Pass the tool's cap parameter to widen up to its hard
  ceiling, or a smaller value to narrow.
- Truncation is reported: the response carries the untruncated count
  (`total`, or a `*_total` field per list), sets `truncated: true`, and the
  summary says how many of how many are shown.
- Cap parameters reject values below 1 and reject booleans.
- Hard ceilings are enforced in code and pinned by `tests/test_token_budget.py`.

### Paging

`list_flows_tool`, `list_communities_tool`, `get_hub_nodes_tool`,
`get_bridge_nodes_tool`, `get_surprising_connections_tool`, `get_knowledge_gaps_tool`,
`get_community_tool`, `get_architecture_overview_tool`, `get_flow_tool`,
`cross_repo_search_tool`, `cross_repo_requests_tool`, `get_impact_radius_tool`,
`query_graph_tool`, `semantic_search_nodes_tool`, `find_large_functions_tool`,
`traverse_graph_tool`, `get_review_context_tool`, `detect_changes_tool` and
`refactor_tool` do not stop at their ceiling: when more rows exist the reply carries
`next_cursor`, and passing it back as `cursor` returns the next page, so the ceiling
bounds one reply, not what you can read. This follows the cursor pagination the MCP
specification defines for its list operations (`cursor` in, `nextCursor` out), with the
result key spelled in snake_case like the other keys here.

- A reply with no `next_cursor` is the last page. `truncated` is `true` exactly
  when `next_cursor` is present, `total` is always the whole list, and a page after
  the first adds `offset`, the index of its first row. The first page is the same
  as before, plus `next_cursor` when the list was cut.
- A cursor is opaque. It is valid only for the same query on the same graph, and
  the page size (`limit`, `max_results` or `top_n`) may change between pages.
  A cursor from a different query or tool, or from before the graph was rebuilt or
  updated, is refused with an error that says to start over without a cursor.
  Cursors are not signed and hold no state on the server.
- Ties in a ranking are ordered by a fixed key (flow and community id, a node's
  qualified name, a node's id for `find_large_functions_tool`, an edge's endpoints and
  id), so a page never repeats or skips a row.
- A review is computed from a git diff on every call, so the cursors of
  `get_review_context_tool` and `detect_changes_tool` are also bound to the base,
  the exact ordered changed-file list, the depth and the detail level, and to the
  rows they page over. A different diff, base or file list refuses the cursor.
  `detail_level="minimal"` has no pages. In `get_review_context_tool` the cursor
  and `offset` are in `context`, next to `truncated`.
- `refactor_tool` pages the edit list of a rename, the dead-code list and the
  suggestion list. A cursor is bound to the mode and its arguments (`old_name` and
  `new_name`, or `kind` and `file_pattern`), so one from another mode is refused.
- `get_impact_radius_tool` pages one list: `impacted_nodes` in standard mode (page
  size `max_results`, at most 100; the traversal grows to reach the page) and
  `impacted_files` in compact mode (page size `max_files`, among the files of the
  `max_results` nodes traversed). The other lists keep their caps and repeat on
  every page, and `truncated` stays true while any of them is cut. `minimal`
  takes no cursor. It is refused when the changed files, depth, resolution or
  detail level differ.
- `query_graph_tool` pages rows in the order the graph yields them; `path_to` is a
  single route and takes no cursor. `results_omitted` counts the rows not in the
  reply, including earlier pages.
- `semantic_search_nodes_tool` ranks a window three times `limit` long, and the
  window sets the ranking, so a cursor reads that window and needs the same
  `limit`. The cursor also records the ranked order itself, so one made before
  embeddings were added or the model changed is refused.
- `traverse_graph_tool` pages the same walk: `token_budget` applies to each page,
  `next_cursor` is present exactly when `truncated` is true, except that a budget
  too small for a single node returns neither nodes nor a cursor.
- Where a reply holds several lists, one cursor moves them together by position:
  `get_knowledge_gaps_tool` shows the same row positions of every category (a category
  with no rows left is empty), `get_architecture_overview_tool` pages the
  communities (largest first), the cross-community rows and the warnings under one
  offset, and `get_community_tool` pages `members` and `member_details`. In
  `get_architecture_overview_tool` each community's member names stay bounded as
  before; read a community's members with `get_community_tool`. A page after the first of these three adds `offset`; in
  `get_community_tool` and `get_flow_tool` the cursor and `offset` sit at the top of
  the reply, beside `flow` or `community`, and `members_truncated` means members remain.
- `get_flow_tool` pages `flow.steps`; `flow.path` (the node ids of every step) is
  returned whole on every page. The `max_source_lines` budget is spent per reply, so
  each page gets a fresh one.
- `get_knowledge_gaps_tool` now counts every isolated node and untested hotspot in
  `summary`; they used to be cut at 50 and 20 before counting.
- `cross_repo_search_tool` searches the repos afresh on every call, so its cursor
  also holds a digest of the merged results and is refused once they change.
  The merged order (match tier, local rank, registry order) is part of those rows, and the
  repos skipped for time are part of the digest.
- `get_bridge_nodes_tool` samples a fixed set of nodes on graphs over 5000 nodes, so
  its ranking is the same on every call.

## MCP Prompts (5 workflow templates)

### `review_changes`
Pre-commit review using detect_changes, affected_flows and test gaps.
```
base: str = "HEAD~1"
```

### `architecture_map`
Architecture documentation using communities, flows and Mermaid diagrams.

### `debug_issue`
Guided debugging using search, flow tracing and recent changes.
```
description: str = ""
```

### `onboard_developer`
New developer orientation using stats, architecture and critical flows.

### `pre_merge_check`
PR readiness check with risk scoring, test gaps and dead code detection.
```
base: str = "HEAD~1"
```

## Skills and Slash Commands

The skills `build-graph`, `review-delta` and `review-pr` ship in the repository's `skills/` folder. `install --platform qoder` (or the default `--platform all`, when Qoder is found on this machine) copies them, with the four workflow skills below, to `.qoder/skills/`. Other clients do not get these three.

### `build-graph`
Build or update the knowledge graph.
- First run: full build
- Later runs: incremental update (changed files only)

### `review-delta`
Review the last commit plus uncommitted and new files.
- Changed files come from `git diff` against `HEAD~1`
- Blast radius is changed nodes plus 2-hop neighbours
- Output is a structured review with guidance

### `review-pr`
Review a PR or branch diff.
- Uses `main` (or `master`) as the base
- Covers every commit in the PR
- Output is a structured review with a risk assessment

`install` also writes four workflow skills, `explore-codebase`, `review-changes`,
`debug-issue` and `refactor-safely`, to `.claude/skills/` for Claude Code,
`.gemini/skills/` for Gemini CLI, `.codebuddy/skills/` for CodeBuddy and
`~/.hermes/skills/code-review-graph/` (or `$HERMES_HOME/skills/code-review-graph/`)
for Hermes Agent. Qoder gets all seven skills in `.qoder/skills/`, and Codex gets
one global skill. With no `--platform`, each is written only when that tool is
found on this machine. `--no-skills` skips them all.
