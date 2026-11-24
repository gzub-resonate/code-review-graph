"""Join GitHub Actions ``uses:`` references to the registered repo that holds them.

A workflow's ``uses: org/repo/.github/workflows/ci.yml@v1`` (a reusable workflow) or
``uses: org/repo/path@v2`` (an action) is stored in its own repo's graph as an
``IMPORTS_FROM`` edge to ``external:github-actions:org/repo/<path>``, with ``owner_repo``,
``path``, ``ref``, ``kind`` and ``job`` in the edge's ``extra``. That points outside the graph.

This module reads those edges from every registered repo (read-only) and resolves the repo
name (the part after ``org/``) to a registered repo, the way ``cross_repo_requests`` resolves a
service name. A link is ``matched`` when the repo is registered and the path is a file or
folder in its graph, ``path_not_found`` when the repo is registered but the path is not,
``ambiguous`` when several registered repos answer to the name, and ``repo_not_registered``
otherwise. Local (``./``) and ``docker://`` references point at no other repo and never
appear. The join is computed on every call; nothing is stored.

An action at the root of its repo (``org/repo@v1``) has an empty path, so ``matched`` there
only says the repo is registered and its graph is readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .graph import GraphStore
from .infra_references import DYNAMIC_GITHUB_ACTIONS_TARGET, EXTERNAL_GITHUB_ACTIONS_PREFIX
from .repo_aliases import RepoNameResolver
from .terraform_links import _like_escape, open_store  # noqa: F401  (open_store re-exported)

STATUSES = ("matched", "path_not_found", "ambiguous", "repo_not_registered")


def read_uses_edges(store: GraphStore) -> list[dict[str, Any]]:
    """The remote ``uses`` references a graph holds, one row per ``IMPORTS_FROM`` edge."""
    rows = store._conn.execute(
        "SELECT source_qualified, file_path, line, target_qualified, extra FROM edges "
        "WHERE kind = 'IMPORTS_FROM' AND target_qualified LIKE ? ESCAPE '\\'",
        (_like_escape(EXTERNAL_GITHUB_ACTIONS_PREFIX) + "%",),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for source, file_path, line, target, extra in rows:
        if target == DYNAMIC_GITHUB_ACTIONS_TARGET:
            continue
        try:
            info = json.loads(extra or "{}")
        except ValueError:
            info = {}
        owner_repo = info.get("owner_repo")
        if info.get("kind") not in ("action", "reusable_workflow") or not owner_repo:
            continue
        out.append({
            "file_path": file_path or source,
            "line": line,
            "owner_repo": owner_repo,
            "path": info.get("path") or "",
            "ref": info.get("ref"),
            "kind": info["kind"],
        })
    return out


def path_exists(store: GraphStore, repo_path: Path, path: Optional[str]) -> bool:
    """Whether the graph of the repo at *repo_path* holds *path* as a file or a folder."""
    rel = (path or "").strip("/")
    if not rel:
        return True
    full = str(repo_path).rstrip("/") + "/" + rel
    row = store._conn.execute(
        "SELECT 1 FROM nodes WHERE kind = 'File' AND "
        "(file_path = ? OR file_path LIKE ? ESCAPE '\\') LIMIT 1",
        (full, _like_escape(full) + "/%"),
    ).fetchone()
    return row is not None


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    path_in_repo,
) -> list[dict[str, Any]]:
    """Resolve each ``uses`` reference to a registered repo and path.

    *path_in_repo* is ``(target_index, path) -> bool``.
    """
    resolver = RepoNameResolver(all_repos)
    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            name = edge["owner_repo"].rsplit("/", 1)[-1]
            targets = resolver.lookup(name)
            if not targets:
                status = "repo_not_registered"
            elif len(targets) > 1:
                status = "ambiguous"
            elif path_in_repo(targets[0], edge["path"]):
                status = "matched"
            else:
                status = "path_not_found"
            try:
                rel = str(Path(edge["file_path"]).relative_to(source_path))
            except ValueError:
                rel = edge["file_path"]
            row: dict[str, Any] = {
                "repo": all_repos[index].get("alias") or source_path.name,
                "file": rel,
                "line": edge["line"],
                "target_repo": name,
                "target_path": edge["path"],
                "ref": edge["ref"],
                "kind": edge["kind"],
                "status": status,
            }
            if status == "matched":
                target = all_repos[targets[0]]
                row["target_repo"] = target.get("alias") or Path(target["path"]).name
            rows.append(row)
    return rows
