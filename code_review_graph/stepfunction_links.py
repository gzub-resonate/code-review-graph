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
* a resource whose name the Terraform evaluator read (a literal, an interpolation, or the
  ``name`` a module call passes down): the name itself, exact (``module "techops_sns_topic"
  { name = "TechOps" }`` declares the topic ``TechOps``);
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
_MACHINE = ("states", "state-machine")
#: ``kind`` of the marker ``Definitions._read`` adds for a machine that something deploys.
_DEPLOYED_KIND = "state-machine/deployed"
#: The deployed name exactly as a Terragrunt unit or a Terraform declaration gives it: an AWS
#: state machine name is case-sensitive, so ``disrupt-look-a-like`` is not ``disrupt-lookalike``.
_DEPLOYED_EXACT_KIND = "state-machine/deployed-exact"

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


_DECLARED_SQL = (
    "SELECT target_qualified, json_extract(extra, '$.target_kind') FROM edges "
    "WHERE kind = 'REFERENCES' AND json_valid(extra) "
    "AND json_extract(extra, '$.resolution') = 'aws_sdk' "
    "AND json_extract(extra, '$.sdk') = 'terraform' "
    "AND json_extract(extra, '$.operation') = 'declare' "
    "AND COALESCE(json_extract(extra, '$.unresolved'), 0) = 0 "
    "AND target_qualified LIKE 'external:aws-%'"
)


def _declared_names(store: GraphStore) -> list[tuple[tuple[str, str], str]]:
    """``((service, kind), name)`` of each Terraform declaration whose name was evaluated."""
    out: list[tuple[tuple[str, str], str]] = []
    valid = set(RESOURCE_TYPES)
    for target, kind in store._conn.execute(_DECLARED_SQL).fetchall():
        service, _, name = target[len(_EXTERNAL_AWS_PREFIX):].partition(":")
        if (service, kind) in valid and name and not name.startswith("?"):
            out.append(((service, kind), name))
    return out


def _mark_deployed(found: dict[tuple[str, str, str], str], name: str, how: str) -> None:
    """Record that something deploys the machine *name*, apart from how it is defined.

    ``break_machine_ties`` reads it: a definition file in the same repo takes the ordinary
    ``(service, kind, name)`` key first, so the deploying source would otherwise be lost.
    """
    found.setdefault(("states", _DEPLOYED_KIND, "~" + _fold(name)), how)
    if how != "terraform_resource":
        # A unit's ``name`` input and a declaration's evaluated name are the deployed name
        # itself, not a label that the spelling-insensitive join has to guess from.
        found.setdefault(("states", _DEPLOYED_EXACT_KIND, name), how)


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
                if key == _MACHINE:
                    _mark_deployed(found, match["label"], "terraform_resource")
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
                unit_key = ("states", "state-machine", "~" + _fold(row[0]))
                found.setdefault(unit_key, "terragrunt_unit")
                _mark_deployed(found, row[0], "terragrunt_unit")
        for unit in lambda_units(store):
            found[("lambda", "function", unit["name"])] = "lambda_unit"
        # A resource whose name the Terraform evaluator read (``aws_terraform_wiring``: a literal,
        # a ``var.environment`` interpolation, or the ``name`` a module call passes to a module
        # that declares ``name = var.name``): ``module "techops_sns_topic" { name = "TechOps" }``
        # declares the topic ``TechOps`` though no label says so. Exact name only: an AWS name is
        # case-sensitive, so the folded spelling of the label rule is not applied to it.
        for kind_key, name in _declared_names(store):
            found.setdefault((*kind_key, name), "terraform_declaration")
            if kind_key == _MACHINE:
                _mark_deployed(found, name, "terraform_declaration")
        # Facts that only explain a miss (``not_found_reasons``): they never define a target.
        from .not_found_reasons import read_facts

        read_facts(store, found)
        return found

    def defines(self, store: GraphStore, repo_path: Path, target: Any) -> bool:
        """Whether the graph of the repo at *repo_path* defines ``(service, kind, name)``."""
        return self.how(store, repo_path, target) is not None

    def knows(self, repo_path: Path) -> bool:
        """Whether this repo's definitions are already read, so ``defines`` needs no store."""
        return str(repo_path) in self._by_repo

    def prefetch(
        self, entries: list[dict[str, Any]], workers: int,
        out_of_budget: Any = None,
    ) -> None:
        """Read the definitions of *entries* on up to *workers* threads, kept between calls.

        With *out_of_budget* the reads stop at a spent budget (what was read stays kept) and
        ``BudgetSpent`` is raised for the repos not read.
        """
        import logging
        import sqlite3

        from .errors import GraphStoreError
        from .repo_cache import MEMO, SKIPPED, parallel_map, parallel_map_budget
        from .terraform_links import open_store
        from .xrepo_budget import BudgetSpent

        todo = [e for e in entries if str(Path(e["path"])) not in self._by_repo]

        def load(entry: dict[str, Any]) -> dict[tuple[str, str, str], str]:
            failed: list[bool] = []

            def compute() -> dict[tuple[str, str, str], str]:
                store = open_store(dict(entry))
                if store is None:
                    failed.append(True)
                    return {}
                try:
                    return self._read(store)
                except (sqlite3.Error, GraphStoreError, OSError) as exc:
                    logging.getLogger(__name__).warning(
                        "Cannot read definitions of %s: %s", entry["path"], exc,
                    )
                    failed.append(True)
                    return {}
                finally:
                    store.close()

            return MEMO.get("definitions", entry, compute, cacheable=lambda _v: not failed)

        if out_of_budget is None:
            for entry, found in zip(todo, parallel_map(load, todo, workers)):
                self._by_repo[str(Path(entry["path"]))] = found
            return
        left = False
        for entry, found in zip(todo, parallel_map_budget(load, todo, workers, out_of_budget)):
            if found is SKIPPED:
                left = True
            else:
                self._by_repo[str(Path(entry["path"]))] = found
        if left:
            raise BudgetSpent

    def how(self, store: GraphStore | None, repo_path: Path, target: Any) -> Any:
        key = str(repo_path)
        if key not in self._by_repo:
            if store is None:
                return None
            self._by_repo[key] = self._read(store)
        service, kind, name = target
        known = self._by_repo[key]
        return known.get((service, kind, name)) or known.get((service, kind, "~" + _fold(name)))


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    defined_in_repo,
    keep=None,
) -> list[dict[str, Any]]:
    """Resolve each state target to the registered repo that defines it.

    *edges_by_repo* maps an index into *all_repos* to the rows ``read_target_edges`` returned
    for it. *defined_in_repo* is ``(repo index, (service, kind, name)) -> bool``. *keep*, when
    given, is ``edge -> bool``: an edge it rejects gets no row and no owner lookup. Each edge
    resolves on its own, so the rows kept are the ones an unfiltered call would give.
    """
    owners: dict[tuple[str, str, str], list[int]] = {}
    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            if keep is not None and not keep(edge):
                continue
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


def break_machine_ties(
    rows: list[dict[str, Any]], known: dict[str, dict[tuple[str, str, str], str]],
    all_repos: list[dict[str, Any]],
) -> None:
    """Settle an ``ambiguous`` state machine owner when exactly one candidate deploys it.

    A repo whose Terraform resource, declaration or Terragrunt unit names the machine beats a
    repo that only holds its definition file. The row becomes ``matched`` with ``tie_break:
    deployed_declaration_over_definition_file``, ``also_defined_in`` (the other repos) and
    ``also_defined_via`` (``{repo: how}``). Two deploying repos, or none, stay ``ambiguous``.
    A Terragrunt unit or declaration counts only when its name is exactly the machine's
    (``disrupt-look-a-like`` does not deploy ``disrupt-lookalike``); a Terraform resource label
    is matched ignoring case, ``-`` and ``_``. Read-side only: nothing a build writes depends
    on it.
    """
    by_name = {
        str(r.get("alias") or Path(r["path"]).name): known.get(str(Path(r["path"])), {})
        for r in all_repos
    }
    for row in rows:
        names = row.get("candidates")
        if row.get("status") != "ambiguous" or row.get("target_kind") != "states" or not names:
            continue
        target = str(row.get("target_name") or "")
        if not target or target.startswith("?") or len(names) >= 10:
            continue
        key = ("states", _DEPLOYED_KIND, "~" + _fold(target))
        exact = ("states", _DEPLOYED_EXACT_KIND, target)
        facts = [by_name.get(n) for n in names]
        if any(f is None for f in facts):
            continue
        # A Terraform resource label is read spelling-insensitively; a unit's or declaration's
        # name only when it is the same name.
        deployers = [
            n for n, f in zip(names, facts)
            if f and (exact in f or f.get(key) == "terraform_resource")
        ]
        if len(deployers) != 1:
            continue
        winner = deployers[0]
        others = [n for n in names if n != winner]
        via = {}
        for n in others:
            repo_facts = by_name[n]
            via[n] = (
                repo_facts.get(("states", "state-machine", target))
                or repo_facts.get(("states", "state-machine", "~" + _fold(target)))
                or "unknown"
            )
        row["status"] = "matched"
        row["target_repo"] = winner
        row["also_defined_in"] = others
        row["also_defined_via"] = via
        row["tie_break"] = "deployed_declaration_over_definition_file"
        del row["candidates"]
