"""Edges from application code to the AWS resources it calls through the AWS SDK.

A function that calls ``s3.put_object(Bucket="x")`` gets a ``REFERENCES`` edge to
``external:aws-s3:x``, the same target an infrastructure reference to that bucket uses (see
:func:`~code_review_graph.infra_references.aws_external_target`), so the question "which code
reads or writes this bucket, table, secret, function or state machine" is a ``callers_of`` /
``references_to`` query on that target, and ``cross_repo_requests_tool(origin="awssdk")``
lists them across the registered repos.

``extra`` of such an edge: ``resolution="aws_sdk"``, ``service`` (the ARN service: ``s3``,
``dynamodb``, ``lambda``, ``states``, ``secretsmanager``, ``ssm``, ``kinesis``, ``firehose``,
``events``, ``ecs``, ``glue``, ``batch``), ``operation`` (snake case: ``put_object``),
``resource`` (the name, ``?`` when it cannot be named), ``target_kind``, ``access``
(``read``, ``write``, ``invoke``, or ``unknown`` when the operation name says nothing) and
``sdk``. A name that cannot be worked out is still an edge, to ``external:aws-<service>:?``,
with ``unresolved`` true and, when the value is an environment variable or a property key,
``placeholder`` (the key, never its value).

Only names are recorded: a secret's id, never its value; a bucket and a key prefix, never an
object. A client has to be seen being made (``boto3.client("s3")``), or a request or command
class has to be imported from the SDK, so a local object that happens to have an ``invoke`` or
``put_object`` method makes no edge. Helper functions that hide the client are not followed.

Also here: S3 locations written as ``s3://``/``https://...amazonaws.com`` literals in code,
``aws s3`` commands in shell scripts and workflow files, and the Terraform side of a bucket
(``aws_s3_bucket``, ``aws_s3_bucket_notification`` and bucket ARNs in policies).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, Optional

from .config_keys import normalize_spring_config_key
from .infra_references import aws_external_target, reduce_arn

__all__ = ["aws_sdk_edges", "RESOLUTION", "UNRESOLVED_NAME"]

RESOLUTION = "aws_sdk"
UNRESOLVED_NAME = "?"
_DYN = "\x00"
_MAX_EDGES_PER_FILE = 2000

_LANG_BY_SUFFIX = {
    ".py": "python", ".java": "java", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".mts": "typescript",
    ".cts": "typescript", ".tsx": "tsx",
}
#: Languages whose string literals are scanned for ``s3://`` locations (text scan).
_URI_SUFFIXES = frozenset({
    ".py", ".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".scala",
    ".sc", ".kt", ".kts", ".sh", ".bash", ".tf",
})
_MARKERS: dict[str, tuple[bytes, ...]] = {
    "python": (b"boto3", b"botocore"),
    "java": (b"software.amazon.awssdk", b"com.amazonaws"),
    "javascript": (b"@aws-sdk/",),
    "typescript": (b"@aws-sdk/",),
    "tsx": (b"@aws-sdk/",),
}


class Val(NamedTuple):
    """What an argument says: *template* is its text, ``\\x00`` standing for each dynamic part."""

    template: Optional[str] = None
    placeholder: Optional[str] = None


class _Svc(NamedTuple):
    key: str
    kind: str
    params: tuple[str, ...]


SERVICES: dict[str, _Svc] = {
    "s3": _Svc("s3", "bucket", ("bucket", "bucketname")),
    "dynamodb": _Svc("dynamodb", "table", ("tablename",)),
    "lambda": _Svc("lambda", "function", ("functionname",)),
    "states": _Svc("states", "state-machine", ("statemachinearn",)),
    "secretsmanager": _Svc("secretsmanager", "secret", ("secretid",)),
    "ssm": _Svc("ssm", "parameter", ("name",)),
    "kinesis": _Svc("kinesis", "stream", ("streamname", "streamarn")),
    "firehose": _Svc("firehose", "delivery-stream", ("deliverystreamname",)),
    "events": _Svc("events", "event-bus", ("eventbusname",)),
    "ecs": _Svc("ecs", "task-definition", ("taskdefinition",)),
    "glue": _Svc("glue", "job", ("jobname",)),
    "batch": _Svc("batch", "job-definition", ("jobdefinition",)),
}
#: Per-operation resource parameters that differ from the service's.
_OP_PARAMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("secretsmanager", "create_secret"): ("name",),
    ("ssm", "get_parameters_by_path"): ("path",),
    ("lambda", "create_function"): ("functionname",),
}
#: boto3 ``client(<name>)`` -> service key.
_BOTO_SERVICE = {
    "s3": "s3", "dynamodb": "dynamodb", "lambda": "lambda", "stepfunctions": "states",
    "sfn": "states", "secretsmanager": "secretsmanager", "ssm": "ssm", "kinesis": "kinesis",
    "firehose": "firehose", "events": "events", "eventbridge": "events", "ecs": "ecs",
    "glue": "glue", "batch": "batch",
}
#: Java package segment (SDK v2 and v1) -> service key.
_JAVA_PKG = {
    "s3": "s3", "dynamodb": "dynamodb", "dynamodbv2": "dynamodb", "lambda": "lambda",
    "sfn": "states", "stepfunctions": "states", "secretsmanager": "secretsmanager",
    "ssm": "ssm", "simplesystemsmanagement": "ssm", "kinesis": "kinesis",
    "firehose": "firehose", "kinesisfirehose": "firehose", "eventbridge": "events",
    "cloudwatchevents": "events", "ecs": "ecs", "glue": "glue", "batch": "batch",
}
#: npm package -> service key.
_JS_PKG = {
    "client-s3": "s3", "client-dynamodb": "dynamodb", "lib-dynamodb": "dynamodb",
    "client-lambda": "lambda", "client-sfn": "states", "client-secrets-manager": "secretsmanager",
    "client-ssm": "ssm", "client-kinesis": "kinesis", "client-firehose": "firehose",
    "client-eventbridge": "events", "client-ecs": "ecs", "client-glue": "glue",
    "client-batch": "batch",
}
_LIB_DYNAMODB_OPS = {
    "Get": "get_item", "Put": "put_item", "Update": "update_item", "Delete": "delete_item",
    "Query": "query", "Scan": "scan", "BatchGet": "batch_get_item",
    "BatchWrite": "batch_write_item", "TransactGet": "transact_get_items",
    "TransactWrite": "transact_write_items",
}
#: Operations that name no single resource; never an edge, even with a splatted argument.
_RESOURCELESS = frozenset({
    "list_buckets", "list_tables", "list_functions", "list_state_machines", "list_secrets",
    "list_streams", "list_delivery_streams", "list_event_buses", "list_clusters",
    "list_task_definitions", "list_jobs", "describe_limits", "describe_parameters",
    "list_job_definitions", "describe_endpoints", "get_caller_identity",
})
_NON_OPERATIONS = frozenset({
    "close", "exceptions", "meta", "get_waiter", "can_paginate", "waiter_names", "get_paginator",
    "generate_presigned_url", "generate_presigned_post", "get_available_subresources",
    "wait_until_exists", "wait_until_not_exists", "reload", "load",
})
#: boto3 S3 transfer helpers that take the bucket by position: method -> index.
_S3_POSITIONAL = {
    "upload_file": 1, "upload_fileobj": 1, "download_file": 0, "download_fileobj": 0, "copy": 1,
}

_READ_PREFIXES = (
    "get", "list", "describe", "query", "scan", "head", "select", "batch_get", "transact_get",
    "download", "search", "check", "exists", "does", "fetch", "read", "lookup", "validate",
    "wait",
)
_WRITE_PREFIXES = (
    "put", "delete", "update", "start", "create", "send", "run", "submit", "upload", "copy",
    "batch_write", "transact_write", "batch_writer", "restore", "stop", "register",
    "deregister", "tag", "untag", "publish", "write", "set", "remove", "add", "import",
    "execute", "cancel", "terminate", "reset", "rotate", "replace", "emit", "invoke",
)


def classify_access(service: str, operation: str) -> str:
    """``read``, ``write``, ``invoke`` (Lambda) or ``unknown``, from the operation name alone."""
    op = operation.lower()
    if service == "lambda" and (op == "invoke" or op.startswith("invoke_")):
        return "invoke"
    for prefix in _READ_PREFIXES:
        if op == prefix or op.startswith(prefix + "_"):
            return "read"
    for prefix in _WRITE_PREFIXES:
        if op == prefix or op.startswith(prefix + "_"):
            return "write"
    return "unknown"


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


# -- Names -------------------------------------------------------------------------------

_NAME_RE: dict[str, re.Pattern[str]] = {
    "bucket": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$"),
    "table": re.compile(r"^[A-Za-z0-9_.\-]{1,255}$"),
    "function": re.compile(r"^[A-Za-z0-9_\-]{1,140}$"),
    "state-machine": re.compile(r"^[A-Za-z0-9_.\-]{1,80}$"),
    "secret": re.compile(r"^[A-Za-z0-9/_+=.@\-]{1,512}$"),
    "parameter": re.compile(r"^[A-Za-z0-9_.\-/]{1,1011}$"),
    "stream": re.compile(r"^[A-Za-z0-9_.\-]{1,128}$"),
    "delivery-stream": re.compile(r"^[A-Za-z0-9_.\-]{1,64}$"),
    "event-bus": re.compile(r"^[A-Za-z0-9_.\-/]{1,256}$"),
    "task-definition": re.compile(r"^[A-Za-z0-9_\-]{1,255}$"),
    "job": re.compile(r"^[A-Za-z0-9_.\-]{1,255}$"),
    "job-definition": re.compile(r"^[A-Za-z0-9_\-]{1,128}$"),
}
#: ARN resource prefix per kind for services ``reduce_arn`` does not know.
_OWN_ARN = {
    "bucket": ("s3", ""), "secret": ("secretsmanager", "secret:"),
    "parameter": ("ssm", "parameter"),
    "stream": ("kinesis", "stream/"), "delivery-stream": ("firehose", "deliverystream/"),
    "event-bus": ("events", "event-bus/"),
}
_PROP_PLACEHOLDER = re.compile(r"^\$\{([A-Za-z0-9_.\-]+)(?::[^}]*)?\}$")


def _name_from_arn(kind: str, text: str) -> Optional[str]:
    parts = text.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or _DYN in parts[1] or _DYN in parts[2]:
        return None
    if kind in _OWN_ARN:
        service, prefix = _OWN_ARN[kind]
        if parts[2] != service or not parts[5].startswith(prefix):
            return None
        rest = parts[5][len(prefix):]
        if kind == "bucket":
            rest = rest.split("/", 1)[0]
        elif kind == "secret":
            rest = re.sub(r"-[A-Za-z0-9]{6}$", "", rest)
        elif kind == "parameter":
            rest = rest[1:] if rest.startswith("/") else rest
            rest = rest if rest.count("/") == 0 else "/" + rest
        return rest
    reduced = reduce_arn(text)
    return reduced[2] if reduced else None


def resolve_name(kind: str, value: Val) -> tuple[Optional[str], Optional[str]]:
    """``(name, placeholder)``: the resource name, or ``None`` plus the property/env key."""
    placeholder = value.placeholder
    text = value.template
    if text is None:
        return None, placeholder
    text = text.strip()
    match = _PROP_PLACEHOLDER.match(text)
    if match:
        return None, normalize_spring_config_key(match.group(1))
    if text.startswith("arn:"):
        name = _name_from_arn(kind, text)
    elif _DYN in text:
        name = None
    else:
        name = text
        if kind == "bucket" and name.startswith("s3://"):
            name = name[5:].split("/", 1)[0]
        elif kind in ("function", "task-definition", "job-definition"):
            name = name.split(":", 1)[0]
    if name is None or _DYN in name or not _NAME_RE[kind].match(name):
        return None, placeholder
    return name, None


# -- Source attribution --------------------------------------------------------------------


class _Scopes:
    """Which node an edge at a given line belongs to."""

    def __init__(self, nodes: list[Any], edges: list[Any], file_path: str) -> None:
        self.file_path = file_path
        contained: dict[tuple[int, str], str] = {}
        for edge in edges:
            if edge.kind == "CONTAINS" and "::" in edge.target:
                tail = edge.target.split("::", 1)[1]
                contained[(edge.line, tail.rsplit(".", 1)[-1])] = edge.target
        self.spans: list[tuple[int, int, str]] = []
        for node in nodes:
            if node.kind in ("Function", "Test"):
                qn = contained.get((node.line_start, node.name))
                if qn:
                    self.spans.append((node.line_start, node.line_end, qn))
        self.resources = {e.target for e in edges if e.kind == "CONTAINS"}

    def source(self, line: int) -> str:
        best: Optional[tuple[int, int, str]] = None
        for start, end, qn in self.spans:
            if start <= line <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, qn)
        return best[2] if best else self.file_path

    def resource(self, rtype: str, label: str) -> str:
        qn = f"{self.file_path}::resource.{rtype}.{label}"
        return qn if qn in self.resources else self.file_path


class _Out:
    """Collects deduplicated edges for one file."""

    def __init__(self, scopes: _Scopes, make_edge: Callable[..., Any]) -> None:
        self.scopes = scopes
        self.make_edge = make_edge
        self.seen: set[tuple[Any, ...]] = set()
        self.edges: list[Any] = []

    def add(
        self, service: str, operation: str, value: Val, line: int, sdk: str,
        kind: Optional[str] = None, access: Optional[str] = None, source: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if len(self.edges) >= _MAX_EDGES_PER_FILE or operation in _RESOURCELESS:
            return
        kind = kind or SERVICES[service].kind
        name, placeholder = resolve_name(kind, value)
        src = source or self.scopes.source(line)
        key = (src, service, operation, name, line)
        if key in self.seen:
            return
        self.seen.add(key)
        info: dict[str, Any] = {
            "resolution": RESOLUTION, "service": service, "operation": operation,
            "resource": name or UNRESOLVED_NAME, "target_kind": kind,
            "access": access or classify_access(service, operation), "sdk": sdk,
        }
        if name is None:
            info["unresolved"] = True
            if placeholder:
                info["placeholder"] = placeholder[:128]
            info.update(confidence=0.6, confidence_tier="INFERRED")
        else:
            info.update(confidence=1.0, confidence_tier="EXTRACTED")
        if extra:
            info.update(extra)
        self.edges.append(self.make_edge(
            kind="REFERENCES", source=src,
            target=aws_external_target(service, name or UNRESOLVED_NAME),
            file_path=self.scopes.file_path, line=line, extra=info,
        ))


# -- Tree helpers ---------------------------------------------------------------------------


def _walk(node: Any) -> Iterator[Any]:
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node: Any) -> int:
    return node.start_point[0] + 1


def _unique_consts(pairs: list[tuple[str, str]]) -> dict[str, str]:
    seen: dict[str, Optional[str]] = {}
    for name, value in pairs:
        seen[name] = value if seen.get(name, value) == value else None
    return {k: v for k, v in seen.items() if v is not None}


_ENV_CALL = re.compile(r"^(?:os\.)?(?:environ\.get|getenv)$")
_FORMAT_FIELD = re.compile(r"\{[^{}]*\}|%[sdr]")


# -- Python ---------------------------------------------------------------------------------


class _Binding(NamedTuple):
    kind: str  # client, resource, table, bucket, paginator
    svc: str
    val: Val = Val()
    op: str = ""


class _Python:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.bindings: dict[tuple[tuple[str, int], str], _Binding] = {}
        self.consts: dict[str, str] = {}
        pairs = []
        for node in _walk(root):
            if node.type == "assignment":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier" \
                        and right.type == "string":
                    value = self.val(right)
                    if value.template is not None and _DYN not in value.template:
                        pairs.append((_text(left, src), value.template))
        self.consts = _unique_consts(pairs)

    # values
    def val(self, node: Any) -> Val:
        src = self.src
        t = node.type
        if t == "string":
            prefix = ""
            parts = []
            for c in node.children:
                if c.type == "string_start":
                    prefix = _text(c, src).lower()
                elif c.type == "string_content":
                    parts.append(_text(c, src))
                elif c.type in ("interpolation", "escape_sequence"):
                    parts.append(_DYN)
            return Val() if "b" in prefix else Val("".join(parts))
        if t == "concatenated_string":
            return self._join([self.val(c) for c in node.children if c.type == "string"])
        if t == "parenthesized_expression" and node.named_child_count:
            return self.val(node.named_children[0])
        if t == "binary_operator":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            op = _text(node.children[1], src) if len(node.children) > 1 else ""
            if left is None or right is None:
                return Val()
            if op == "+":
                return self._join([self.val(left), self.val(right)], keep=True)
            if op == "%":
                lv = self.val(left)
                return Val(_FORMAT_FIELD.sub(_DYN, lv.template)) if lv.template else Val()
            return Val()
        if t == "call":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn is None:
                return Val()
            fn_text = _text(fn, src)
            if _ENV_CALL.match(fn_text) and args is not None and args.named_child_count:
                key = self.val(args.named_children[0])
                if key.template and _DYN not in key.template:
                    return Val(None, key.template)
            attr = fn.child_by_field_name("attribute") if fn.type == "attribute" else None
            if attr is not None and _text(attr, src) == "format":
                obj = fn.child_by_field_name("object")
                if obj is not None and obj.type == "string":
                    lv = self.val(obj)
                    return Val(_FORMAT_FIELD.sub(_DYN, lv.template)) if lv.template else Val()
            return Val()
        if t == "subscript":
            value = node.child_by_field_name("value")
            sub = node.child_by_field_name("subscript")
            if value is not None and sub is not None and _text(value, src) in (
                "os.environ", "environ",
            ):
                key = self.val(sub)
                if key.template and _DYN not in key.template:
                    return Val(None, key.template)
            return Val()
        if t == "identifier":
            const = self.consts.get(_text(node, src))
            return Val(const) if const is not None else Val()
        return Val()

    @staticmethod
    def _join(parts: list[Val], keep: bool = False) -> Val:
        out = []
        for part in parts:
            out.append(part.template if part.template is not None else _DYN)
        text = "".join(out)
        return Val(text) if text and (keep or any(p.template for p in parts)) else Val()

    # scopes
    @staticmethod
    def _chain(node: Any) -> list[tuple[str, int]]:
        chain: list[tuple[str, int]] = []
        p = node.parent
        while p is not None:
            if p.type in ("function_definition", "lambda"):
                chain.append(("f", p.start_byte))
            elif p.type == "class_definition":
                chain.append(("c", p.start_byte))
            p = p.parent
        chain.append(("m", 0))
        return chain

    def _store(self, key: str, binding: _Binding, node: Any) -> None:
        chain = self._chain(node)
        if key.startswith("self."):
            scope = next((s for s in chain if s[0] == "c"), chain[0])
        else:
            scope = chain[0]
            if scope[0] == "c":
                self.bindings[(scope, "self." + key)] = binding
        self.bindings[(scope, key)] = binding

    def _lookup(self, key: str, node: Any) -> Optional[_Binding]:
        for scope in self._chain(node):
            if key.startswith("self.") and scope[0] != "c":
                continue
            found = self.bindings.get((scope, key))
            if found is not None:
                return found
        return None

    # bindings
    def _args(self, call: Any) -> tuple[list[Any], dict[str, Any], bool]:
        pos: list[Any] = []
        kws: dict[str, Any] = {}
        splat = False
        args = call.child_by_field_name("arguments")
        if args is None:
            return pos, kws, splat
        for c in args.named_children:
            if c.type == "keyword_argument":
                name = c.child_by_field_name("name")
                value = c.child_by_field_name("value")
                if name is not None and value is not None:
                    kws[_text(name, self.src).lower()] = value
            elif c.type == "dictionary_splat":
                splat = True
            elif c.type != "list_splat" and c.type != "comment":
                pos.append(c)
        return pos, kws, splat

    def expr_binding(self, node: Any) -> Optional[_Binding]:
        t = node.type
        if t in ("identifier", "attribute"):
            text = _text(node, self.src)
            if text.endswith(".meta.client"):
                base = self._lookup(text[: -len(".meta.client")], node)
                return _Binding("client", base.svc) if base and base.kind == "resource" else None
            return self._lookup(text, node)
        if t in ("await", "parenthesized_expression") and node.named_child_count:
            return self.expr_binding(node.named_children[-1])
        if t == "call":
            return self.value_binding(node)
        return None

    def value_binding(self, call: Any) -> Optional[_Binding]:
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            return None
        attr = _text(fn.child_by_field_name("attribute"), self.src)
        recv = fn.child_by_field_name("object")
        pos, kws, _splat = self._args(call)
        if attr in ("client", "resource"):
            arg = pos[0] if pos else kws.get("service_name")
            service = self.val(arg).template if arg is not None else None
            key = _BOTO_SERVICE.get((service or "").lower())
            return _Binding(attr, key) if key else None
        base = self.expr_binding(recv) if recv is not None else None
        if base is None:
            return None
        if attr == "Table" and base.kind == "resource" and base.svc == "dynamodb":
            arg = pos[0] if pos else kws.get("name")
            return _Binding("table", "dynamodb", self.val(arg) if arg is not None else Val())
        if attr in ("Bucket", "Object") and base.kind == "resource" and base.svc == "s3":
            arg = pos[0] if pos else kws.get("name") or kws.get("bucket_name")
            return _Binding("bucket", "s3", self.val(arg) if arg is not None else Val())
        if attr == "get_paginator" and base.kind in ("client", "resource") and pos:
            op = self.val(pos[0]).template
            return _Binding("paginator", base.svc, op=op) if op and _DYN not in op else None
        return None

    def collect_bindings(self) -> None:
        for node in _walk(self.root):
            key_node = value = None
            if node.type == "assignment":
                key_node = node.child_by_field_name("left")
                value = node.child_by_field_name("right")
            elif node.type == "as_pattern" and node.named_child_count >= 2:
                key_node, value = node.named_children[-1], node.named_children[0]
                if key_node.type == "as_pattern_target" and key_node.named_child_count:
                    key_node = key_node.named_children[0]
            if key_node is None or value is None:
                continue
            if key_node.type not in ("identifier", "attribute"):
                continue
            binding = self.expr_binding(value)
            if binding is not None:
                self._store(_text(key_node, self.src), binding, node)

    # call sites
    def run(self) -> None:
        self.collect_bindings()
        for node in _walk(self.root):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "attribute":
                continue
            recv = fn.child_by_field_name("object")
            method = _text(fn.child_by_field_name("attribute"), self.src)
            if recv is None or method.startswith("_") or method in _NON_OPERATIONS:
                continue
            base = self.expr_binding(recv)
            if base is None:
                continue
            self._site(node, base, method)

    def _site(self, call: Any, base: _Binding, method: str) -> None:
        line = _line(call)
        pos, kws, splat = self._args(call)
        sdk = "boto3"
        if base.kind in ("table", "bucket"):
            if method in ("Table", "Bucket", "Object", "get_paginator"):
                return
            self.out.add(base.svc, method, base.val, line, sdk)
            return
        if base.kind == "paginator":
            if method == "paginate":
                self._by_params(base.svc, base.op, kws, splat, line)
            return
        svc = base.svc
        if method in ("Table", "Bucket", "Object") and base.kind == "resource":
            return
        if svc == "s3" and method in _S3_POSITIONAL:
            idx = _S3_POSITIONAL[method]
            arg = kws.get("bucket") or (pos[idx] if len(pos) > idx else None)
            if arg is not None:
                self.out.add(svc, method, self.val(arg), line, sdk)
            return
        self._by_params(svc, method, kws, splat, line)

    def _by_params(
        self, svc: str, op: str, kws: dict[str, Any], splat: bool, line: int,
    ) -> None:
        sdk = "boto3"
        if svc == "events" and op == "put_events":
            entries = kws.get("entries")
            if entries is None:
                if splat:
                    self.out.add(svc, op, Val(), line, sdk)
                return
            buses = [
                self.val(p.child_by_field_name("value"))
                for p in _walk(entries)
                if p.type == "pair" and p.child_by_field_name("key") is not None
                and self.val(p.child_by_field_name("key")).template == "EventBusName"
            ]
            if entries.type in ("list", "dictionary") and not buses:
                buses = [Val("default")]
            elif entries.type not in ("list", "dictionary"):
                buses = [Val()]
            for bus in buses[:10]:
                self.out.add(svc, op, bus, line, sdk)
            return
        if svc == "ssm" and op in ("get_parameters", "delete_parameters"):
            names = kws.get("names")
            if names is not None and names.type == "list":
                for item in names.named_children[:20]:
                    self.out.add(svc, op, self.val(item), line, sdk)
            elif names is not None or splat:
                self.out.add(svc, op, Val(), line, sdk)
            return
        params = _OP_PARAMS.get((svc, op), SERVICES[svc].params)
        for name in params:
            if name in kws:
                self.out.add(svc, op, self.val(kws[name]), line, sdk)
                return
        if splat:
            self.out.add(svc, op, Val(), line, sdk)


# -- Java -----------------------------------------------------------------------------------

_JAVA_IMPORT = re.compile(
    r"^(?:software\.amazon\.awssdk|com\.amazonaws)\.services\.([a-z0-9]+)(?:\.[a-z0-9]+)*?"
    r"\.([A-Za-z0-9_]+|\*)$"
)
_V1_S3_BUCKET_FIRST = frozenset({
    "PutObjectRequest", "GetObjectRequest", "DeleteObjectRequest", "GetObjectMetadataRequest",
})
_V1_S3_DIRECT = frozenset({
    "putObject", "getObject", "deleteObject", "listObjects", "listObjectsV2", "doesObjectExist",
    "getObjectAsString", "getObjectMetadata",
})
_V1_S3_TYPES = frozenset({"AmazonS3", "AmazonS3Client"})
_SPRING_VALUE = re.compile(r"^\$\{([^}:]+)(?::[^}]*)?\}$")


class _Java:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.classes: dict[str, tuple[str, str]] = {}  # simple name -> (service, sdk)
        self.wild: dict[str, str] = {}  # service -> sdk
        self.consts: dict[str, str] = {}
        self.v1_s3_vars: set[str] = set()
        self.string_vars: set[str] = set()
        pairs: list[tuple[str, str]] = []
        self.props: dict[str, Optional[str]] = {}
        for node in _walk(root):
            if node.type == "import_declaration":
                self._import(_text(node, src))
            elif node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
                if name is not None and value is not None and value.type == "string_literal":
                    lit = self.val(value)
                    if lit.template is not None and _DYN not in lit.template:
                        pairs.append((_text(name, src), lit.template))
            if node.type in ("field_declaration", "local_variable_declaration", "formal_parameter"):
                self._declared(node)
        self.consts = _unique_consts(pairs)

    def _import(self, text: str) -> None:
        text = text.replace("import", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
        m = _JAVA_IMPORT.match(text)
        if not m or m.group(1) not in _JAVA_PKG:
            return
        service = _JAVA_PKG[m.group(1)]
        sdk = "aws-sdk-java-v2" if text.startswith("software.") else "aws-sdk-java-v1"
        if m.group(2) == "*":
            self.wild[service] = sdk
        else:
            self.classes[m.group(2)] = (service, sdk)

    def _declared(self, node: Any) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        type_name = _text(type_node, self.src)
        names = [
            _text(d.child_by_field_name("name"), self.src)
            for d in node.children if d.type == "variable_declarator"
        ]
        if node.type == "formal_parameter":
            n = node.child_by_field_name("name")
            names = [_text(n, self.src)] if n is not None else []
        if type_name in _V1_S3_TYPES and type_name in self.classes:
            self.v1_s3_vars.update(names)
        elif type_name == "String":
            self.string_vars.update(names)
            if node.type in ("field_declaration", "formal_parameter"):
                key = self._value_annotation(node)
                for name in names:
                    if name in self.props and self.props[name] != key:
                        self.props[name] = None
                    else:
                        self.props[name] = key

    def _value_annotation(self, node: Any) -> Optional[str]:
        for c in node.children:
            if c.type != "modifiers":
                continue
            for a in c.children:
                a_name = a.child_by_field_name("name") if a.type == "annotation" else None
                if a_name is not None and _text(a_name, self.src) == "Value":
                    args = a.child_by_field_name("arguments")
                    lit = next(
                        (x for x in (args.named_children if args else [])
                         if x.type == "string_literal"), None,
                    )
                    m = _SPRING_VALUE.match(self.val(lit).template or "") if lit is not None \
                        else None
                    if m:
                        return normalize_spring_config_key(m.group(1))
        return None

    def val(self, node: Any) -> Val:
        src = self.src
        t = node.type
        if t == "string_literal":
            parts = [
                _text(c, src) if c.type == "string_fragment" else _DYN
                for c in node.children if c.type in ("string_fragment", "escape_sequence")
            ]
            return Val("".join(parts))
        if t == "parenthesized_expression" and node.named_child_count:
            return self.val(node.named_children[0])
        if t == "binary_expression":
            op = node.child_by_field_name("operator")
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if op is not None and _text(op, src) == "+" and left is not None and right is not None:
                parts = [self.val(left), self.val(right)]
                text = "".join(p.template if p.template is not None else _DYN for p in parts)
                return Val(text) if any(p.template for p in parts) else Val()
            return Val()
        if t == "identifier":
            name = _text(node, src)
            if name in self.consts:
                return Val(self.consts[name])
            if self.props.get(name):
                return Val(None, self.props[name])
            return Val()
        if t == "method_invocation":
            name = node.child_by_field_name("name")
            obj = node.child_by_field_name("object")
            args = node.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_child_count else None
            if name is None or first is None:
                return Val()
            n, o = _text(name, src), (_text(obj, src) if obj is not None else "")
            if o == "System" and n == "getenv" and first.type == "string_literal":
                key = self.val(first).template
                return Val(None, key) if key and _DYN not in key else Val()
            if n == "getProperty" and first.type == "string_literal":
                key = self.val(first).template
                if key and _DYN not in key:
                    return Val(None, normalize_spring_config_key(key))
                return Val()
            if o == "String" and n == "format" and first.type == "string_literal":
                lv = self.val(first)
                return Val(_FORMAT_FIELD.sub(_DYN, lv.template)) if lv.template else Val()
        return Val()

    def run(self) -> None:
        if not self.classes and not self.wild:
            return
        for node in _walk(self.root):
            if node.type == "object_creation_expression":
                t = node.child_by_field_name("type")
                name = _text(t, self.src) if t is not None else ""
                self._request(node, name, builder=False)
            elif node.type == "method_invocation":
                obj = node.child_by_field_name("object")
                name = node.child_by_field_name("name")
                if obj is not None and name is not None and obj.type == "identifier":
                    if _text(name, self.src) == "builder":
                        self._request(node, _text(obj, self.src), builder=True)
                    elif _text(obj, self.src) in self.v1_s3_vars:
                        self._v1_direct(node, _text(name, self.src))

    def _service_of(self, cls: str) -> Optional[tuple[str, str]]:
        if cls in self.classes:
            return self.classes[cls]
        if len(self.wild) == 1 and (cls.endswith("Request") or cls.endswith("RequestEntry")):
            service, sdk = next(iter(self.wild.items()))
            return service, sdk
        return None

    def _request(self, root: Any, cls: str, builder: bool) -> None:
        if not (cls.endswith("Request") or cls.endswith("RequestEntry")):
            return
        found = self._service_of(cls)
        if found is None:
            return
        service, sdk = found
        if cls == "PutEventsRequest":
            return
        params = set(SERVICES[service].params)
        op_name = cls[: -len("RequestEntry")] if cls.endswith("RequestEntry") else cls[:-7]
        operation = _snake(op_name)
        if cls == "PutEventsRequestEntry":
            operation = "put_events"
        params = set(_OP_PARAMS.get((service, operation), tuple(params)))
        value: Optional[Val] = None
        node = root
        while True:
            parent = node.parent
            if parent is None or parent.type != "method_invocation" \
                    or parent.child_by_field_name("object") != node:
                break
            node = parent
            name = _text(parent.child_by_field_name("name"), self.src)
            norm = re.sub(r"^(with|set)", "", name).lower() if not builder else name.lower()
            args = parent.child_by_field_name("arguments")
            if norm in params and args is not None and args.named_child_count and value is None:
                value = self.val(args.named_children[0])
        if value is None and not builder and service == "s3" and cls in _V1_S3_BUCKET_FIRST:
            args = root.child_by_field_name("arguments")
            if args is not None and args.named_child_count:
                value = self.val(args.named_children[0])
        if value is None and cls == "PutEventsRequestEntry":
            value = Val("default")
        if value is None:
            return
        self.out.add(service, operation, value, _line(root), sdk)

    def _v1_direct(self, call: Any, method: str) -> None:
        if method not in _V1_S3_DIRECT:
            return
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_child_count else None
        if first is None:
            return
        is_text = first.type in ("string_literal", "binary_expression") or (
            first.type == "identifier" and (
                _text(first, self.src) in self.string_vars or _text(first, self.src) in self.consts
            )
        ) or first.type == "method_invocation" and self.val(first).placeholder is not None
        if not is_text:
            return
        self.out.add("s3", _snake(method), self.val(first), _line(call), "aws-sdk-java-v1")


# -- JavaScript / TypeScript ----------------------------------------------------------------


class _Js:
    def __init__(self, src: bytes, root: Any, out: _Out) -> None:
        self.src, self.root, self.out = src, root, out
        self.classes: dict[str, tuple[str, str]] = {}  # local name -> (service, original)
        self.namespaces: dict[str, str] = {}  # local namespace -> service
        self.consts: dict[str, str] = {}
        pairs: list[tuple[str, str]] = []
        for node in _walk(root):
            if node.type == "import_statement":
                self._import(node)
            elif node.type == "variable_declarator":
                self._declarator(node, pairs)
        self.consts = _unique_consts(pairs)

    @staticmethod
    def _service(package: str) -> Optional[str]:
        if not package.startswith("@aws-sdk/"):
            return None
        return _JS_PKG.get(package[len("@aws-sdk/"):])

    def _string(self, node: Any) -> Optional[str]:
        if node is None or node.type != "string":
            return None
        return "".join(_text(c, self.src) for c in node.children if c.type == "string_fragment")

    def _import(self, node: Any) -> None:
        source = node.child_by_field_name("source")
        service = self._service(self._string(source) or "")
        if service is None:
            return
        for spec in _walk(node):
            if spec.type == "import_specifier":
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                if name is not None:
                    local = _text(alias or name, self.src)
                    self.classes[local] = (service, _text(name, self.src))
            elif spec.type == "namespace_import" and spec.named_child_count:
                self.namespaces[_text(spec.named_children[0], self.src)] = service

    def _declarator(self, node: Any, pairs: list[tuple[str, str]]) -> None:
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            return
        if value.type == "string" and name.type == "identifier":
            lit = self._string(value)
            if lit:
                pairs.append((_text(name, self.src), lit))
        elif value.type == "template_string" and name.type == "identifier":
            val = self.val(value)
            if val.template and _DYN not in val.template:
                pairs.append((_text(name, self.src), val.template))
        inner = value
        if value.type == "await_expression" and value.named_child_count:
            inner = value.named_children[0]
        if inner.type == "call_expression":
            fn = inner.child_by_field_name("function")
            args = inner.child_by_field_name("arguments")
            if fn is not None and _text(fn, self.src) == "require" and args is not None \
                    and args.named_child_count:
                service = self._service(self._string(args.named_children[0]) or "")
                if service is None:
                    return
                if name.type == "identifier":
                    self.namespaces[_text(name, self.src)] = service
                elif name.type == "object_pattern":
                    for c in name.named_children:
                        if c.type == "shorthand_property_identifier_pattern":
                            n = _text(c, self.src)
                            self.classes[n] = (service, n)
                        elif c.type == "pair_pattern":
                            k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                            if k is not None and v is not None:
                                self.classes[_text(v, self.src)] = (service, _text(k, self.src))

    def val(self, node: Any) -> Val:
        src = self.src
        t = node.type
        if t in ("parenthesized_expression", "as_expression", "non_null_expression",
                 "satisfies_expression") and node.named_child_count:
            return self.val(node.named_children[0])
        if t == "string":
            return Val(self._string(node))
        if t == "template_string":
            parts = []
            for c in node.children:
                if c.type == "string_fragment":
                    parts.append(_text(c, src))
                elif c.type in ("template_substitution", "escape_sequence"):
                    parts.append(_DYN)
            return Val("".join(parts))
        if t == "binary_expression":
            op = node.child_by_field_name("operator")
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or op is None:
                return Val()
            o = _text(op, src)
            if o in ("||", "??"):
                lv = self.val(left)
                return lv if lv.placeholder else Val()
            if o == "+":
                parts = [self.val(left), self.val(right)]
                text = "".join(p.template if p.template is not None else _DYN for p in parts)
                return Val(text) if any(p.template for p in parts) else Val()
            return Val()
        if t == "identifier":
            const = self.consts.get(_text(node, src))
            return Val(const) if const is not None else Val()
        if t in ("member_expression", "subscript_expression"):
            text = _text(node, src)
            m = re.match(r"^process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[['\"]([^'\"]+)['\"]\])$", text)  # noqa: E501
            if m:
                return Val(None, m.group(1) or m.group(2))
        return Val()

    def run(self) -> None:
        if not self.classes and not self.namespaces:
            return
        for node in _walk(self.root):
            if node.type != "new_expression":
                continue
            ctor = node.child_by_field_name("constructor")
            if ctor is None:
                continue
            found: Optional[tuple[str, str]] = None
            if ctor.type == "identifier":
                found = self.classes.get(_text(ctor, self.src))
            elif ctor.type == "member_expression":
                obj, prop = ctor.child_by_field_name("object"), ctor.child_by_field_name("property")
                if obj is not None and prop is not None and _text(obj, self.src) in self.namespaces:
                    found = (self.namespaces[_text(obj, self.src)], _text(prop, self.src))
            if found is None or not found[1].endswith("Command"):
                continue
            self._command(node, found[0], found[1][: -len("Command")])

    def _command(self, node: Any, service: str, base: str) -> None:
        operation = _snake(base)
        if service == "dynamodb" and base in _LIB_DYNAMODB_OPS:
            operation = _LIB_DYNAMODB_OPS[base]
        line = _line(node)
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_child_count else None
        if first is None or operation in _RESOURCELESS:
            return
        if first.type != "object":
            if first.type in ("identifier", "member_expression", "call_expression"):
                self.out.add(service, operation, Val(), line, "aws-sdk-js-v3")
            return
        pairs: dict[str, Any] = {}
        spread = False
        for c in first.named_children:
            if c.type == "pair":
                k = c.child_by_field_name("key")
                if k is not None:
                    key = self._string(k) if k.type == "string" else _text(k, self.src)
                    pairs[key.lower()] = c.child_by_field_name("value")
            elif c.type == "shorthand_property_identifier":
                pairs[_text(c, self.src).lower()] = c
            elif c.type == "spread_element":
                spread = True
        if service == "events" and operation == "put_events":
            entries = pairs.get("entries")
            if entries is None:
                if spread:
                    self.out.add(service, operation, Val(), line, "aws-sdk-js-v3")
                return
            buses = [
                self.val(p.child_by_field_name("value")) for p in _walk(entries)
                if p.type == "pair" and p.child_by_field_name("key") is not None
                and _text(p.child_by_field_name("key"), self.src).strip("'\"").lower()
                == "eventbusname"
            ]
            if entries.type == "array" and not buses:
                buses = [Val("default")]
            elif entries.type != "array":
                buses = [Val()]
            for bus in buses[:10]:
                self.out.add(service, operation, bus, line, "aws-sdk-js-v3")
            return
        for name in _OP_PARAMS.get((service, operation), SERVICES[service].params):
            if name in pairs:
                value = pairs[name]
                val = Val(self.consts.get(_text(value, self.src))) \
                    if value.type == "shorthand_property_identifier" else self.val(value)
                self.out.add(service, operation, val, line, "aws-sdk-js-v3")
                return
        if spread:
            self.out.add(service, operation, Val(), line, "aws-sdk-js-v3")


# -- S3 locations written as strings ------------------------------------------------------------

_BUCKET_DNS = r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]"
_DYNAMIC_AFTER = r"\$\{[^}\s]+\}|\{[A-Za-z_][A-Za-z0-9_.]*\}|%s|\$[A-Za-z_][A-Za-z0-9_]*"
_S3_URI = re.compile(
    rf"\bs3[an]?://(?:(?P<b>{_BUCKET_DNS})(?![A-Za-z0-9.\-_${{}}%])|(?P<d>{_DYNAMIC_AFTER}))"
    r"(?P<k>/[A-Za-z0-9._\-/=]*)?"
)
_S3_VHOST = re.compile(
    rf"https?://(?P<b>{_BUCKET_DNS})\.s3(?:[.\-][a-z0-9\-]+)?\.amazonaws\.com(?P<k>/[A-Za-z0-9._\-/=]*)?"
)
_S3_PATHSTYLE = re.compile(
    rf"https?://s3(?:[.\-][a-z0-9\-]+)?\.amazonaws\.com/(?P<b>{_BUCKET_DNS})(?![A-Za-z0-9.\-_])"
    r"(?P<k>/[A-Za-z0-9._\-/=]*)?"
)
_URI_READ = re.compile(
    r"\.read\b|read_(?:csv|parquet|json|excel|orc|table|feather)|\.load\(|textFile|wholeTextFiles"
    r"|get_object|getObject|download|from_s3|\.ls\(|list_objects|listObjects", re.I,
)
_URI_WRITE = re.compile(
    r"\.write\b|to_(?:csv|parquet|json|excel|orc|feather)\b|\.save\(|saveAs|put_object|putObject"
    r"|upload|\.rm\(|delete_object|deleteObject|copy_object", re.I,
)
#: Names that documentation uses for "some bucket"; an edge to them would be noise.
_DOC_BUCKETS = frozenset({
    "bucket", "my-bucket", "mybucket", "bucket-name", "bucketname", "your-bucket", "example-bucket",
    "example", "my_bucket", "bucket_name", "your-bucket-name", "my-bucket-name",
})


def _dynamic_key(dyn: str) -> Optional[str]:
    """The variable or property named by ``${x}``, ``{x}`` or ``$x``; ``None`` for ``%s``."""
    key = dyn.strip("${}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
        return None
    return normalize_spring_config_key(key) if "." in key else key


def _uri_access(line_text: str) -> str:
    read, write = bool(_URI_READ.search(line_text)), bool(_URI_WRITE.search(line_text))
    return "read" if read and not write else "write" if write and not read else "unknown"


def _scan_uris(text: str, out: _Out) -> None:
    lines = text.splitlines()
    for number, line_text in enumerate(lines, 1):
        if "s3" not in line_text:
            continue
        for rx in (_S3_URI, _S3_VHOST, _S3_PATHSTYLE):
            for m in rx.finditer(line_text):
                bucket = m.group("b")
                if bucket in _DOC_BUCKETS:
                    continue
                value: Val
                if bucket is not None:
                    value = Val(bucket)
                else:
                    value = Val(_DYN, _dynamic_key(m.group("d")))
                prefix = (m.groupdict().get("k") or "").strip("/")
                extra = {"key_prefix": prefix.rsplit("/", 1)[0][:128]} if "/" in prefix else {}
                out.add(
                    "s3", "s3_uri", value, number, "uri-literal", kind="bucket",
                    access=_uri_access(line_text), extra=extra,
                )


# -- Shell and workflow files --------------------------------------------------------------------

_AWS_S3_CMD = re.compile(r"\baws\s+(?:--?\S+(?:[ =]\S+)?\s+)*?s3(?P<api>api)?\s+(?P<sub>[a-z0-9\-]+)(?P<rest>[^\n;|&]*)")  # noqa: E501
_S3_ARG_URI = re.compile(rf"s3://(?:(?P<b>{_BUCKET_DNS})|(?P<d>{_DYNAMIC_AFTER}))(?P<k>/[^\s'\"]*)?")  # noqa: E501
_BUCKET_FLAG = re.compile(r"--bucket[ =]['\"]?(?P<v>[^\s'\"]+)")
_CMD_ACCESS_SRC = {"cp": "read", "sync": "read", "mv": "write", "ls": "read", "rm": "write",
                   "presign": "read", "mb": "write", "rb": "write", "website": "write"}
_CMD_ACCESS_DST = {"cp": "write", "sync": "write", "mv": "write"}


def _shell_value(raw: str) -> Val:
    raw = raw.strip("'\"")
    if re.fullmatch(_BUCKET_DNS, raw):
        return Val(raw)
    m = re.fullmatch(r"\$\{\{\s*(?:env|vars|secrets|inputs)\.([A-Za-z0-9_]+)\s*\}\}", raw)
    if m:
        return Val(_DYN, m.group(1))
    m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)[^}]*\}?", raw)
    return Val(_DYN, m.group(1)) if m else Val(_DYN)


_VALUED_FLAGS = frozenset({
    "--exclude", "--include", "--profile", "--region", "--acl", "--sse", "--storage-class",
    "--content-type", "--cache-control", "--metadata", "--endpoint-url", "--expires",
})


def _words_before(rest: str) -> list[str]:
    """The non-flag words (and not the values of flags) that precede an argument."""
    words: list[str] = []
    skip = False
    for token in rest.split():
        if skip:
            skip = False
        elif token.startswith("-"):
            skip = token in _VALUED_FLAGS
        else:
            words.append(token)
    return words


def _scan_shell(text: str, out: _Out) -> None:
    joined: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not buffer:
            start = number
        if raw.rstrip().endswith("\\"):
            buffer += raw.rstrip()[:-1] + " "
            continue
        joined.append((start, buffer + raw))
        buffer = ""
    if buffer:
        joined.append((start, buffer))
    for number, line_text in joined:
        if "s3" not in line_text:
            continue
        stripped = line_text.lstrip()
        if stripped.startswith("#"):
            continue
        for cmd in _AWS_S3_CMD.finditer(line_text):
            sub, rest = cmd.group("sub"), cmd.group("rest")
            if cmd.group("api"):
                flag = _BUCKET_FLAG.search(rest)
                if flag:
                    op = sub.replace("-", "_")
                    out.add("s3", op, _shell_value(flag.group("v")), number, "aws-cli",
                            kind="bucket")
                continue
            uris = list(_S3_ARG_URI.finditer(rest))
            for idx, m in enumerate(uris):
                if m.group("b"):
                    value = Val(m.group("b"))
                else:
                    value = _shell_value(m.group("d"))
                dest = (
                    idx == 1 if len(uris) > 1 else bool(_words_before(rest[: m.start()]))
                ) and sub in _CMD_ACCESS_DST
                access = (_CMD_ACCESS_DST if dest else _CMD_ACCESS_SRC).get(sub)
                if access is None:
                    continue
                prefix = (m.group("k") or "").strip("/")
                extra = {"key_prefix": prefix.rsplit("/", 1)[0][:128]} if "/" in prefix else {}
                out.add("s3", sub, value, number, "aws-cli", kind="bucket", access=access,
                        extra=extra)


# -- Terraform ---------------------------------------------------------------------------------

_TF_RESOURCE = re.compile(r'^\s*resource\s+"(aws_s3_bucket|aws_s3_bucket_notification)"\s+"([^"]+)"\s*\{', re.M)  # noqa: E501
_TF_ATTR = re.compile(r'^\s*(\w+)\s*=\s*(?:"([^"\n]*)"|(aws_s3_bucket\.[A-Za-z0-9_\-]+)\.\w+)', re.M)  # noqa: E501
_TF_NOTIFY = re.compile(
    r"^\s*(lambda_function_arn|topic_arn|queue_arn)\s*=\s*"
    r'(?:"([^"\n]*)"|aws_(lambda_function|sns_topic|sqs_queue)\.([A-Za-z0-9_\-]+)\.\w+)', re.M,
)
_TF_BUCKET_ARN = re.compile(rf'"arn:aws[a-z\-]*:s3:::(?P<b>{_BUCKET_DNS})(?:/[^"\s]*)?"')
_TF_SERVICE = {"lambda_function": "lambda", "sns_topic": "sns", "sqs_queue": "sqs"}


def _block(text: str, open_at: int) -> str:
    depth, i = 0, open_at
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_at:i + 1]
        i += 1
    return text[open_at:]


def _scan_terraform(text: str, out: _Out) -> None:
    if "aws_s3_bucket" not in text and ":s3:::" not in text:
        return
    labels: dict[str, str] = {}
    blocks: list[tuple[str, str, int, str]] = []
    for m in _TF_RESOURCE.finditer(text):
        body = _block(text, m.end() - 1)
        blocks.append((m.group(1), m.group(2), text.count("\n", 0, m.start()) + 1, body))
        if m.group(1) == "aws_s3_bucket":
            for a in _TF_ATTR.finditer(body):
                if a.group(1) == "bucket" and a.group(2) and "${" not in a.group(2):
                    labels[m.group(2)] = a.group(2)
    for rtype, label, line, body in blocks:
        source = out.scopes.resource(rtype, label)
        if rtype == "aws_s3_bucket":
            name = labels.get(label)
            if name and _NAME_RE["bucket"].match(name):
                out.add("s3", "declare", Val(name), line, "terraform", kind="bucket",
                        access="declare", source=source, extra={"terraform_label": label})
            continue
        bucket: Optional[str] = None
        for a in _TF_ATTR.finditer(body):
            if a.group(1) == "bucket":
                bucket = a.group(2) or labels.get(a.group(3).split(".", 1)[1])
        if not bucket or not _NAME_RE["bucket"].match(bucket):
            continue
        targets = []
        for n in _TF_NOTIFY.finditer(body):
            if n.group(3):
                targets.append(f"{_TF_SERVICE[n.group(3)]}/{n.group(4)}")
            else:
                reduced = reduce_arn(n.group(2) or "")
                if reduced:
                    targets.append(f"{reduced[0]}/{reduced[2]}")
        out.add("s3", "notification", Val(bucket), line, "terraform", kind="bucket",
                access="notify", source=source, extra={"notify_targets": targets[:20]})
    for number, line_text in enumerate(text.splitlines(), 1):
        for m in _TF_BUCKET_ARN.finditer(line_text):
            owner = _tf_owner(text, number, out)
            out.add("s3", "policy", Val(m.group("b")), number, "terraform", kind="bucket",
                    access="policy", source=owner)


def _tf_owner(text: str, line: int, out: _Out) -> str:
    owner = out.scopes.file_path
    for m in re.finditer(r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([^"]+)"\s*\{', text, re.M):
        start = text.count("\n", 0, m.start()) + 1
        body = _block(text, m.end() - 1)
        if start <= line <= start + body.count("\n"):
            owner = out.scopes.resource(m.group(1), m.group(2))
    return owner


# -- Entry point -------------------------------------------------------------------------------

_WORKFLOW_PARTS = (".github/workflows/", ".github/actions/")


def aws_sdk_edges(
    path: Path, source: bytes, nodes: list[Any], edges: list[Any],
    get_parser: Callable[[str], Any],
) -> list[Any]:
    """The AWS SDK, ``s3://``, ``aws s3`` and Terraform bucket edges of one parsed file."""
    suffix = path.suffix.lower()
    posix = path.as_posix()
    is_workflow = suffix in (".yml", ".yaml") and any(p in posix for p in _WORKFLOW_PARTS)
    language = _LANG_BY_SUFFIX.get(suffix)
    sdk_markers = _MARKERS.get(language or "", ())
    wants_sdk = bool(language) and any(m in source for m in sdk_markers)
    wants_uri = (suffix in _URI_SUFFIXES or is_workflow) and b"s3" in source
    if not (wants_sdk or wants_uri):
        return []
    file_node = next((n for n in nodes if n.kind == "File"), None)
    if file_node is None:
        return []
    from .parser import EdgeInfo

    scopes = _Scopes(nodes, edges, file_node.file_path)
    out = _Out(scopes, EdgeInfo)
    if wants_sdk and language:
        parser = get_parser(language)
        if parser is not None:
            root = parser.parse(source).root_node
            runner = {"python": _Python, "java": _Java}.get(language, _Js)
            runner(source, root, out).run()
    if wants_uri:
        text = source.decode("utf-8", "replace")
        if suffix == ".tf":
            _scan_terraform(text, out)
        if suffix in (".sh", ".bash") or is_workflow:
            _scan_shell(text, out)
        if suffix not in (".sh", ".bash") and not is_workflow:
            _scan_uris(text, out)
    return out.edges
