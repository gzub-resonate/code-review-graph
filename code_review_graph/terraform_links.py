"""Join a stack's remote Terraform module sources to the registered repo that holds them.

A Terragrunt ``terraform { source = "git::ssh://git@github.com/org/repo.git//modules/x" }``
is stored in its own repo's graph as an ``IMPORTS_FROM`` edge to
``external:terraform-module:<base>``, with the ``subdir`` and ``ref`` in the edge's ``extra``.
That points outside the graph, so nothing in the module's own repo knew it was used.

This module reads those edges from every registered repo (read-only) and resolves the last
segment of ``<base>`` to a registered repo, the way ``cross_repo_requests`` resolves a
service name. A link is ``matched`` when the repo is registered and the ``subdir`` holds
files in its graph, ``module_not_found`` when the repo is registered but the folder is not
in its graph, ``ambiguous`` when several registered repos answer to the name, and
``repo_not_registered`` otherwise. A Terraform registry address (``namespace/name/provider``,
``registry.terraform.io/...`` or ``tfr://...``) names a published module, not a git repo, so it
is ``registry_module`` and is never matched against registered repos. The join is computed on
every call; nothing is stored.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .errors import GraphStoreError
from .graph import GraphStore
from .infra_references import EXTERNAL_DYNAMIC_TERRAFORM_TARGET, EXTERNAL_TERRAFORM_MODULE_PREFIX
from .repo_aliases import RepoNameResolver

logger = logging.getLogger(__name__)

STATUSES = (
    "matched", "module_not_found", "ambiguous", "repo_not_registered", "registry_module",
)

_REGISTRY_HOST = "registry.terraform.io/"
_REGISTRY_ADDRESS = re.compile(r"\w[\w-]*/[\w.-]+/[\w.-]+")


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def repo_name_of(base: str) -> str:
    """The repository name a module address ends in (``.../org/repo`` gives ``repo``)."""
    return base.rstrip("/").rsplit("/", 1)[-1]


def registry_module_name(base: str, source_kind: Optional[str] = None) -> Optional[str]:
    """The module name when *base* is a Terraform registry address, else ``None``.

    Registry addresses are ``namespace/name/provider``, optionally behind
    ``registry.terraform.io/`` or ``tfr://``. A git source (``git::``, ``github.com/...``,
    another ``://`` scheme) is never one.
    """
    text = base
    if text.startswith("tfr://"):
        text = text[len("tfr://"):].lstrip("/")
        source_kind = "registry"
    elif "://" in text:
        return None
    if text.startswith(_REGISTRY_HOST):
        text = text[len(_REGISTRY_HOST):]
        source_kind = "registry"
    elif source_kind in ("git", "archive", "local"):
        return None
    elif text.split("/", 1)[0].lower() == "github.com":
        return None
    if not _REGISTRY_ADDRESS.fullmatch(text):
        return None
    return text.split("/")[1]


def read_module_edges(store: GraphStore) -> list[dict[str, Any]]:
    """The remote module sources a graph names, one row per ``IMPORTS_FROM`` edge."""
    rows = store._conn.execute(
        "SELECT source_qualified, file_path, line, target_qualified, extra FROM edges "
        "WHERE kind = 'IMPORTS_FROM' AND target_qualified LIKE ? ESCAPE '\\'",
        (_like_escape(EXTERNAL_TERRAFORM_MODULE_PREFIX) + "%",),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for source, file_path, line, target, extra in rows:
        if target == EXTERNAL_DYNAMIC_TERRAFORM_TARGET:
            continue
        try:
            info = json.loads(extra or "{}")
        except ValueError:
            info = {}
        out.append({
            "file_path": file_path or source,
            "line": line,
            "base": target[len(EXTERNAL_TERRAFORM_MODULE_PREFIX):],
            "subdir": info.get("subdir"),
            "ref": info.get("ref"),
            "source_kind": info.get("source_kind"),
        })
    return out


def module_folder_exists(store: GraphStore, repo_path: Path, subdir: Optional[str]) -> bool:
    """Whether the graph of the repo at *repo_path* holds a file under *subdir*."""
    prefix = str(repo_path).rstrip("/") + "/" + (subdir or "").strip("/")
    row = store._conn.execute(
        "SELECT 1 FROM nodes WHERE kind = 'File' AND file_path LIKE ? ESCAPE '\\' LIMIT 1",
        (_like_escape(prefix.rstrip("/")) + "/%",),
    ).fetchone()
    return row is not None


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    folder_exists,
) -> list[dict[str, Any]]:
    """Resolve each remote module source to a registered repo and folder.

    *edges_by_repo* maps an index into *all_repos* to the rows ``read_module_edges``
    returned for it. *folder_exists* is ``(target_index, subdir) -> bool``.
    """
    resolver = RepoNameResolver(all_repos)
    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            registry_name = registry_module_name(edge["base"], edge.get("source_kind"))
            name = registry_name or repo_name_of(edge["base"])
            targets = [] if registry_name else resolver.lookup(name)
            if registry_name:
                status = "registry_module"
            elif not targets:
                status = "repo_not_registered"
            elif len(targets) > 1:
                status = "ambiguous"
            elif folder_exists(targets[0], edge["subdir"]):
                status = "matched"
            else:
                status = "module_not_found"
            try:
                rel = str(Path(edge["file_path"]).relative_to(source_path))
            except ValueError:
                rel = edge["file_path"]
            row: dict[str, Any] = {
                "repo": all_repos[index].get("alias") or source_path.name,
                "file": rel,
                "line": edge["line"],
                "module_repo": name,
                "module_path": edge["subdir"],
                "ref": edge["ref"],
                "status": status,
            }
            if status == "matched":
                target = all_repos[targets[0]]
                row["module_repo"] = target.get("alias") or Path(target["path"]).name
            rows.append(row)
    return rows


def open_store(entry: dict[str, Any]) -> Optional[GraphStore]:
    """A read-only store for a registry entry, or ``None`` when it cannot be read."""
    from .incremental import get_db_path

    try:
        db_path = get_db_path(Path(entry["path"]), read_only=True)
        if not db_path.is_file():
            return None
        return GraphStore(db_path, read_only=True)
    except (GraphStoreError, sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Cannot read %s: %s", entry.get("path"), exc)
        return None
