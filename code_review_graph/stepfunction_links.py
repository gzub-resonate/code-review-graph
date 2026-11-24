"""Join the AWS resources Step Functions states call to the registered repo that defines them.

A Task state's target is stored in its own repo's graph as a ``REFERENCES`` edge to
``external:aws-<service>:<name>``: written out in the definition, or named from the deploying
Terragrunt unit's replacement maps (``asl_resolver``, ``asl_replacement_vars``). That points
outside the graph, so nothing in the repo that owns the Lambda knew a state machine elsewhere
runs it.

This module reads those edges from every registered repo (read-only) and looks the name up in
what each registered repo defines, by the rules the in-repo links use:

* a Lambda Terragrunt unit deployed under that name (``asl_resolver.lambda_units``: its
  ``function_name``, else the directory above the unit), matched exactly;
* a Terraform resource of the matching type whose label is the name, ignoring case, ``-``
  and ``_`` (``aws_lambda_function``, ``aws_ecs_task_definition``, ``aws_sns_topic``,
  ``aws_sfn_state_machine`` and the other types in :data:`RESOURCE_TYPES`), the naming
  convention of ``asl_name_convention``;
* for a state machine, a Step Functions definition file of that name (same spelling rule),
  or a Step Functions Terragrunt unit whose ``name`` input (the deployed name, which need not
  be the file's: ``flash-1-train-cmd-prep`` deploys ``train_cmd_prep.asl.json``) is that name,
  same spelling rule.

A link is ``matched`` when exactly one registered repo defines the name, ``ambiguous`` when
several do, and ``not_found`` otherwise (the owner is not registered, or names it another
way). The defining repo may be the calling repo itself. The join is computed on every call;
nothing is stored, and no value is read: only names already in the graphs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .asl_resolver import RESOLUTION_REPLACEMENT_VARS, lambda_units
from .graph import GraphStore
from .terraform_links import _like_escape

STATUSES = ("matched", "not_found", "ambiguous")

_EXTERNAL_AWS_PREFIX = "external:aws-"

#: ``(service, target kind)`` -> the Terraform resource type that defines such a target.
RESOURCE_TYPES: dict[tuple[str, str], str] = {
    ("lambda", "function"): "aws_lambda_function",
    ("ecs", "task-definition"): "aws_ecs_task_definition",
    ("ecs", "cluster"): "aws_ecs_cluster",
    ("sns", "topic"): "aws_sns_topic",
    ("states", "state-machine"): "aws_sfn_state_machine",
    ("sqs", "queue"): "aws_sqs_queue",
    ("dynamodb", "table"): "aws_dynamodb_table",
    ("batch", "job-definition"): "aws_batch_job_definition",
    ("glue", "job"): "aws_glue_job",
    ("codebuild", "project"): "aws_codebuild_project",
}
#: The target kind of a service's edge that does not say (the parser omits it when a
#: service has one kind).
_DEFAULT_KIND = {
    "lambda": "function", "ecs": "task-definition", "sns": "topic", "states": "state-machine",
    "sqs": "queue", "dynamodb": "table", "batch": "job-definition", "glue": "job",
    "codebuild": "project",
}
_RESOURCE_NAME = re.compile(r"^resource\.(?P<type>[A-Za-z0-9_]+)\.(?P<label>[^.]+)$")


def _fold(name: str) -> str:
    """Spelling-insensitive form of a name: case, ``-`` and ``_`` do not count."""
    return re.sub(r"[-_]", "", name).lower()


def read_target_edges(store: GraphStore) -> list[dict[str, Any]]:
    """The AWS targets the state machines of a graph call, one row per ``REFERENCES`` edge."""
    rows = store._conn.execute(
        "SELECT e.source_qualified, e.file_path, e.line, e.target_qualified, e.extra, "
        "n.name, n.extra FROM edges e JOIN nodes n ON n.qualified_name = e.source_qualified "
        "WHERE e.kind = 'REFERENCES' AND n.language = 'asl' "
        "AND e.target_qualified LIKE ? ESCAPE '\\' ORDER BY e.file_path, e.line, e.id",
        (_like_escape(_EXTERNAL_AWS_PREFIX) + "%",),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for _source, file_path, line, target, extra, state_name, node_extra in rows:
        service, _, name = target[len(_EXTERNAL_AWS_PREFIX):].partition(":")
        if not service or not name:
            continue
        try:
            info = json.loads(extra or "{}")
            node_info = json.loads(node_extra or "{}")
        except ValueError:
            info, node_info = {}, {}
        kind = info.get("target_kind") if isinstance(info, dict) else None
        if not isinstance(kind, str) or not kind:
            kind = _DEFAULT_KIND.get(service, "")
        resolved = isinstance(info, dict) and info.get("resolution") == RESOLUTION_REPLACEMENT_VARS
        out.append({
            "file_path": file_path,
            "line": line,
            "machine": node_info.get("state_machine") if isinstance(node_info, dict) else None,
            "state": (
                node_info.get("state_path") if isinstance(node_info, dict) else None
            ) or state_name,
            "service": service,
            "kind": kind,
            "name": name,
            "named_by": "replacement_vars" if resolved else "definition",
        })
    return out


class Definitions:
    """What each registered repo defines, read once per repo and call."""

    def __init__(self) -> None:
        self._by_repo: dict[str, dict[tuple[str, str, str], str]] = {}

    def _read(self, store: GraphStore) -> dict[tuple[str, str, str], str]:
        """``(service, kind, name key) -> how it is defined`` for one graph."""
        found: dict[tuple[str, str, str], str] = {}
        by_type = {resource: key for key, resource in RESOURCE_TYPES.items()}
        for row in store._conn.execute(
            "SELECT name FROM nodes WHERE language = 'hcl' AND kind = 'Class' "
            "AND name LIKE 'resource.aws\\_%' ESCAPE '\\'"
        ):
            match = _RESOURCE_NAME.match(row[0])
            key = by_type.get(match["type"]) if match is not None else None
            if key is not None and match is not None:
                found.setdefault((*key, "~" + _fold(match["label"])), "terraform_resource")
        for row in store._conn.execute(
            "SELECT name FROM nodes WHERE language = 'asl' AND kind = 'Class'"
        ):
            found.setdefault(
                ("states", "state-machine", "~" + _fold(row[0])), "state_machine_file",
            )
        # The deployed name of a Step Functions Terragrunt unit (its ``name`` input), which
        # need not be the definition file's name (``flash-1-train-cmd-prep`` deploys
        # ``train_cmd_prep.asl.json``).
        for row in store._conn.execute(
            "SELECT json_extract(extra, '$.terragrunt_inputs.state_machine_name') FROM nodes "
            "WHERE kind = 'File' AND language = 'hcl' AND json_valid(extra) "
            "AND json_extract(extra, '$.terragrunt_inputs.state_machine_name') IS NOT NULL"
        ):
            if isinstance(row[0], str) and row[0]:
                key = ("states", "state-machine", "~" + _fold(row[0]))
                found.setdefault(key, "terragrunt_unit")
        for unit in lambda_units(store):
            found[("lambda", "function", unit["name"])] = "lambda_unit"
        return found

    def defines(self, store: GraphStore, repo_path: Path, target: Any) -> bool:
        """Whether the graph of the repo at *repo_path* defines ``(service, kind, name)``."""
        return self.how(store, repo_path, target) is not None

    def how(self, store: GraphStore, repo_path: Path, target: Any) -> Any:
        key = str(repo_path)
        if key not in self._by_repo:
            self._by_repo[key] = self._read(store)
        service, kind, name = target
        known = self._by_repo[key]
        return known.get((service, kind, name)) or known.get((service, kind, "~" + _fold(name)))


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    defined_in_repo,
) -> list[dict[str, Any]]:
    """Resolve each state target to the registered repo that defines it.

    *edges_by_repo* maps an index into *all_repos* to the rows ``read_target_edges`` returned
    for it. *defined_in_repo* is ``(repo index, (service, kind, name)) -> bool``.
    """
    owners: dict[tuple[str, str, str], list[int]] = {}
    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            target = (edge["service"], edge["kind"], edge["name"])
            if target not in owners:
                owners[target] = [
                    i for i in range(len(all_repos)) if defined_in_repo(i, target)
                ]
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
                "machine": edge["machine"],
                "state": edge["state"],
                "target_kind": edge["service"],
                "target_name": edge["name"],
                "target": f"{edge['service']}/{edge['name']}",
                "named_by": edge["named_by"],
                "status": status,
            }
            if edge["kind"] != _DEFAULT_KIND.get(edge["service"]):
                row["target_subkind"] = edge["kind"]
            if found:
                names = [
                    all_repos[i].get("alias") or Path(all_repos[i]["path"]).name for i in found
                ]
                if status == "matched":
                    row["target_repo"] = names[0]
                else:
                    row["candidates"] = names[:10]
            rows.append(row)
    return rows
