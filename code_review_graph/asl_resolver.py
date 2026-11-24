"""Link Step Functions Task states to what they call: AWS resources and code in the repository.

A Task state that runs a Lambda function or an ECS task names it, and the parser records the
call as a ``REFERENCES`` edge to ``external:aws-lambda:<name>`` or ``external:aws-ecs:<name>``.
Three passes add to that, each with its own ``resolution`` label on the edges it makes:

``asl_replacement_vars``
    Most definitions do not spell the name: the Task's target is a ``${placeholder}`` that the
    deploying Terragrunt unit fills from its ``replacement_vars`` maps. The parser reduces the
    allowlisted ``inputs`` of each unit to naming data on the unit's file node
    (``terragrunt_inputs``: the definition file that ``step_function_file_path`` names, and
    ``{key, service, kind, name}`` per replacement entry; see
    ``docs/INFRASTRUCTURE_REFERENCES.md``). For a Task state of that definition whose target is
    ``${key}``, this pass adds ``state -> external:aws-<service>:<name>``. The name was read,
    not guessed, so the edge records ``EXTRACTED`` in ``extra`` (its ``confidence_tier`` column
    follows the graph-wide rule for a target that names no node, like the parser's literal
    external edges). A placeholder that stands for different names in different units (one
    per environment) gets one edge per distinct name.

``asl_name_convention``
    When the same repository defines that function or task definition in Terraform, a second
    edge goes from the state to the resource. The graph keeps values out, so a Terraform
    resource has no ``function_name`` or ``family`` to read: only its label. The link is
    therefore a naming convention, not a proof. A Lambda ``order-validator`` is matched to the
    resource labelled ``order_validator`` (case and ``-`` / ``_`` are ignored), and only when
    exactly one resource carries that label. The edge is ``INFERRED`` at 0.6.

``asl_lambda_handler``
    A Lambda Terragrunt unit records its ``handler`` (``module.function``), its deployed
    ``function_name`` and the source directory ``git_repo_sub_dir`` evaluates to. A state that
    references ``external:aws-lambda:<name>`` gets an edge to the ``Function`` node
    ``function`` of ``<source dir>/<module>.<ext>`` of the unit deployed as ``<name>``. It is
    ``EXTRACTED`` when both the name and the directory were evaluated statically. When either
    was not, a directory-name convention stands in (the directory above the unit names the
    function, and the handler module is looked for in a directory of that name) and the edge
    is ``INFERRED`` at 0.6. A name whose units disagree on the handler gets no edge. Java
    ``Class::method`` handlers are not linked.

The original external edge is never changed, so a state whose function lives in another
repository, or whose name does not match, keeps it and nothing is guessed.

The links are derived: every pass removes the ones it made before and makes them again, so an
update that touches only the Terraform, only the state machine or only the handler gives what
a full build does.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import TYPE_CHECKING, Any, Optional

from .infra_references import aws_external_target
from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

__all__ = [
    "ASL_CODE_RESOLUTIONS",
    "RESOLUTION",
    "RESOLUTION_LAMBDA_HANDLER",
    "RESOLUTION_REPLACEMENT_VARS",
    "has_lambda_units",
    "lambda_units",
    "resolve_asl_local_targets",
]

RESOLUTION = "asl_name_convention"
RESOLUTION_REPLACEMENT_VARS = "asl_replacement_vars"
RESOLUTION_LAMBDA_HANDLER = "asl_lambda_handler"

#: The resolutions whose edges lead from a state into nodes of the repository.
ASL_CODE_RESOLUTIONS = (RESOLUTION, RESOLUTION_LAMBDA_HANDLER)

_DERIVED = (RESOLUTION, RESOLUTION_REPLACEMENT_VARS, RESOLUTION_LAMBDA_HANDLER)

#: external-target prefix -> the Terraform resource type that defines such a target.
_TARGETS = {
    "external:aws-lambda:": "aws_lambda_function",
    "external:aws-ecs:": "aws_ecs_task_definition",
}
_RESOURCE_NAME = re.compile(r"^resource\.(?P<type>[A-Za-z0-9_]+)\.(?P<label>[^.]+)$")
_LAMBDA_PREFIX = "external:aws-lambda:"
#: File extensions a ``module.function`` handler's module may have, in order.
_HANDLER_EXTENSIONS = (".py", ".js", ".mjs", ".cjs", ".ts")
_INFERRED = {"confidence": 0.6, "confidence_tier": "INFERRED"}
_EXTRACTED = {"confidence": 1.0, "confidence_tier": "EXTRACTED"}


def _fold(name: str) -> str:
    """Spelling-insensitive form of a name: case, ``-`` and ``_`` do not count."""
    return re.sub(r"[-_]", "", name).lower()


def _json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _units(store: GraphStore) -> list[tuple[str, dict[str, Any]]]:
    """``(unit file, terragrunt_inputs)`` of every Terragrunt unit that recorded any."""
    rows = store._conn.execute(  # intentional: bounded post-build maintenance pass
        "SELECT file_path, extra FROM nodes WHERE kind = 'File' AND language = 'hcl' "
        "AND json_valid(extra) AND json_extract(extra, '$.terragrunt_inputs') IS NOT NULL "
        "ORDER BY file_path"
    ).fetchall()
    found: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        info = _json(row["extra"]).get("terragrunt_inputs")
        if isinstance(info, dict):
            found.append((row["file_path"], info))
    return found


def has_lambda_units(store: GraphStore) -> bool:
    """Whether any Terragrunt unit of the graph recorded a Lambda handler."""
    row = store._conn.execute(  # intentional: one bounded read
        "SELECT 1 FROM nodes WHERE kind = 'File' AND language = 'hcl' AND json_valid(extra) "
        "AND json_extract(extra, '$.terragrunt_inputs.handler') IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


def lambda_units(store: GraphStore) -> list[dict[str, Any]]:
    """The Lambda units of a graph: deployed name, unit file, handler node and how it was found.

    One entry per unit that records a handler: ``name`` (``function_name``, else the name of
    the directory above the unit), ``unit`` (the unit's file), ``handler`` (the qualified name
    of the handler's ``Function`` node, or ``None`` when it is not in the graph), ``line``
    and ``static`` (whether both the name and the source directory were evaluated, not taken
    from the directory-name convention).
    """
    conn = store._conn  # intentional: bounded post-build maintenance pass
    found: list[dict[str, Any]] = []
    for unit, info in _units(store):
        handler = info.get("handler")
        if not isinstance(handler, str) or "." not in handler:
            continue
        module, _, function = handler.rpartition(".")
        module = module.replace(".", "/")
        named = info.get("function_name")
        fallback = posixpath.basename(posixpath.dirname(posixpath.dirname(unit)))
        name = named if isinstance(named, str) and named else fallback
        if not name:
            continue
        source_dir = info.get("source_dir")
        static = isinstance(named, str) and bool(named) and isinstance(source_dir, str)
        qualified: Optional[str] = None
        line = 0
        if isinstance(source_dir, str) and source_dir:
            for extension in _HANDLER_EXTENSIONS:
                row = conn.execute(
                    "SELECT qualified_name, line_start FROM nodes WHERE kind = 'Function' "
                    "AND file_path = ? AND name = ? AND parent_name IS NULL "
                    "ORDER BY line_start LIMIT 1",
                    (f"{source_dir}/{module}{extension}", function),
                ).fetchone()
                if row is not None:
                    qualified, line = row["qualified_name"], row["line_start"] or 0
                    break
        else:
            # No directory was evaluated: look for the module in a directory named after the
            # function (``function_name`` first, then the directory above the unit).
            rows = conn.execute(
                "SELECT qualified_name, file_path, line_start FROM nodes "
                "WHERE kind = 'Function' AND name = ? AND parent_name IS NULL AND is_test = 0 "
                "AND language != 'asl' ORDER BY file_path, line_start",
                (function,),
            ).fetchall()
            for directory in dict.fromkeys(n for n in (name, fallback) if n):
                tails = tuple(
                    f"/{directory}/{module}{extension}" for extension in _HANDLER_EXTENSIONS
                )
                matches = [r for r in rows if r["file_path"].endswith(tails)]
                if len({r["file_path"] for r in matches}) == 1:
                    qualified, line = matches[0]["qualified_name"], matches[0]["line_start"] or 0
                if matches:
                    break
        found.append({
            "name": name, "unit": unit, "handler": qualified, "line": line, "static": static,
        })
    return found


def _resolve_replacement_vars(store: GraphStore, stats: dict[str, int]) -> None:
    """``state -> external:aws-<service>:<name>`` for each ``${key}`` a unit fills."""
    conn = store._conn  # intentional: bounded post-build maintenance pass
    #: definition file -> placeholder key -> {(service, kind, name)}
    by_machine: dict[str, dict[str, set[tuple[str, str, str]]]] = {}
    for _unit, info in _units(store):
        if info.get("dynamic_reason") == "depth_limit":
            stats["include_depth_limit"] += 1
        entries = info.get("replacement_vars")
        machine = info.get("state_machine_file")
        if not isinstance(entries, list) or not entries:
            continue
        if not isinstance(machine, str) or not machine:
            stats["units_without_definition"] += 1
            continue
        keys = by_machine.setdefault(machine, {})
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key, service, kind, name = (
                entry.get(field) for field in ("key", "service", "kind", "name")
            )
            if all(isinstance(part, str) and part for part in (key, service, kind, name)):
                keys.setdefault(key, set()).add((service, kind, name))
    for machine in sorted(by_machine):
        keys = by_machine[machine]
        states = conn.execute(
            "SELECT qualified_name, line_start, extra FROM nodes WHERE kind = 'Function' "
            "AND language = 'asl' AND file_path = ? ORDER BY line_start, qualified_name",
            (machine,),
        ).fetchall()
        for state in states:
            placeholders = _json(state["extra"]).get("target_placeholders")
            if not isinstance(placeholders, list):
                continue
            linked = False
            for key in placeholders:
                for service, kind, name in sorted(keys.get(key, ())):
                    store.upsert_edge(EdgeInfo(
                        kind="REFERENCES", source=state["qualified_name"],
                        target=aws_external_target(service, name),
                        file_path=machine, line=state["line_start"] or 0,
                        extra={
                            "resolution": RESOLUTION_REPLACEMENT_VARS,
                            "resource_kind": service, "target_kind": kind,
                            "placeholder": key, **_EXTRACTED,
                        },
                    ))
                    stats["replacement_links"] += 1
                    linked = True
            stats["placeholder_states_linked" if linked else "placeholder_states_open"] += 1


def _resolve_name_convention(store: GraphStore, stats: dict[str, int]) -> None:
    """``state -> Terraform resource`` by label, for Lambda functions and ECS task definitions."""
    conn = store._conn  # intentional: bounded post-build maintenance pass
    resources: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE language = 'hcl' AND kind = 'Class'"
    ):
        match = _RESOURCE_NAME.match(row["name"])
        if match is None or match["type"] not in _TARGETS.values():
            continue
        resources.setdefault(
            (match["type"], _fold(match["label"])), [],
        ).append(row["qualified_name"])
    if not resources:
        return
    rows = conn.execute(
        "SELECT source_qualified, target_qualified, file_path, line, extra FROM edges "
        "WHERE kind = 'REFERENCES' AND (target_qualified LIKE 'external:aws-lambda:%' "
        "OR target_qualified LIKE 'external:aws-ecs:%') ORDER BY id"
    ).fetchall()
    for row in rows:
        target = row["target_qualified"]
        prefix = next(p for p in _TARGETS if target.startswith(p))
        name = target[len(prefix):]
        found = resources.get((_TARGETS[prefix], _fold(name)), [])
        if len(found) > 1:
            stats["ambiguous"] += 1
            continue
        if not found:
            continue
        original = _json(row["extra"])
        extra = {
            key: original[key] for key in ("resource_kind", "target_kind") if key in original
        }
        extra.update({"resolution": RESOLUTION, "external_target": target, **_INFERRED})
        store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source=row["source_qualified"], target=found[0],
            file_path=row["file_path"], line=row["line"], extra=extra,
        ))
        stats["links"] += 1


def _resolve_lambda_handlers(store: GraphStore, stats: dict[str, int]) -> None:
    """``state -> handler Function`` for each state that references a Lambda of this repo."""
    #: deployed name -> handler qualified name -> whether any unit found it statically
    handlers: dict[str, dict[str, bool]] = {}
    for unit in lambda_units(store):
        if unit["handler"] is None:
            stats["handler_not_indexed"] += 1
            continue
        known = handlers.setdefault(unit["name"], {})
        known[unit["handler"]] = known.get(unit["handler"], False) or unit["static"]
    if not handlers:
        return
    rows = store._conn.execute(  # intentional: bounded post-build maintenance pass
        "SELECT source_qualified, target_qualified, file_path, line FROM edges "
        "WHERE kind = 'REFERENCES' AND target_qualified LIKE 'external:aws-lambda:%' "
        "ORDER BY id"
    ).fetchall()
    done: set[tuple[str, str]] = set()
    for row in rows:
        target = row["target_qualified"]
        found = handlers.get(target[len(_LAMBDA_PREFIX):])
        if not found:
            continue
        if len(found) > 1:
            stats["handler_ambiguous"] += 1
            continue
        (qualified, static), = found.items()
        if (row["source_qualified"], qualified) in done:
            continue
        done.add((row["source_qualified"], qualified))
        store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source=row["source_qualified"], target=qualified,
            file_path=row["file_path"], line=row["line"],
            extra={
                "resolution": RESOLUTION_LAMBDA_HANDLER, "resource_kind": "lambda",
                "external_target": target, **(_EXTRACTED if static else _INFERRED),
            },
        ))
        stats["handler_links"] += 1
        if not static:
            stats["handler_links_inferred"] += 1


def resolve_asl_local_targets(store: GraphStore) -> dict[str, int]:
    """Make the derived Step Functions edges again (see the module docstring).

    Returns the counters of the pass. ``links`` and ``ambiguous`` are the Terraform
    name-convention links; ``replacement_links``, ``placeholder_states_linked`` and
    ``placeholder_states_open`` are the placeholder targets (``include_depth_limit`` counts
    the units that include more files than may be read and were left dynamic); and
    ``handler_links`` (``handler_links_inferred`` of them by the directory-name convention),
    ``handler_ambiguous`` and ``handler_not_indexed`` are the handler links.
    """
    conn = store._conn  # intentional: bounded post-build maintenance pass
    removed = 0
    for resolution in _DERIVED:
        removed += conn.execute(
            "DELETE FROM edges WHERE kind = 'REFERENCES' "
            "AND json_valid(extra) AND json_extract(extra, '$.resolution') = ?",
            (resolution,),
        ).rowcount
    stats = dict.fromkeys((
        "links", "ambiguous", "replacement_links", "placeholder_states_linked",
        "placeholder_states_open", "units_without_definition", "include_depth_limit",
        "handler_links", "handler_links_inferred", "handler_ambiguous", "handler_not_indexed",
    ), 0)
    _resolve_replacement_vars(store, stats)
    _resolve_name_convention(store, stats)
    _resolve_lambda_handlers(store, stats)
    store.commit()
    if removed or stats["links"] or stats["replacement_links"] or stats["handler_links"]:
        store._invalidate_cache()
    logger.info("Step Functions target resolution: %s", stats)
    return stats
