"""Terraform wiring between AWS resources: what triggers what, and what each repo declares.

``aws_sdk_calls`` already records an ``aws_s3_bucket`` and its notifications. This module reads the
rest of the wiring from the ``resource`` blocks of a ``.tf`` file, with the same rules (names that
are written out, never a value, no expression is run):

* **Declarations** of a queue, topic, Lambda function, DynamoDB table, EventBridge rule or
  EventBridge Scheduler schedule: an edge of operation ``declare``. A rule or schedule keeps its
  schedule expression (``cron(...)`` or ``rate(...)``), a rule its event pattern's ``source``
  values, and a queue the name of its dead-letter queue.
* **Triggers**: a Lambda event source mapping (queue, stream or table to function), an SNS
  subscription (topic to Lambda or queue), an EventBridge target (rule to Lambda, state machine,
  queue, topic or ECS cluster), a Scheduler schedule's target, and an API Gateway v2 route
  (route key to the Lambda behind its integration). The edge sits on the resource that fires and
  lists what it reaches in ``notify_targets`` (``lambda/<name>``), as a bucket notification does;
  a route sits on the Lambda itself, with the route key.

A reference to a resource in another file of the module (``aws_sqs_queue.jobs.arn``) cannot be
named from this file alone, so the edge keeps ``source_ref`` or a ``ref:<type>.<label>`` target
and ``aws_sdk_links`` resolves it from the declarations of the same directory.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .infra_references import reduce_arn

#: Terraform type -> ``(service, target kind, name attribute)`` of what it declares.
DECLARED: dict[str, tuple[str, str, str]] = {
    "aws_sqs_queue": ("sqs", "queue", "name"),
    "aws_sns_topic": ("sns", "topic", "name"),
    "aws_lambda_function": ("lambda", "function", "function_name"),
    "aws_dynamodb_table": ("dynamodb", "table", "name"),
    "aws_cloudwatch_event_rule": ("events", "rule", "name"),
    "aws_scheduler_schedule": ("scheduler", "schedule", "name"),
}
#: Terraform type -> ``(service, kind)`` of a resource that can be named in an ARN attribute.
REF_TYPES: dict[str, tuple[str, str]] = {
    **{rtype: (v[0], v[1]) for rtype, v in DECLARED.items()},
    "aws_s3_bucket": ("s3", "bucket"),
    "aws_sfn_state_machine": ("states", "state-machine"),
    "aws_ecs_cluster": ("ecs", "cluster"),
    "aws_kinesis_stream": ("kinesis", "stream"),
}
WIRING_OPS = frozenset({"event_source", "subscribe", "event_target", "schedule_target", "route"})
MARKERS = (
    b"aws_sqs_queue", b"aws_sns_topic", b"aws_lambda_function", b"aws_lambda_event_source_mapping",
    b"aws_dynamodb_table", b"aws_cloudwatch_event", b"aws_scheduler_schedule",
    b"aws_apigatewayv2",
)
_MAX_TARGETS = 20

_ATTR_LINE = re.compile(r"^[ \t]*([A-Za-z_][\w\-]*)[ \t]*=[ \t]*(\S.*?)[ \t]*$")
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_COMMENT = re.compile(r"[ \t]+(?:#|//).*$")
_LITERAL = re.compile(r'"([^"\n$]*)"')
_INTERP = re.compile(r'"\$\{(.+)\}"')
_REF = re.compile(
    r"(aws_[a-z0-9_]+)\.([A-Za-z0-9_\-]+)(?:\[[^\]\n]*\])?\.[A-Za-z_]\w*"
)
_SCHEDULE = re.compile(r"(?:cron|rate|at)\([^()\n]{1,100}\)")
_EVENT_SOURCE = re.compile(r'"?source"?[ \t]*[:=][ \t]*\[([^\]\n]{1,300})\]')
_QUOTED = re.compile(r'"([A-Za-z0-9_.\-]{1,100})"')
_DEAD_LETTER = re.compile(
    r'deadLetterTargetArn"?[ \t]*[:=][ \t]*(?:"([^"\n]*)"|(aws_sqs_queue\.[A-Za-z0-9_\-]+)'
    r"(?:\[[^\]\n]*\])?\.\w+)"
)
_NESTED_TARGET_ARN = re.compile(
    r"^[ \t]*target[ \t]*\{(?P<body>.*?)^[ \t]*\}", re.M | re.S,
)
_NESTED_ARN_ATTR = re.compile(r"^[ \t]*arn[ \t]*=[ \t]*(\S.*?)[ \t]*$", re.M)
_ROUTE_KEY = re.compile(r"^(?:[A-Z]{3,7}[ \t]+/[^\s\"]{0,200}|\$default)$")
_INTEGRATION_ID = re.compile(r"aws_apigatewayv2_integration\.([A-Za-z0-9_\-]+)\.id")
_LAMBDA_IN_URI = re.compile(r"function:([A-Za-z0-9_\-]{1,140})(?:[:/]|$)")
_ARN_KINESIS = re.compile(r"^arn:aws[a-z\-]*:kinesis:[^:]*:[^:]*:stream/([A-Za-z0-9_.\-]{1,128})$")
_ARN_EVENT_RULE = re.compile(
    r"^arn:aws[a-z\-]*:events:[^:]*:[^:]*:rule/(?:[^/]+/)?([A-Za-z0-9_.\-]{1,64})$"
)
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")


def _brace_delta(line: str) -> int:
    text = _COMMENT.sub("", _STRING.sub('""', line))
    return text.count("{") - text.count("}")


def top_attrs(body: str) -> dict[str, str]:
    """The ``name = value`` lines at the top level of a block body, as written (first wins)."""
    attrs: dict[str, str] = {}
    depth = 0
    for line in body.split("\n"):
        if depth == 1:
            match = _ATTR_LINE.match(line)
            if match and match.group(1) not in attrs:
                attrs[match.group(1)] = _COMMENT.sub("", match.group(2)).strip()
        depth += _brace_delta(line)
    return attrs


def _literal(raw: Optional[str]) -> Optional[str]:
    match = _LITERAL.fullmatch(raw.strip()) if raw else None
    return match.group(1) if match else None


def _ref_of(raw: Optional[str]) -> Optional[tuple[str, str]]:
    """``(type, label)`` of an attribute reference such as ``aws_sqs_queue.jobs.arn``."""
    if not raw:
        return None
    text = raw.strip()
    interp = _INTERP.fullmatch(text)
    if interp:
        text = interp.group(1).strip()
    match = _REF.fullmatch(text)
    return (match.group(1), match.group(2)) if match else None


class _Names:
    """``(Terraform type, label)`` -> declared name, for the declarations of one file."""

    def __init__(self, found: dict[tuple[str, str], str]) -> None:
        self.found = found

    def resolve(
        self, raw: Optional[str], default_service: Optional[str] = None,
    ) -> Optional[tuple[str, Optional[str], Optional[str]]]:
        """``(service, name or None, ref or None)`` of an ARN, name or reference, else ``None``.

        A reference to a resource not declared in this file comes back with ``name`` ``None``
        and the ``type.label`` in ``ref``, for the link step to resolve.
        """
        ref = _ref_of(raw)
        if ref is not None and ref[0] in REF_TYPES:
            service = REF_TYPES[ref[0]][0]
            name = self.found.get(ref)
            return service, name, (None if name else f"{ref[0]}.{ref[1]}")
        text = _literal(raw)
        if text is None:
            return None
        if text.startswith("arn:"):
            reduced = reduce_arn(text)
            if reduced is not None:
                return reduced[0], reduced[2], None
            for pattern, service in ((_ARN_KINESIS, "kinesis"), (_ARN_EVENT_RULE, "events")):
                match = pattern.match(text)
                if match:
                    return service, match.group(1), None
            return None
        if default_service and _PLAIN_NAME.match(text):
            return default_service, text.split(":", 1)[0], None
        return None


def _target(resolved: Optional[tuple[str, Optional[str], Optional[str]]]) -> Optional[str]:
    if resolved is None:
        return None
    service, name, ref = resolved
    if name:
        return f"{service}/{name}"
    return f"ref:{ref}" if ref else None


def scan_wiring(
    blocks: list[tuple[int, int, str, str, str]],
    bucket_names: dict[str, str],
    line_of: Callable[[int], int],
    out: Any,
    val: Callable[..., Any],
    name_ok: Callable[[str, str], bool],
    max_edges: int,
) -> None:
    """Write the declaration and trigger edges of the resource *blocks* of one file to *out*."""
    parsed = [
        (start, rtype, label, top_attrs(body), body)
        for start, _end, rtype, label, body in blocks
    ]
    found: dict[tuple[str, str], str] = {
        ("aws_s3_bucket", label): name for label, name in bucket_names.items()
    }
    for _start, rtype, label, attrs, _body in parsed:
        if rtype in DECLARED:
            _service, kind, attr = DECLARED[rtype]
            name = _literal(attrs.get(attr))
            if name and name_ok(kind, name):
                found[(rtype, label)] = name
    names = _Names(found)
    integrations: dict[str, str] = {}
    for _start, rtype, label, attrs, _body in parsed:
        if rtype == "aws_apigatewayv2_integration":
            uri = attrs.get("integration_uri")
            ref = _ref_of(uri)
            name = names.found.get(ref) if ref and ref[0] == "aws_lambda_function" else None
            if name is None and uri:
                match = _LAMBDA_IN_URI.search(uri)
                name = match.group(1) if match else None
            if name:
                integrations[label] = name

    def edge(
        service: str, kind: str, op: str, name: Optional[str], line: int, rtype: str,
        label: str, extra: dict[str, Any],
    ) -> None:
        if len(out.edges) >= max_edges:
            return
        source = out.scopes.resource(rtype, label)
        out.add(service, op, val(name), line, "terraform", kind=kind, access="declare"
                if op == "declare" else "notify", source=source, extra=extra)

    for start, rtype, label, attrs, body in parsed:
        line = line_of(start)
        if rtype in DECLARED:
            service, kind, _attr = DECLARED[rtype]
            name = found.get((rtype, label))
            if not name:
                continue
            extra: dict[str, Any] = {"terraform_label": label, "terraform_type": rtype}
            schedule = _literal(attrs.get("schedule_expression"))
            if schedule and _SCHEDULE.fullmatch(schedule):
                extra["schedule"] = schedule
            if rtype == "aws_cloudwatch_event_rule":
                pattern = _EVENT_SOURCE.search(body)
                if pattern:
                    sources = [s for s in _QUOTED.findall(pattern.group(1)) if "." in s]
                    if sources:
                        extra["event_sources"] = sources[:5]
            if rtype == "aws_sqs_queue":
                dead = _DEAD_LETTER.search(body)
                if dead:
                    literal, ref = dead.group(1), dead.group(2)
                    raw = f'"{literal}"' if literal is not None else f"{ref}.arn"
                    target = _target(names.resolve(raw))
                    if target and target.startswith("sqs/"):
                        extra["dead_letter_target"] = target
            if rtype == "aws_scheduler_schedule":
                nested = _NESTED_TARGET_ARN.search(body)
                arn = _NESTED_ARN_ATTR.search(nested.group("body")) if nested else None
                target = _target(names.resolve(arn.group(1) if arn else None))
                if target:
                    extra["notify_targets"] = [target]
            edge(service, kind, "declare", name, line, rtype, label, extra)
        elif rtype == "aws_lambda_event_source_mapping":
            source = names.resolve(attrs.get("event_source_arn"))
            target = _target(names.resolve(attrs.get("function_name"), "lambda"))
            _fire(edge, source, target, "event_source", line, rtype, label, "event_source_mapping")
        elif rtype == "aws_sns_topic_subscription":
            topic = names.resolve(attrs.get("topic_arn"))
            protocol = _literal(attrs.get("protocol"))
            target = None
            if protocol in ("lambda", "sqs"):
                target = _target(names.resolve(attrs.get("endpoint")))
            extra = {"wiring": "sns_subscription"}
            if protocol and re.fullmatch(r"[a-z\-]{2,20}", protocol):
                extra["protocol"] = protocol
            _fire(edge, topic, target, "subscribe", line, rtype, label, None, extra)
        elif rtype == "aws_cloudwatch_event_target":
            rule_attr = attrs.get("rule")
            rule = names.resolve(rule_attr, "events")
            target = _target(names.resolve(attrs.get("arn")))
            _fire(edge, rule, target, "event_target", line, rtype, label, "event_target")
        elif rtype == "aws_apigatewayv2_route":
            route_key = _literal(attrs.get("route_key"))
            match = _INTEGRATION_ID.search(attrs.get("target", ""))
            function = integrations.get(match.group(1)) if match else None
            if function and route_key and _ROUTE_KEY.match(route_key):
                edge("lambda", "function", "route", function, line, rtype, label,
                     {"wiring": "api_gateway", "route": route_key})


def _fire(
    edge: Callable[..., None],
    source: Optional[tuple[str, Optional[str], Optional[str]]],
    target: Optional[str],
    op: str,
    line: int,
    rtype: str,
    label: str,
    wiring: Optional[str],
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Write the edge of a resource that fires *target*, on *source* (the firing resource)."""
    if source is None:
        return
    service, name, ref = source
    kind = next((v[1] for v in REF_TYPES.values() if v[0] == service), "")
    info: dict[str, Any] = dict(extra or {})
    if wiring:
        info["wiring"] = wiring
    if target:
        info["notify_targets"] = [target]
    if ref:
        info["source_ref"] = ref
    if not target and "protocol" not in info:
        return
    edge(service, kind, op, name, line, rtype, label, info)
