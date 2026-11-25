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

A call whose name lives in Spring configuration carries ``resolved_names`` (profile -> name, see
:mod:`~code_review_graph.aws_config_names`); each profile's name is joined by exact equality
and the row reports ``names`` and ``matched_profiles``. SQS and SNS publish/listen edges are
read as calls too.

Other services have no owner lookup: their rows are ``not_found``, which says "no registered repo
defines it by a name this join knows", not that the resource does not exist. A name that could not
be worked out is ``unresolved``. A Terraform declaration, notification or policy row is
``declared``. The join is computed on every call; nothing is stored, and no value is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .aws_config_names import config_key_for_getter, config_prefixes
from .aws_sdk_calls import RESOLUTION, UNRESOLVED_NAME
from .aws_terraform_wiring import REF_TYPES, WIRING_OPS
from .graph import GraphStore

STATUSES = ("matched", "not_found", "ambiguous", "unresolved", "declared")
_TERRAFORM_OPS = frozenset({"declare", "notification", "policy"}) | WIRING_OPS
#: Extra fields a Terraform edge carries through to its row.
_ROW_EXTRA = (
    "schedule", "event_sources", "wiring", "route", "protocol", "dead_letter_target",
)


def _config_key(
    store: GraphStore, prefixes: dict[str, set[str]], getter: Any,
) -> tuple[str | None, bool]:
    """The property key a bean getter reads, and whether the repo's config files define it.

    Only a type with exactly one prefix is used, so the key is never a guess between two.
    """
    key = config_key_for_getter(prefixes, getter)
    if key is None:
        return None, False
    defined = store._conn.execute(
        "SELECT 1 FROM nodes WHERE kind = 'ConfigProperty' AND name = ? LIMIT 1", (key,),
    ).fetchone() is not None
    return key, defined


#: Messaging edges (SQS and SNS from Java and Python) are AWS calls too; they carry no
#: ``resolution == "aws_sdk"``, so they are selected by kind and target.
_MESSAGING_TARGETS = (("external:aws-sqs:", "sqs", "queue"), ("external:aws-sns:", "sns", "topic"))
_MESSAGING_UNRESOLVED = "?unresolved"
_NAME_FIELDS = ("resolved_names", "env_placeholder", "names_masked")


def _messaging_row_info(target: str, info: dict[str, Any]) -> dict[str, Any] | None:
    """An ``aws_sdk``-shaped view of a SQS/SNS publish or listen edge, or ``None``."""
    for prefix, service, kind in _MESSAGING_TARGETS:
        if target.startswith(prefix):
            name = target[len(prefix):]
            unresolved = bool(info.get("unresolved")) or name == _MESSAGING_UNRESOLVED
            direction = info.get("direction")
            return {
                **info, "service": service, "target_kind": kind,
                "operation": info.get("operation")
                or ("receive_message" if direction == "listen" else "send_message"),
                "access": info.get("access") or ("read" if direction == "listen" else "write"),
                "sdk": "messaging", "unresolved": unresolved,
                "placeholder": info.get("config_key") or info.get("env_var"),
                "_name": UNRESOLVED_NAME if unresolved else name,
            }
    return None


_NAMES_PER_PROFILE = 5


def merge_resolved_names(into: dict[str, list[str]], raw: Any) -> None:
    """Fold one edge's ``resolved_names`` (a JSON text or a dict) into ``profile -> [names]``.

    Used by ``query_graph external_targets``; each profile keeps at most five distinct names.
    """
    try:
        names = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return
    if not isinstance(names, dict):
        return
    for profile, name in names.items():
        if isinstance(profile, str) and isinstance(name, str):
            bucket = into.setdefault(profile[:64], [])
            if name not in bucket and len(bucket) < _NAMES_PER_PROFILE:
                bucket.append(name[:128])


def read_edges(store: GraphStore) -> list[dict[str, Any]]:
    """The AWS SDK edges of a graph, one row per edge."""
    rows = store._conn.execute(
        "SELECT source_qualified, file_path, line, target_qualified, extra, kind FROM edges "
        "WHERE json_valid(extra) AND ((kind = 'REFERENCES' "
        "AND json_extract(extra, '$.resolution') = ?) OR (kind IN ('CONSUMES', 'PRODUCES') "
        "AND (target_qualified LIKE 'external:aws-sqs:%' "
        "OR target_qualified LIKE 'external:aws-sns:%'))) ORDER BY file_path, line, id",
        (RESOLUTION,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    prefixes = config_prefixes(store._conn)
    for source, file_path, line, target, raw, edge_kind in rows:
        try:
            info = json.loads(raw or "{}")
        except ValueError:
            continue
        if not isinstance(info, dict):
            continue
        name = target.split(":", 2)[-1] if target.startswith("external:aws-") else ""
        if edge_kind != "REFERENCES":
            info = _messaging_row_info(target, info) or {}
            name = info.pop("_name", name)
        if not isinstance(info.get("service"), str):
            continue
        out.append({
            "file_path": file_path, "line": line, "source": source,
            "service": info["service"], "kind": info.get("target_kind", ""), "name": name,
            "operation": info.get("operation", ""), "access": info.get("access", "unknown"),
            "sdk": info.get("sdk", ""), "unresolved": bool(info.get("unresolved")),
            "placeholder": info.get("placeholder"),
            "extra": {
                k: info[k]
                for k in (
                    "notify_targets", "key_prefix", "terraform_label", "terraform_type",
                    "source_ref", *_ROW_EXTRA, *_NAME_FIELDS,
                )
                if k in info
            },
        })
        if (
            info.get("unresolved") and not out[-1]["placeholder"]
            and isinstance(info.get("config_getter"), dict)
        ):
            key, defined = _config_key(store, prefixes, info["config_getter"])
            if key:
                out[-1]["placeholder"] = key
                out[-1]["extra"]["config_defined"] = defined
    _resolve_terraform_refs(out)
    return out


_DECLARE_SQL = (
    "SELECT target_qualified, file_path, line, extra FROM edges "
    "WHERE kind = 'REFERENCES' AND json_valid(extra) "
    "AND json_extract(extra, '$.resolution') = ? "
    "AND json_extract(extra, '$.sdk') = 'terraform' "
    "AND json_extract(extra, '$.operation') = 'declare' "
    "AND COALESCE(json_extract(extra, '$.unresolved'), 0) = 0 "
    "ORDER BY file_path, line, id"
)


def read_declare_edges(store: GraphStore) -> list[dict[str, Any]]:
    """Only the Terraform ``declare`` edges of a graph, in the shape :func:`read_edges` gives.

    A cheap read for a repo whose own AWS calls are not listed but whose declarations are
    still needed to find an owner (``config_defined`` and the other fields are not filled).
    """
    out: list[dict[str, Any]] = []
    for target, file_path, line, raw in store._conn.execute(
        _DECLARE_SQL, (RESOLUTION,),
    ).fetchall():
        try:
            info = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(info, dict) or not target.startswith("external:aws-"):
            continue
        service, _, name = target[len("external:aws-"):].partition(":")
        if not service or not name:
            continue
        out.append({
            "file_path": file_path, "line": line, "source": "", "service": service,
            "kind": info.get("target_kind", ""), "name": name, "operation": "declare",
            "access": info.get("access", "declare"), "sdk": "terraform", "unresolved": False,
            "placeholder": None,
            "extra": {
                k: info[k] for k in ("terraform_label", "terraform_type") if k in info
            },
        })
    return out


def _resolve_terraform_refs(edges: list[dict[str, Any]]) -> None:
    """Name the Terraform references that point at a resource declared in another file.

    A module is a directory, so ``aws_sqs_queue.jobs`` written in ``main.tf`` is the queue that
    ``queues.tf`` of the same directory declares. A reference with no such declaration is left
    unresolved (the edge) or dropped (a target), never guessed.
    """
    declared: dict[tuple[str, str, str], str] = {}
    for edge in edges:
        extra = edge["extra"]
        if (
            edge["sdk"] == "terraform" and edge["operation"] == "declare"
            and not edge["unresolved"] and edge["name"] != UNRESOLVED_NAME
            and extra.get("terraform_type") and extra.get("terraform_label")
        ):
            folder = edge["file_path"].rsplit("/", 1)[0]
            declared[(folder, extra["terraform_type"], extra["terraform_label"])] = edge["name"]
    for edge in edges:
        if edge["sdk"] != "terraform":
            continue
        extra = edge["extra"]
        folder = edge["file_path"].rsplit("/", 1)[0]
        ref = extra.get("source_ref")
        if edge["unresolved"] and isinstance(ref, str) and "." in ref:
            rtype, _, label = ref.partition(".")
            name = declared.get((folder, rtype, label))
            if name:
                edge["name"], edge["unresolved"] = name, False
        targets = extra.get("notify_targets")
        if isinstance(targets, list):
            kept = []
            for target in targets:
                if isinstance(target, str) and target.startswith("ref:"):
                    rtype, _, label = target[4:].partition(".")
                    name = declared.get((folder, rtype, label))
                    target = f"{REF_TYPES[rtype][0]}/{name}" if name and rtype in REF_TYPES else ""
                if target:
                    kept.append(target)
            extra["notify_targets"] = kept


def _declared_by(
    edge: dict[str, Any], matched_profiles: list[str], names_by_profile: Any, index: int,
    declared_at: dict[tuple[str, str, int], tuple[str, Any]], repo: dict[str, Any],
) -> dict[str, Any] | None:
    """Where the matched Terraform declaration is: its repo, file (relative) and line.

    ``None`` when the owner was found by another rule (a Terragrunt unit, a resource label)
    that has no declaration line to point at.
    """
    name = edge["name"]
    if matched_profiles and isinstance(names_by_profile, dict):
        for profile in matched_profiles:
            candidate = names_by_profile.get(profile)
            if isinstance(candidate, str) and (edge["service"], candidate, index) in declared_at:
                name = candidate
                break
    where = declared_at.get((edge["service"], name, index))
    if where is None:
        return None
    root = Path(repo["path"])
    try:
        file = str(Path(where[0]).relative_to(root))
    except ValueError:
        file = where[0]
    return {
        "repo": repo.get("alias") or root.name, "file": file, "line": where[1],
        "name": name,
    }


def resolve_links(
    edges_by_repo: dict[int, list[dict[str, Any]]],
    all_repos: list[dict[str, Any]],
    defined_in_repo: Any,
    context_declarations: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve each call to the registered repo that defines its target.

    *defined_in_repo* is ``(repo index, (service, kind, name)) -> bool`` for the services the
    state machine join knows. *context_declarations* are the Terraform ``declare`` edges of
    registered repos whose own calls are not listed (``read_declare_edges``): they can own a
    target but never get a row, so a code repo is joined to infrastructure it was not
    selected together with.
    """
    declared: dict[tuple[str, str], set[int]] = {}
    #: ``(service, name, repo index) -> (file, line)`` of the first ``declare`` edge.
    declared_at: dict[tuple[str, str, int], tuple[str, Any]] = {}
    notify: dict[tuple[str, str], list[str]] = {}
    for index, edges in edges_by_repo.items():
        for edge in edges:
            if edge["sdk"] != "terraform" or edge["unresolved"]:
                continue
            key = (edge["service"], edge["name"])
            if edge["operation"] == "declare":
                declared.setdefault(key, set()).add(index)
                declared_at.setdefault((*key, index), (edge["file_path"], edge["line"]))
            for target in edge["extra"].get("notify_targets", []):
                if target not in notify.setdefault(key, []):
                    notify[key].append(target)
    for index, edges in (context_declarations or {}).items():
        if index in edges_by_repo:
            continue
        for edge in edges:
            key = (edge["service"], edge["name"])
            declared.setdefault(key, set()).add(index)
            declared_at.setdefault((*key, index), (edge["file_path"], edge["line"]))
    triggers: dict[str, list[str]] = {}
    for index, edges in edges_by_repo.items():
        for edge in edges:
            if edge["sdk"] != "terraform" or edge["unresolved"]:
                continue
            origin = f"{edge['service']}/{edge['name']}"
            for target in edge["extra"].get("notify_targets", []):
                note = f"{origin} ({edge['operation']})"
                if note not in triggers.setdefault(target, []):
                    triggers[target].append(note)
            if edge["operation"] == "route" and edge["extra"].get("route"):
                note = f"api_gateway {edge['extra']['route']}"
                if note not in triggers.setdefault(origin, []):
                    triggers[origin].append(note)
    owners: dict[tuple[str, str, str], list[int]] = {}

    def owners_of(service: str, kind: str, name: str) -> list[int]:
        target = (service, kind, name)
        if target not in owners:
            by_tf = set(declared.get((service, name), ()))
            by_defs = {i for i in range(len(all_repos)) if defined_in_repo(i, target)}
            owners[target] = sorted(by_tf | by_defs)
        return owners[target]

    rows: list[dict[str, Any]] = []
    for index, edges in edges_by_repo.items():
        source_path = Path(all_repos[index]["path"])
        for edge in edges:
            matched_profiles: list[str] = []
            names = edge["extra"].get("resolved_names")
            names = names if isinstance(names, dict) and edge["sdk"] != "terraform" else {}
            if (edge["unresolved"] or edge["name"] == UNRESOLVED_NAME) and not names:
                status, found = "unresolved", []
            elif edge["operation"] in _TERRAFORM_OPS and edge["sdk"] == "terraform":
                status, found = "declared", []
            elif edge["unresolved"] or edge["name"] == UNRESOLVED_NAME:
                # The name is known per profile: join each one, by exact name equality only.
                by_profile = {
                    profile: owners_of(edge["service"], edge["kind"], str(name))
                    for profile, name in names.items()
                }
                matched_profiles = [p for p, repos_ in by_profile.items() if repos_]
                found = sorted({i for p in matched_profiles for i in by_profile[p]})
                status = "matched" if len(found) == 1 else "ambiguous" if found else "not_found"
            else:
                found = owners_of(edge["service"], edge["kind"], edge["name"])
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
            if "config_defined" in edge["extra"]:
                row["config_defined"] = edge["extra"]["config_defined"]
            if names:
                row["names"] = {str(p): str(n) for p, n in names.items()}
                if matched_profiles:
                    row["matched_profiles"] = matched_profiles
            if edge["extra"].get("env_placeholder"):
                row["env_placeholder"] = edge["extra"]["env_placeholder"]
            if edge["extra"].get("names_masked"):
                row["names_masked"] = True
            for field in _ROW_EXTRA:
                if edge["extra"].get(field):
                    row[field] = edge["extra"][field]
            if edge["sdk"] == "terraform" and edge["operation"] == "declare":
                fired_by = triggers.get(row["target"])
                if fired_by:
                    row["triggered_by"] = fired_by[:20]
            if found:
                names = [
                    all_repos[i].get("alias") or Path(all_repos[i]["path"]).name for i in found
                ]
                if status == "matched":
                    row["target_repo"] = names[0]
                    declared_by = _declared_by(
                        edge, matched_profiles, names_by_profile=edge["extra"].get(
                            "resolved_names"),
                        index=found[0], declared_at=declared_at, repo=all_repos[found[0]],
                    )
                    if declared_by:
                        row["declared_by"] = declared_by
                else:
                    row["candidates"] = names[:10]
            targets = notify.get((edge["service"], edge["name"]))
            if targets and status in ("matched", "ambiguous", "declared"):
                row["notify_targets"] = targets[:20]
            rows.append(row)
    return rows
