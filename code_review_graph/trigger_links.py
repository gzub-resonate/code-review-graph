"""Inbound triggers: what invokes a Lambda, queue, state machine, topic or bucket handler.

A query-time join over what the Terraform indexer already stored; nothing is parsed again.
Two kinds of stored facts are read from each repo's graph (read-only):

* the Terraform *wiring* edges of ``aws_terraform_wiring`` (operations ``event_source``,
  ``subscribe``, ``event_target``, ``notification``, ``route``, and a Scheduler schedule's
  ``declare``), with the ``declare`` edges that name the resources they point at;
* the ``REFERENCES`` edges between HCL resource nodes, for the shapes the wiring pass does not
  record (``aws_lambda_permission``, ``aws_pipes_pipe``, ``aws_api_gateway_integration``).

Each row is one ``source -> target`` link with its ``trigger_kind``, the declaring file and
line, and a ``status``:

* ``resolved``: source and target both have a name that was written out (or found in a
  declaration of the same Terraform directory);
* ``source_unresolved``: the firing side is only known by its Terraform label
  (``aws_api_gateway_rest_api.public``), not by a deployed name (it comes from a module
  variable, another directory, or a resource the indexer does not name);
* ``target_unresolved``: the invoked side is only known by its Terraform label.

Rows from the reference-only shapes (``via == "reference"``) are inferences from which resources
an HCL block mentions, not from the attribute that mentions them; they are marked as such.
No value is read and no expression is evaluated.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .aws_sdk_calls import RESOLUTION, UNRESOLVED_NAME
from .aws_terraform_wiring import REF_TYPES
from .graph import GraphStore
from .graph import _sanitize_name as _clean_name

STATUSES = ("resolved", "source_unresolved", "target_unresolved")
TRIGGER_KINDS = (
    "eventbridge_rule", "eventbridge_scheduler", "event_source_mapping", "sns_subscription",
    "s3_notification", "api_gateway", "lambda_permission", "eventbridge_pipe",
)

_WIRING_SQL = (
    "SELECT source_qualified, file_path, line, target_qualified, extra FROM edges "
    "WHERE kind = 'REFERENCES' AND json_valid(extra) "
    "AND json_extract(extra, '$.resolution') = ? AND json_extract(extra, '$.sdk') = 'terraform' "
    "AND json_extract(extra, '$.operation') IN "
    "('declare', 'event_source', 'subscribe', 'event_target', 'notification', 'route') "
    "ORDER BY file_path, line, id"
)
_REF_SQL = (
    "SELECT source_qualified, file_path, line, target_qualified FROM edges "
    "WHERE kind = 'REFERENCES' AND ("
    "source_qualified LIKE '%::resource.aws_lambda_permission.%' "
    "OR source_qualified LIKE '%::resource.aws_pipes_pipe.%' "
    "OR source_qualified LIKE '%::resource.aws_api_gateway_integration.%') "
    "AND target_qualified LIKE '%::resource.aws\\_%' ESCAPE '\\' ORDER BY file_path, line, id"
)
_RESOURCE = re.compile(r"resource\.(aws_[a-z0-9_]+)\.([A-Za-z0-9_\-]+)$")

#: Terraform type -> ``(service, kind)`` of what can invoke through a Lambda permission, and
#: the service principal that implies. The principal is inferred from the type; the literal is
#: not stored by the indexer.
_PERMISSION_SOURCES = {
    "aws_cloudwatch_event_rule": ("events", "rule", "events.amazonaws.com"),
    "aws_sns_topic": ("sns", "topic", "sns.amazonaws.com"),
    "aws_s3_bucket": ("s3", "bucket", "s3.amazonaws.com"),
    "aws_apigatewayv2_api": ("apigateway", "api", "apigateway.amazonaws.com"),
    "aws_api_gateway_rest_api": ("apigateway", "api", "apigateway.amazonaws.com"),
}
_PIPE_SOURCES = {
    "aws_sqs_queue": ("sqs", "queue"),
    "aws_kinesis_stream": ("kinesis", "stream"),
    "aws_dynamodb_table": ("dynamodb", "table"),
}
_PIPE_TARGETS = {
    "aws_lambda_function": ("lambda", "function"),
    "aws_sfn_state_machine": ("states", "state-machine"),
}
_NOTIFY_TYPES = {"lambda": "aws_lambda_function", "sns": "aws_sns_topic",
                 "sqs": "aws_sqs_queue"}
_SERVICE_KIND = {v[0]: v[1] for v in REF_TYPES.values()}


def _parse_target(target: str) -> tuple[str, str]:
    """``(service, name)`` of ``external:aws-<service>:<name>``."""
    service, _, name = target[len("external:aws-"):].partition(":")
    return service, name


def _resource_of(qualified: str) -> tuple[str, str] | None:
    match = _RESOURCE.search(qualified.rsplit("::", 1)[-1])
    return (match.group(1), match.group(2)) if match else None


def _folder(file_path: str) -> str:
    return file_path.rsplit("/", 1)[0]


class _Declared:
    """``(folder, Terraform type, label)`` -> ``(service, name)`` from the ``declare`` edges."""

    def __init__(self) -> None:
        self.by_label: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.rules: dict[str, dict[str, Any]] = {}

    def add(self, edge: dict[str, Any]) -> None:
        extra = edge["extra"]
        if edge["unresolved"] or not extra.get("terraform_type"):
            return
        key = (_folder(edge["file_path"]), extra["terraform_type"], extra.get("terraform_label"))
        self.by_label.setdefault(key, (edge["service"], edge["name"]))
        if extra["terraform_type"] == "aws_cloudwatch_event_rule":
            self.rules.setdefault(edge["name"], extra)

    def name(self, folder: str, rtype: str, label: str) -> str | None:
        found = self.by_label.get((folder, rtype, label))
        return found[1] if found else None


def _side(
    declared: _Declared, folder: str, ref: str | None, service: str, name: str | None,
) -> tuple[str, str, bool]:
    """``(kind, name, resolved)`` of one side given a written-out *name* or a ``type.label`` ref.

    A reference the declarations of the folder cannot name falls back to the Terraform label.
    """
    if name and name != UNRESOLVED_NAME:
        return _SERVICE_KIND.get(service, ""), name, True
    if ref and "." in ref:
        rtype, _, label = ref.removeprefix("ref:").partition(".")
        found = declared.name(folder, rtype, label)
        kind = REF_TYPES[rtype][1] if rtype in REF_TYPES else ""
        if found:
            return kind, found, True
        return kind, f"{rtype}.{label}", False
    return _SERVICE_KIND.get(service, ""), name or UNRESOLVED_NAME, False


def _wiring_edges(store: GraphStore) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source, file_path, line, target, raw in store._conn.execute(
        _WIRING_SQL, (RESOLUTION,),
    ).fetchall():
        try:
            info = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(info, dict) or not target.startswith("external:aws-"):
            continue
        service, name = _parse_target(target)
        out.append({
            "source": source, "file_path": file_path, "line": line, "service": service,
            "name": name, "operation": info.get("operation", ""),
            "unresolved": bool(info.get("unresolved")) or name == UNRESOLVED_NAME,
            "extra": info,
        })
    return out


def _target_items(
    declared: _Declared, edge: dict[str, Any],
) -> list[tuple[str, str, str, bool]]:
    """``(service, kind, name, resolved)`` per target an edge fires (a protocol-only one too)."""
    folder = _folder(edge["file_path"])
    items: list[tuple[str, str, str, bool]] = []
    for raw in edge["extra"].get("notify_targets") or []:
        if not isinstance(raw, str):
            continue
        if raw.startswith("ref:"):
            rtype = raw[4:].partition(".")[0]
            service = REF_TYPES[rtype][0] if rtype in REF_TYPES else ""
            kind, name, ok = _side(declared, folder, raw, service, None)
            items.append((service, kind, name, ok))
        elif "/" in raw:
            service, _, name = raw.partition("/")
            if edge["operation"] == "notification":
                # A bucket notification writes the Terraform label where a reference was used.
                name = declared.name(folder, _NOTIFY_TYPES.get(service, ""), name) or name
            items.append((service, _SERVICE_KIND.get(service, ""), name, True))
    if not items and edge["extra"].get("protocol"):
        # An https/email/sms subscription: the endpoint is a value and is not stored.
        items.append((edge["extra"]["protocol"], "endpoint", edge["extra"]["protocol"], True))
    return items


def _row(
    trigger_kind: str, repo_file: str, line: int, source: tuple[str, str, str, bool],
    target: tuple[str, str, str, bool], via: str, extra: dict[str, Any],
) -> dict[str, Any]:
    s_service, s_kind, s_name, s_ok = source
    t_service, t_kind, t_name, t_ok = target
    status = "resolved" if s_ok and t_ok else "source_unresolved" if not s_ok else (
        "target_unresolved"
    )
    row: dict[str, Any] = {
        "trigger_kind": trigger_kind,
        "source": s_name, "source_service": s_service, "source_kind": s_kind,
        "target": f"{t_service}/{t_name}", "target_name": t_name, "target_service": t_service,
        "target_kind": t_kind,
        "file": repo_file, "line": line, "status": status, "via": via,
    }
    if not s_ok and not t_ok:
        row["target_unresolved"] = True
    row.update({k: v for k, v in extra.items() if v})
    return row


def _wiring_rows(
    edges: list[dict[str, Any]], declared: _Declared,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for edge in edges:
        op, extra = edge["operation"], edge["extra"]
        folder = _folder(edge["file_path"])
        service = edge["service"]
        common = {"file": edge["file_path"], "line": edge["line"]}
        if op == "route":
            route = extra.get("route")
            if not route:
                continue
            rows.append(_row(
                "api_gateway", common["file"], common["line"],
                ("apigateway", "route", route, True), ("lambda", "function", edge["name"], True),
                "terraform", {"route": route},
            ))
            continue
        if op == "declare":
            if extra.get("terraform_type") != "aws_scheduler_schedule":
                continue
            kind, name, ok = _side(declared, folder, None, service, edge["name"])
            for item in _target_items(declared, edge):
                rows.append(_row(
                    "eventbridge_scheduler", common["file"], common["line"],
                    (service, "schedule", name, ok), item, "terraform",
                    {"schedule": extra.get("schedule")},
                ))
            continue
        kind, name, ok = _side(
            declared, folder, extra.get("source_ref"), service,
            None if edge["unresolved"] else edge["name"],
        )
        items = _target_items(declared, edge)
        trigger = {
            "event_source": "event_source_mapping", "subscribe": "sns_subscription",
            "event_target": "eventbridge_rule", "notification": "s3_notification",
        }[op]
        meta: dict[str, Any] = {}
        if op == "event_target" and ok:
            rule = declared.rules.get(name, {})
            meta["schedule"] = rule.get("schedule")
            meta["event_pattern_sources"] = rule.get("event_sources")
        if op == "subscribe" and extra.get("protocol"):
            meta["protocol"] = extra["protocol"]
        for item in items:
            rows.append(_row(
                trigger, common["file"], common["line"], (service, kind, name, ok), item,
                "terraform", meta,
            ))
    return rows


def _reference_rows(
    store: GraphStore, declared: _Declared,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    first_line: dict[tuple[str, str, str], int] = {}
    for source, file_path, line, target in store._conn.execute(_REF_SQL).fetchall():
        owner, found = _resource_of(source), _resource_of(target)
        if owner and found:
            key = (owner[0], owner[1], file_path)
            grouped.setdefault(key, []).append(found)
            first_line[key] = min(line, first_line.get(key, line))
    rows: list[dict[str, Any]] = []
    for (rtype, label, file_path), refs in sorted(grouped.items()):
        line = first_line[(rtype, label, file_path)]
        folder = _folder(file_path)

        def named(ref: tuple[str, str], kinds: dict[str, Any]) -> tuple[str, str, str, bool]:
            service, kind = kinds[ref[0]][0], kinds[ref[0]][1]
            name = declared.name(folder, ref[0], ref[1])
            return service, kind, name or f"{ref[0]}.{ref[1]}", bool(name)

        functions = [r for r in refs if r[0] == "aws_lambda_function"]
        if rtype == "aws_lambda_permission":
            for fn in dict.fromkeys(functions):
                for src in dict.fromkeys(r for r in refs if r[0] in _PERMISSION_SOURCES):
                    principal = _PERMISSION_SOURCES[src[0]][2]
                    rows.append(_row(
                        "lambda_permission", file_path, line, named(src, _PERMISSION_SOURCES),
                        named(fn, _PIPE_TARGETS), "reference", {"principal": principal},
                    ))
        elif rtype == "aws_api_gateway_integration":
            for fn in dict.fromkeys(functions):
                for api in dict.fromkeys(r for r in refs if r[0] == "aws_api_gateway_rest_api"):
                    rows.append(_row(
                        "api_gateway", file_path, line, named(api, _PERMISSION_SOURCES),
                        named(fn, _PIPE_TARGETS), "reference", {},
                    ))
        elif rtype == "aws_pipes_pipe":
            targets = [r for r in dict.fromkeys(refs) if r[0] in _PIPE_TARGETS]
            sources = [r for r in dict.fromkeys(refs) if r[0] in _PIPE_SOURCES]
            for target in targets:
                if len(sources) == 1:
                    source = named(sources[0], _PIPE_SOURCES)
                else:
                    # None, or several that could be the source or a second target: not guessed.
                    source = ("", "", f"aws_pipes_pipe.{label}", False)
                rows.append(_row(
                    "eventbridge_pipe", file_path, line, source,
                    named(target, _PIPE_TARGETS), "reference", {},
                ))
    return rows


def read_triggers(store: GraphStore) -> list[dict[str, Any]]:
    """Every inbound-trigger row of one graph; ``file`` is the stored (absolute) path."""
    edges = _wiring_edges(store)
    declared = _Declared()
    for edge in edges:
        if edge["operation"] == "declare":
            declared.add(edge)
    rows = _wiring_rows(edges, declared)
    rows.extend(_reference_rows(store, declared))
    # A wiring edge and the reference pass can describe one link twice; keep the wiring one.
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for row in rows:
        key = (row["trigger_kind"], row["source"], row["target"], row["file"], row["line"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    unique.sort(key=lambda r: (r["file"], r["line"], r["trigger_kind"], r["source"], r["target"]))
    return unique


def matches(row: dict[str, Any], target: str | None, source: str | None) -> bool:
    """Whether a row passes the ``target`` and ``source`` filters (exact name, or ``svc/name``)."""
    if target:
        wanted = target.strip()
        label = row["target_name"].rsplit(".", 1)[-1]
        if wanted not in (row["target_name"], row["target"], label):
            return False
    if source:
        wanted = source.strip()
        label = row["source"].rsplit(".", 1)[-1]
        if wanted not in (
            row["source"], f"{row['source_service']}/{row['source']}", label,
        ):
            return False
    return True


def finish(row: dict[str, Any], repo: str, root: str) -> dict[str, Any]:
    """Name the repo and make the file relative to it, with every string cleaned."""
    out = dict(row)
    file_path = out["file"]
    out["file"] = file_path[len(root):].lstrip("/") if file_path.startswith(root) else file_path
    out["repo"] = repo
    return {
        k: (_clean_name(v) if isinstance(v, str) else [_clean_name(i) for i in v]
            if isinstance(v, list) else v)
        for k, v in out.items()
    }
