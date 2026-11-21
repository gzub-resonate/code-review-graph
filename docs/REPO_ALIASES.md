# Repo Aliases (A Second Name for a Repository)

A registry entry holds one optional alias, and `code-review-graph register
--alias` replaces it, so a repository cannot be known by a second name. Names
used elsewhere often differ from the registered alias or the folder name: an
old service name, a name from a chat thread, a name in another system.

An optional file maps those extra names to repositories you have already
registered. No registry change, no re-registering.

## Quick start

Create `repo-aliases.toml` next to `registry.json`, in `~/.code-review-graph/`
(or in `$CRG_HOME` when that is set):

```toml
[aliases]
old-service-name = "billing"
legacy-orders = "orders-service"
```

Each value is the alias or the folder name of a registered repository. Then
use the extra name anywhere a repository is picked by name:

```text
cross_repo_search_tool(query="invoice", repos=["old-service-name"])
```

The file is optional. Without it nothing changes.

## Schema reference

One `[aliases]` table of `name = "target"` pairs.

| Part | Type | Rules |
|------|------|-------|
| name (the key) | string | Letters, digits, `.`, `_` and `-`; 1 to 100 characters. Case-sensitive: `Billing` and `billing` are different names. |
| target (the value) | string | Not empty. The alias or folder name of a registered repository. |

At most 200 aliases are read; later entries are ignored.

## Resolution order

A name is resolved in this order, and the first match wins:

1. A real registry alias.
2. A folder name.
3. A name in `repo-aliases.toml`.

So nothing in the file can shadow a registered repository: if `billing` is a
registered alias or a registered folder, `billing = "orders"` in the file has
no effect.

The target is looked up with rules 1 and 2 only, one level deep. A target
that is itself only a name in the file does not resolve, so there are no
chains and no cycles. A target that matches no registered repository is
skipped with a warning, and the name is reported the way any unknown name is
(for example in `unknown` from `cross_repo_search_tool`).

`register` and `unregister` never read or change the file. If you unregister
a repository, aliases that pointed at it stop resolving and log a warning.

## Validation rules

The loader never raises, so a broken file cannot break a tool call.

- A file that is not valid TOML (this includes a repeated key) gives no
  aliases and one warning.
- `aliases` that is not a table gives no aliases and one warning.
- An entry with an invalid name, or a target that is not a non-empty string,
  is skipped with a warning; the other entries still load.
- On Python older than 3.11 the `tomli` package is needed to read the file.
  Without it the file is ignored with a warning.

## Troubleshooting

- **The name is reported as unknown.** Check that the target is spelled like
  a registered alias or folder (`code-review-graph repos` lists them), and
  that the name is not a chain of two file entries.
- **Nothing happens.** Check the file is in the same folder as
  `registry.json`; `$CRG_HOME` moves both.
