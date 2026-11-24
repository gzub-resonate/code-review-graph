"""Join the AWS resources application code calls to the repos that define them.

``aws_sdk_calls`` stores a code-side call as a ``REFERENCES`` edge to
``external:aws-<service>:<name>`` with ``extra.resolution == "aws_sdk"``. This module reads those
edges from every registered repo (read-only), and for each target looks for the registered repo
that defines it:

* an S3 bucket: a Terraform ``aws_s3_bucket`` whose ``bucket`` is that name (an edge of operation
  ``declare`` that ``aws_sdk_calls`` writes from the Terraform file); the bucket's
  ``aws_s3_bucket_notification`` targets (``lambda/<name>``, ``sns/<name>``, ``sqs/<name>``) come
  back as ``notify_targets``;
* a Lambda function, state machine, table, ECS task definition, Batch job definition or Glue job:
  by the rules of :class:`~code_review_graph.stepfunction_links.Definitions`.

Other services have no owner lookup: their rows are ``not_found``, which says "no registered repo
defines it by a name this join knows", not that the resource does not exist. A name that could not
be worked out is ``unresolved``. A Terraform declaration, notification or policy row is
``declared``. The join is computed on every call; nothing is stored, and no value is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .aws_sdk_calls import RESOLUTION, UNRESOLVED_NAME
from .graph import GraphStore

STATUSES = ("matched", "not_found", "ambiguous", "unresolved", "declared")
_TERRAFORM_OPS = frozenset({"declare", "notification", "policy"})


def read_edges(store: GraphStore) -> list[dict[str, Any]]:
    """The AWS SDK edges of a graph, one row per edge."""
    rows = store._conn.execute(
        "SELECT source_qualified, file_path, line, target_qualified, extra FROM edges "
        "WHERE kind = 'REFERENCES' AND json_valid(extra) "
        "AND json_extract(extra, '$.resolution') = ? ORDER BY file_path, line, id",
        (RESOLUTION,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for source, file_path, line, target, raw in rows:
        try:
            info = json.loads(raw or "{}")
        except ValueError:
            continue
        if not isinstance(info, dict) or not isinstance(info.get("service"), str):
            continue
        name = target.split(":", 2)[-1] if target.startswith("external:aws-") else ""
        out.append({
            "file_path": file_path, "line": line, "source": source,
            "service": info["service"], "kind": info.get("target_kind", ""), "name": name,
            "operation": info.get("operation", ""), "access": info.get("access", "unknown"),
            "sdk": info.get("sdk", ""), "unresolved": bool(info.get("unresolved")),
            "placeholder": info.get("placeholder"),
            "extra": {
                k: info[k] for k in ("notify_targets", "key_prefix") if k in info
            },
        })
    return out


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    defined_in_repo: Any,
) -> list[dict[str, Any]]:
    """Resolve each call to the registered repo that defines its target.

    *defined_in_repo* is ``(repo index, (service, kind, name)) -> bool`` for the services the
    state machine join knows.
    """
    declared: dict[tuple[str, str], set[int]] = {}
    notify: dict[tuple[str, str], list[str]] = {}
    for index, edges in edges_by_repo.items():
        for edge in edges:
            if edge["sdk"] != "terraform" or edge["unresolved"]:
                continue
            key = (edge["service"], edge["name"])
            if edge["operation"] == "declare":
                declared.setdefault(key, set()).add(index)
            for target in edge["extra"].get("notify_targets", []):
                if target not in notify.setdefault(key, []):
                    notify[key].append(target)
    owners: dict[tuple[str, str, str], list[int]] = {}
    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            target = (edge["service"], edge["kind"], edge["name"])
            if edge["unresolved"] or edge["name"] == UNRESOLVED_NAME:
                status, found = "unresolved", []
            elif edge["operation"] in _TERRAFORM_OPS and edge["sdk"] == "terraform":
                status, found = "declared", []
            else:
                if target not in owners:
                    by_tf = set(declared.get((edge["service"], edge["name"]), ()))
                    by_defs = {
                        i for i in range(len(all_repos)) if defined_in_repo(i, target)
                    }
                    owners[target] = sorted(by_tf | by_defs)
                found = owners[target]
                status = "matched" if len(found) == 1 else "ambiguous" if found else "not_found"
            try:
                rel = str(Path(edge["file_path"]).relative_to(source_path))
            except ValueError:
                rel = edge["file_path"]
            row: dict[str, Any] = {
                "repo": all_repos[index].get("alias") or source_path.name,
                "file": rel,
                "line": edge["line"],
                "source": edge["source"].split("::", 1)[-1],
                "target_kind": edge["service"],
                "target_name": edge["name"],
                "target": f"{edge['service']}/{edge['name']}",
                "operation": edge["operation"],
                "access": edge["access"],
                "sdk": edge["sdk"],
                "status": status,
            }
            if edge["placeholder"]:
                row["placeholder"] = edge["placeholder"]
            if edge["extra"].get("key_prefix"):
                row["key_prefix"] = edge["extra"]["key_prefix"]
            if found:
                names = [
                    all_repos[i].get("alias") or Path(all_repos[i]["path"]).name for i in found
                ]
                if status == "matched":
                    row["target_repo"] = names[0]
                else:
                    row["candidates"] = names[:10]
            targets = notify.get((edge["service"], edge["name"]))
            if targets and status in ("matched", "ambiguous", "declared"):
                row["notify_targets"] = targets[:20]
            rows.append(row)
    return rows
