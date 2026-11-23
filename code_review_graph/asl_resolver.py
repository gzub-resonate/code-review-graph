"""Link Step Functions Task states to the Terraform resource that defines what they call.

A Task state that runs a Lambda function or an ECS task names it, and the parser records the
call as a ``REFERENCES`` edge to ``external:aws-lambda:<name>`` or ``external:aws-ecs:<name>``.
When the same repository defines that function or task definition in Terraform, this pass adds a
second edge from the state to the resource, so ``references_to`` on the resource lists the
state machines that run it.

The graph keeps values out, so a Terraform resource has no ``function_name`` or ``family`` to
read: only its label. The link is therefore a naming convention, not a proof. A Lambda
``order-validator`` is matched to the resource labelled ``order_validator`` (case and ``-`` / ``_``
are ignored), and only when exactly one resource carries that label. The edge says so: it is
``INFERRED`` at 0.6 and its ``resolution`` is ``asl_name_convention``. The original external edge is
never changed, so a state whose function lives in another repository, or whose name does not
match, keeps it and nothing is guessed.

The links are derived: every pass removes the ones it made before and makes them again, so an
update that touches only the Terraform or only the state machine gives what a full build does.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

__all__ = ["RESOLUTION", "resolve_asl_local_targets"]

RESOLUTION = "asl_name_convention"

#: external-target prefix -> the Terraform resource type that defines such a target.
_TARGETS = {
    "external:aws-lambda:": "aws_lambda_function",
    "external:aws-ecs:": "aws_ecs_task_definition",
}
_RESOURCE_NAME = re.compile(r"^resource\.(?P<type>[A-Za-z0-9_]+)\.(?P<label>[^.]+)$")


def _fold(name: str) -> str:
    """Spelling-insensitive form of a name: case, ``-`` and ``_`` do not count."""
    return re.sub(r"[-_]", "", name).lower()


def resolve_asl_local_targets(store: GraphStore) -> dict[str, int]:
    """Add an inferred edge from each Task state to the Terraform resource it names.

    Returns ``{"links": n, "ambiguous": n}``.
    """
    conn = store._conn  # intentional: bounded post-build maintenance pass
    removed = conn.execute(
        "DELETE FROM edges WHERE kind = 'REFERENCES' "
        "AND json_valid(extra) AND json_extract(extra, '$.resolution') = ?",
        (RESOLUTION,),
    ).rowcount

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

    links = ambiguous = 0
    if resources:
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
                ambiguous += 1
                continue
            if not found:
                continue
            try:
                original = json.loads(row["extra"] or "{}")
            except (TypeError, json.JSONDecodeError):
                original = {}
            extra = {
                key: original[key]
                for key in ("resource_kind", "target_kind")
                if isinstance(original, dict) and key in original
            }
            extra.update({
                "resolution": RESOLUTION,
                "external_target": target,
                "confidence": 0.6,
                "confidence_tier": "INFERRED",
            })
            store.upsert_edge(EdgeInfo(
                kind="REFERENCES", source=row["source_qualified"], target=found[0],
                file_path=row["file_path"], line=row["line"], extra=extra,
            ))
            links += 1
    store.commit()
    if links or removed:
        store._invalidate_cache()
    result = {"links": links, "ambiguous": ambiguous}
    logger.info("Step Functions local target resolution: %s", result)
    return result
