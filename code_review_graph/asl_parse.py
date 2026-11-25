"""Amazon States Language (AWS Step Functions) state machines as graph nodes.

State machines are plain JSON, so they are read with the bundled JSON grammar.
Only names and shape enter the graph, never values; see :func:`parse_asl`.
:class:`code_review_graph.parser.CodeParser` calls it for ``.asl.json`` and
``.asl`` files and passes in the JSON parser and its node and edge classes, as
the Ember and Scala modules do, so this module never imports the parser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .constants import normalize_file_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .parser import EdgeInfo, NodeInfo

__all__ = ["parse_asl"]

# Limits on what one definition may cost.
_MAX_ASL_BYTES = 1_048_576
_MAX_ASL_JSON_DEPTH = 64
_MAX_ASL_JSON_NODES = 200_000
_MAX_ASL_STATES = 2_000
_MAX_ASL_NESTING = 8
_MAX_ASL_LIST_ITEMS = 500
_MAX_ASL_OBJECT_KEYS = 2_000
_MAX_ASL_STRING_BYTES = 4_096

# State machines are plain JSON, so the structural parser reads them with the
# bundled JSON grammar. Only names and shape enter the graph: never ``Parameters``,
# ``InputPath``/``OutputPath``/``ResultPath``, ``Choice`` variables or values, or
# ``Comment`` text. From a resource ARN only the resource name survives.
_ASL_STATE_TYPES = frozenset({
    "Task", "Choice", "Parallel", "Map", "Wait", "Pass", "Succeed", "Fail",
})
# Unquoted ``${key}`` template variables (Terraform ``templatefile``, Terragrunt)
# make a definition invalid JSON until it is rendered. Quote them so the shape
# can still be read; a quoted string is skipped whole and left alone.
_ASL_QUOTE_OR_DOLLAR_RE = re.compile(rb'["$]')
#: The text of a string after its opening quote, up to the closing quote, a raw
#: newline or the end. It always matches (possibly empty), so it never backtracks.
_ASL_STRING_BODY_RE = re.compile(rb'(?:[^"\\\n]+|\\[^\n])*')
_ASL_UNQUOTED_PLACEHOLDER_RE = re.compile(rb'\$\{[^}"\n]{0,256}\}')
_ASL_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.\-]{0,127})\}")
_ASL_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,256}")
_ASL_SERVICE_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,39}")
_ASL_ACTION_RE = re.compile(r"[A-Za-z0-9]{1,64}(?:\.sync(?::2)?|\.waitForTaskToken)?")
_ASL_OPTIMIZED_RESOURCE_RE = re.compile(r"arn:[a-z\-]+:states:::(.+)")
# Optimized integrations (and the Lambda ARN form) whose Parameters name the
# thing a Task calls. Value: the Parameters fields to read, in order, with the
# external-target kind for each. Fields whose value is a path or an intrinsic
# are dynamic and keep no text.
_ASL_TARGET_FIELDS: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("lambda", "invoke"): (("FunctionName", "function"),),
    ("states", "startExecution"): (("StateMachineArn", "state-machine"),),
    ("ecs", "runTask"): (("TaskDefinition", "task-definition"), ("Cluster", "cluster")),
    ("sns", "publish"): (("TopicArn", "topic"),),
    ("sqs", "sendMessage"): (("QueueUrl", "queue"),),
    ("dynamodb", "getItem"): (("TableName", "table"),),
    ("dynamodb", "putItem"): (("TableName", "table"),),
    ("dynamodb", "updateItem"): (("TableName", "table"),),
    ("dynamodb", "deleteItem"): (("TableName", "table"),),
    ("batch", "submitJob"): (("JobDefinition", "job-definition"),),
    ("glue", "startJobRun"): (("JobName", "job"),),
    ("codebuild", "startBuild"): (("ProjectName", "project"),),
}
_ASL_EXTERNAL_PREFIX = {
    "lambda": "aws-lambda", "states": "aws-states", "ecs": "aws-ecs",
    "sns": "aws-sns", "sqs": "aws-sqs", "dynamodb": "aws-dynamodb",
    "batch": "aws-batch", "glue": "aws-glue", "codebuild": "aws-codebuild",
}
# ARN resource part -> name, by target kind: the prefix before the name.
_ASL_ARN_RESOURCE_PREFIX = {
    "function": "function:", "state-machine": "stateMachine:",
    "task-definition": "task-definition/", "cluster": "cluster/",
    "table": "table/", "job-definition": "job-definition/",
}


class _AslObj(dict):
    """A JSON object read for ASL, with the lines it spans."""

    __slots__ = ("line_start", "line_end")

    def __init__(self, line_start: int, line_end: int) -> None:
        super().__init__()
        self.line_start = line_start
        self.line_end = line_end


_ASL_NUMBER_TEXT = re.compile(r"-?\d{1,20}(?:\.\d{1,20})?(?:[eE][+-]?\d{1,4})?")


class _AslNumber(int):
    """A JSON number as the ASL reader hands it on: equal to ``0``, with its text kept.

    No number is ever used as a value, so every one compares as ``0``. The text of a short,
    well-formed one is kept as ``text`` for a Choice condition to show; anything else has
    ``text`` empty, and the condition then shows ``?``, never a number it did not read.
    """

    text: str = ""

    @classmethod
    def read(cls, node: Any) -> "_AslNumber":
        number = cls(0)
        raw = node.text.decode("utf-8", errors="replace") if node.text else ""
        if _ASL_NUMBER_TEXT.fullmatch(raw):
            number.text = raw
        return number


def _asl_quote_placeholders(source: bytes) -> bytes:
    """Quote unquoted ``${key}`` template variables so the JSON grammar accepts them."""
    if b"${" not in source:
        return source
    # One pass, each byte read once. A string is skipped whole; one that never
    # closes on its line is abandoned at the line's end, never retried from a
    # later quote (the definition is not valid JSON then, whatever is quoted).
    out = bytearray()
    copied = 0
    position = 0
    size = len(source)
    while position < size:
        found = _ASL_QUOTE_OR_DOLLAR_RE.search(source, position)
        if found is None:
            break
        position = found.start()
        if source[position] == 0x22:  # a string
            body = _ASL_STRING_BODY_RE.match(source, position + 1)
            end = body.end() if body is not None else position + 1
            closed = end < size and source[end] == 0x22
            position = end + 1 if closed else max(end, position + 1)
            continue
        placeholder = _ASL_UNQUOTED_PLACEHOLDER_RE.match(source, position)
        if placeholder is None:
            position += 1
            continue
        out += source[copied:position] + b'"' + placeholder.group(0) + b'"'
        copied = position = placeholder.end()
    out += source[copied:]
    return bytes(out)


_ASL_ERROR_NAME = re.compile(r"(?:States\.[A-Za-z]+|[A-Za-z_][A-Za-z0-9_.]*)")
_MAX_ASL_ERROR_NAMES = 5
_MAX_ASL_ERROR_NAME_LEN = 64


def _asl_error_equals(rule: Any) -> list[str]:
    """Error names a Catch rule lists, in order.

    Only ``States.*`` names and plain identifier-shaped custom names up to 64
    characters are kept, at most five; anything else is dropped.
    """
    names = rule.get("ErrorEquals") if isinstance(rule, _AslObj) else None
    kept: list[str] = []
    for name in names if isinstance(names, list) else []:
        if (
            isinstance(name, str)
            and len(name) <= _MAX_ASL_ERROR_NAME_LEN
            and _ASL_ERROR_NAME.fullmatch(name)
        ):
            kept.append(name)
            if len(kept) >= _MAX_ASL_ERROR_NAMES:
                break
    return kept


_MAX_ASL_CONDITION_CHARS = 160
_MAX_ASL_CONDITION_VALUE_CHARS = 48
_MAX_ASL_CONDITION_DEPTH = 3
_MAX_ASL_CONDITION_TERMS = 6
_ASL_CONDITION_NOT_A_TEST = frozenset({"Variable", "Next", "Comment"})
_ASL_COMPARISON_RE = re.compile(r"(?:String|Numeric|Boolean|Timestamp)[A-Za-z]*|Is[A-Z][A-Za-z]*")


#: Characters that Unicode files under letters or symbols but that draw nothing: the Hangul
#: fillers, the Braille blank and the Khmer inherent vowels (the Default_Ignorable code points
#: that the category test below keeps). Listed by code point, since the standard library has no
#: Default_Ignorable property.
_ASL_BLANK = frozenset(
    "ᅟᅠㅤﾠ"  # Hangul choseong, jungseong and (half)width fillers
    "⠀"  # Braille pattern blank
    "឴឵"  # Khmer vowel inherent Aq and Aa
    "᠎"  # Mongolian vowel separator
)


def _asl_clean(text: str) -> str:
    """NFKC-normalise, then keep only characters that are visible.

    Letters, digits, punctuation, symbols and the plain space stay; control, format, joiner,
    combining, surrogate, private-use, unassigned and separator characters are removed, and so
    are the blank-drawing letters and symbols in :data:`_ASL_BLANK`. Every check below runs on
    this text, and this text is what is shown, so nothing can split a token for the check and
    be invisible when it is shown.
    """
    import unicodedata

    return "".join(
        ch for ch in unicodedata.normalize("NFKC", text)
        if ch not in _ASL_BLANK and (ch == " " or unicodedata.category(ch)[0] in "LNPS")
    )


_ASL_SECRET_SHAPE = re.compile(r"-----BEGIN|://[^/\s]*:[^/\s]*@|\bBearer\s+\S{12,}", re.I)
_ASL_WORD_SPLIT = re.compile(r"[\s'\"(){}\[\]%=,;<>|&]+")


def _asl_literal_is_sensitive(text: str) -> bool:
    """Whether a Choice operand or JSONata expression holds something shaped like a credential.

    The same token shapes the engine refuses elsewhere (``aws_config_names``): an access key,
    an account id, a ``ghp_``/``xox``/``eyJ``/``sk-`` prefix, a long mixed letter-digit run,
    plus a PEM header, credentials in a URL and a bearer token. A rule's test is shown to
    whoever queries, so such a value is replaced, never trimmed.
    """
    from .aws_config_names import _looks_like_token

    if _ASL_SECRET_SHAPE.search(text):
        return True
    return any(_looks_like_token(piece) for piece in _ASL_WORD_SPLIT.split(text) if piece)


def _asl_condition_value(key: str, value: Any) -> str:
    """One comparison operand: a quoted string, a number, a boolean or a ``...Path``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _AslNumber):
        return value.text or "?"
    if isinstance(value, (int, float)):
        return str(value)[:_MAX_ASL_CONDITION_VALUE_CHARS]
    if isinstance(value, str):
        text = _asl_clean(value)
        if not key.endswith("Path") and _asl_literal_is_sensitive(text):
            return '"<redacted>"'
        if len(text) > _MAX_ASL_CONDITION_VALUE_CHARS:
            text = text[: _MAX_ASL_CONDITION_VALUE_CHARS - 1] + "~"
        return text if key.endswith("Path") else f'"{text}"'
    return "?"


def _asl_condition(rule: Any, depth: int = 0) -> Optional[str]:
    """The test of a Choice rule as one short line, or ``None`` when it has none to read.

    ``$.mode StringEquals "scale_down"``, ``not $.x IsPresent true``,
    ``($.a BooleanEquals true and $.b NumericGreaterThan 3)``. A compound rule keeps at most
    six terms and three levels, and the whole line is cut at 160 characters, so a crafted
    definition cannot make the edge large.
    """
    if not isinstance(rule, _AslObj):
        return None
    if depth >= _MAX_ASL_CONDITION_DEPTH:
        return "..."
    for word in ("And", "Or"):
        terms = rule.get(word)
        if isinstance(terms, list):
            parts = [
                part for part in
                (_asl_condition(term, depth + 1) for term in terms[:_MAX_ASL_CONDITION_TERMS])
                if part
            ]
            if len(terms) > _MAX_ASL_CONDITION_TERMS:
                parts.append("...")
            return f"({f' {word.lower()} '.join(parts)})" if parts else None
    if "Not" in rule:
        inner = _asl_condition(rule.get("Not"), depth + 1)
        return f"not {inner}" if inner else None
    expression = rule.get("Condition")
    if isinstance(expression, str):
        return _asl_condition_value("Condition", expression).strip('"')
    variable = rule.get("Variable")
    for key, value in rule.items():
        if key in _ASL_CONDITION_NOT_A_TEST or not _ASL_COMPARISON_RE.fullmatch(key):
            continue
        subject = _asl_clean(variable) if isinstance(variable, str) else "?"
        if _asl_literal_is_sensitive(subject):
            subject = "<redacted>"
        subject = subject[:_MAX_ASL_CONDITION_VALUE_CHARS]
        return f"{subject} {key} {_asl_condition_value(key, value)}"
    return None


def _asl_condition_text(rule: Any) -> Optional[str]:
    """:func:`_asl_condition`, cut to the length an edge keeps."""
    text = _asl_condition(rule)
    if text and len(text) > _MAX_ASL_CONDITION_CHARS:
        text = text[: _MAX_ASL_CONDITION_CHARS - 1] + "~"
    return text


def read_choice_conditions(
    source: bytes, json_parser: Any,
) -> dict[tuple[int, int], Optional[str]]:
    """The test of each Choice rule in an ASL file, by ``(line, index)`` of the rule.

    ``(line, index)`` is what a ``choice`` edge stores (``edge.line`` and ``extra.index``), so
    ``next_states_of`` can name the rule an edge came from. The graph never stores a Choice
    rule's variable or value (the build keeps definitions value-free); this reads them from
    the file when a query asks, with the same size and depth caps the build applies. A
    position that two rules share (a file written on one line) maps to ``None``, never to a
    guess.
    """
    if len(source) > _MAX_ASL_BYTES or json_parser is None:
        return {}
    root = json_parser.parse(_asl_quote_placeholders(source)).root_node
    if root.has_error:
        return {}
    values = [c for c in root.named_children if c.type != "comment"]
    budget = [_MAX_ASL_JSON_NODES]
    document = _asl_read_value(values[0], 0, budget, set()) if len(values) == 1 else None
    if not isinstance(document, _AslObj):
        return {}
    found: dict[tuple[int, int], Optional[str]] = {}
    stack: list[Any] = [document]
    visited = 0
    while stack and visited < _MAX_ASL_JSON_NODES:
        current = stack.pop()
        visited += 1
        if isinstance(current, _AslObj):
            choices = current.get("Choices")
            for index, rule in enumerate(choices if isinstance(choices, list) else []):
                if not isinstance(rule, _AslObj):
                    continue
                key = (rule.line_start, index)
                text = _asl_condition_text(rule)
                found[key] = text if key not in found or found[key] == text else None
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict, list)))
    return found


def _sanitize_asl_name(name: str) -> str:
    """Drop control characters from a state or machine name and cap its length."""
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:256]


def _asl_encode_segment(name: str) -> str:
    """Escape the characters an ASL path uses as separators."""
    return (
        name.replace("%", "%25").replace("/", "%2F")
        .replace("[", "%5B").replace("]", "%5D")
    )


def _asl_placeholder_keys(text: str) -> list[str]:
    """Return the ``${key}`` names in *text*, sorted and without repeats."""
    return sorted(set(_ASL_PLACEHOLDER_RE.findall(text)))


def _asl_name_from_value(
    value: str, target_kind: str,
) -> tuple[Optional[str], list[str]]:
    """Reduce one literal Parameters value to a resource name.

    Returns ``(name, placeholder_keys)``. *name* is ``None`` when the name part is
    not a plain literal; *placeholder_keys* then holds the ``${key}`` names found
    in the name part only, never the partition, region or account.
    """
    text = value.strip()
    if text.startswith("arn:"):
        parts = text.split(":", 5)
        if len(parts) < 6:
            return None, []
        resource = parts[5]
        prefix = _ASL_ARN_RESOURCE_PREFIX.get(target_kind, "")
        if prefix:
            if not resource.startswith(prefix):
                return None, []
            resource = resource[len(prefix):]
        elif "/" in resource:
            resource = resource.rsplit("/", 1)[-1]
    elif text.startswith(("https://", "http://")):
        resource = text.rstrip("/").rsplit("/", 1)[-1]
    else:
        resource = text
    if target_kind in ("table",) and "/" in resource:
        resource = resource.split("/", 1)[0]
    # A version, alias or revision is a qualifier on the name, not part of it.
    if target_kind in ("function", "task-definition", "job-definition"):
        resource = resource.split(":", 1)[0]
    elif target_kind == "state-machine":
        resource = resource.split(":", 1)[0]
    if "${" in resource:
        return None, _asl_placeholder_keys(resource)
    if _ASL_NAME_RE.fullmatch(resource) is None:
        return None, []
    return resource, []


def _asl_string(node: Any) -> Optional[str]:
    """Decode one JSON string node, or ``None`` when it is oversized or malformed."""
    raw = node.text
    if raw is None or len(raw) > _MAX_ASL_STRING_BYTES:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return decoded if isinstance(decoded, str) else None

def _asl_read_value(node: Any, depth: int, budget: list[int], problems: set[str],
) -> Any:
    """Turn a JSON node into plain Python, bounded in depth and size.

    Numbers become ``0`` because no number is ever read; strings are kept
    only up to ``_MAX_ASL_STRING_BYTES``.
    """
    budget[0] -= 1
    if budget[0] < 0:
        problems.add("truncated")
        return None
    if depth > _MAX_ASL_JSON_DEPTH:
        problems.add("truncated")
        return None
    kind = node.type
    if kind == "string":
        return _asl_string(node)
    if kind == "true":
        return True
    if kind == "false":
        return False
    if kind == "number":
        return _AslNumber.read(node)
    if kind == "array":
        items: list[Any] = []
        for child in node.named_children:
            if child.type == "comment":
                continue
            if len(items) >= _MAX_ASL_LIST_ITEMS:
                problems.add("truncated")
                break
            items.append(_asl_read_value(child, depth + 1, budget, problems))
        return items
    if kind == "object":
        obj = _AslObj(node.start_point[0] + 1, node.end_point[0] + 1)
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            key_node = pair.child_by_field_name("key")
            value_node = pair.child_by_field_name("value")
            if key_node is None or value_node is None or key_node.type != "string":
                continue
            key = _asl_string(key_node)
            if key is None:
                continue
            if key in obj:
                problems.add("duplicate_keys")
                continue
            if len(obj) >= _MAX_ASL_OBJECT_KEYS:
                problems.add("truncated")
                break
            obj[key] = _asl_read_value(value_node, depth + 1, budget, problems)
        return obj
    return None

def _asl_task_target(state: dict, resource: Any,
) -> tuple[dict[str, Any], Optional[tuple[str, str, str]]]:
    """Describe what a Task calls, without keeping any value.

    Returns ``(extra, edge)``: *extra* holds ``resource_kind`` and, for a target
    that cannot be named, ``target_dynamic`` and the ``${key}`` names; *edge* is
    ``(target, resource_kind, target_kind)`` when a literal name was found.
    """
    extra: dict[str, Any] = {}
    if not isinstance(resource, str):
        return extra, None
    service = ""
    action = ""
    fields: tuple[tuple[str, str], ...] = ()
    arn_parts = resource.split(":", 5)
    if len(arn_parts) == 6 and arn_parts[0] == "arn" and arn_parts[2] == "lambda":
        # A Lambda function ARN used directly as the Resource.
        name, keys = _asl_name_from_value(resource, "function")
        extra["resource_kind"] = "lambda"
        if name is not None:
            return extra, (f"external:aws-lambda:{name}", "lambda", "function")
        extra["target_dynamic"] = True
        if keys:
            extra["target_placeholders"] = keys
        return extra, None
    optimized = _ASL_OPTIMIZED_RESOURCE_RE.fullmatch(resource)
    if optimized is not None:
        integration = optimized.group(1)
        if integration.startswith("aws-sdk:"):
            sdk_service = integration.split(":")[1] if integration.count(":") >= 2 else ""
            if _ASL_SERVICE_RE.fullmatch(sdk_service):
                extra["resource_kind"] = f"aws-sdk:{sdk_service}"
            return extra, None
        pieces = integration.split(":", 1)
        service = pieces[0]
        action = pieces[1] if len(pieces) > 1 else ""
        if _ASL_SERVICE_RE.fullmatch(service) is None:
            return extra, None
        extra["resource_kind"] = service
        if _ASL_ACTION_RE.fullmatch(action):
            extra["resource_action"] = action
        base_action = action.split(".", 1)[0]
        fields = _ASL_TARGET_FIELDS.get((service, base_action), ())
    elif "${" in resource:
        keys = _asl_placeholder_keys(resource)
        extra["resource_kind"] = "dynamic"
        extra["target_dynamic"] = True
        if keys:
            extra["target_placeholders"] = keys
        return extra, None
    else:
        return extra, None
    if not fields:
        return extra, None
    params = state.get("Parameters")
    if not isinstance(params, dict):
        params = state.get("Arguments")
    if not isinstance(params, dict):
        return extra, None
    dynamic_keys: set[str] = set()
    saw_dynamic = False
    for field_name, target_kind in fields:
        if field_name in params and isinstance(params[field_name], str):
            name, keys = _asl_name_from_value(params[field_name], target_kind)
            if name is not None:
                prefix = _ASL_EXTERNAL_PREFIX.get(service)
                if prefix is not None:
                    if len(fields) > 1:
                        extra["target_kind"] = target_kind
                    return extra, (f"external:{prefix}:{name}", service, target_kind)
            saw_dynamic = True
            dynamic_keys.update(keys)
        elif field_name in params or f"{field_name}.$" in params:
            # A non-string value or a path (``Field.$``) names nothing statically.
            saw_dynamic = True
    if saw_dynamic:
        extra["target_dynamic"] = True
        if dynamic_keys:
            extra["target_placeholders"] = sorted(dynamic_keys)
    return extra, None

def parse_asl(
    path: Path,
    source: bytes,
    json_parser: Any,
    node_cls: Any,
    edge_cls: Any,
    is_test: bool,
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Index an Amazon States Language (Step Functions) state machine.

    A file node, one ``Class`` node for the machine and one ``Function`` node per
    state, nested Parallel branches and Map processors included. Flow becomes
    ``TRANSITIONS_TO`` edges (``extra.via`` is ``next``, ``default``, ``choice``,
    ``catch``, ``branch`` or ``map``; a Catch edge adds a bounded ``error_equals``, the
    Default edge ``default``; a state with Retry or Catch gets ``has_retry`` or
    ``has_catch``); a Task that calls a nameable resource gets a
    ``REFERENCES`` edge to ``external:aws-<service>:<name>``. Values never enter the
    graph. An unreadable or oversized definition keeps only its file node.
    """
    file_path = normalize_file_path(path)
    file_extra: dict[str, Any] = {
        "config_format": "asl",
        "values_indexed": False,
        "asl_problem": None,
    }
    nodes: list[NodeInfo] = [node_cls(
        kind="File",
        name=file_path,
        file_path=file_path,
        line_start=1,
        line_end=source.count(b"\n") + 1,
        language="asl",
        is_test=is_test,
        extra=file_extra,
    )]
    edges: list[EdgeInfo] = []
    if len(source) > _MAX_ASL_BYTES:
        file_extra["asl_problem"] = "too_large"
        return nodes, edges
    if json_parser is None:
        file_extra["asl_problem"] = "no_json_grammar"
        return nodes, edges
    tree = json_parser.parse(_asl_quote_placeholders(source))
    root = tree.root_node
    if root.has_error:
        file_extra["asl_problem"] = "invalid_json"
        return nodes, edges
    values = [c for c in root.named_children if c.type != "comment"]
    problems: set[str] = set()
    budget = [_MAX_ASL_JSON_NODES]
    document = (
        _asl_read_value(values[0], 0, budget, problems) if len(values) == 1 else None
    )
    if not isinstance(document, _AslObj) or not isinstance(document.get("States"), dict):
        file_extra["asl_problem"] = "not_a_state_machine"
        return nodes, edges

    name = path.name
    for suffix in (".asl.json", ".asl"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    machine = _sanitize_asl_name(name) or "state-machine"
    machine_extra: dict[str, Any] = {
        "asl_format": True,
        "asl_role": "state_machine",
        "display_name": f"statemachine {machine}",
        "state_count": 0,
        "unresolved_transitions": 0,
    }
    query_language = document.get("QueryLanguage")
    if query_language in ("JSONPath", "JSONata"):
        machine_extra["query_language"] = query_language
    machine_qualified = f"{file_path}::{machine}"
    nodes.append(node_cls(
        kind="Class",
        name=machine,
        file_path=file_path,
        line_start=document.line_start,
        line_end=document.line_end,
        language="asl",
        extra=machine_extra,
    ))
    edges.append(edge_cls(
        kind="CONTAINS", source=file_path, target=machine_qualified,
        file_path=file_path, line=document.line_start,
    ))

    seen_identities: set[str] = set()

    def read_machine(
        definition: _AslObj, prefix: str, parent_qualified: str, depth: int,
    ) -> None:
        states = definition.get("States")
        if not isinstance(states, _AslObj):
            return
        start_at = definition.get("StartAt")
        qualified_by_name: dict[str, str] = {}
        ordered: list[tuple[str, _AslObj, str]] = []
        for raw_name, state in states.items():
            if not isinstance(state, _AslObj):
                continue
            state_name = _sanitize_asl_name(raw_name)
            identity = f"{machine}/{prefix}{_asl_encode_segment(state_name)}"
            if identity in seen_identities:
                problems.add("collision")
                continue
            if len(seen_identities) >= _MAX_ASL_STATES:
                problems.add("truncated")
                break
            seen_identities.add(identity)
            qualified_by_name[state_name] = f"{file_path}::{identity}"
            ordered.append((state_name, state, qualified_by_name[state_name]))
        for state_name, state, qualified in ordered:
            state_type = state.get("Type")
            if state_type not in _ASL_STATE_TYPES:
                state_type = "Unknown"
            extra: dict[str, Any] = {
                "state_type": state_type,
                "is_start": isinstance(start_at, str)
                and _sanitize_asl_name(start_at) == state_name,
                "end": state.get("End") is True or state_type in ("Succeed", "Fail"),
            }
            target_edge = None
            if state_type == "Task":
                task_extra, target_edge = _asl_task_target(
                    state, state.get("Resource"),
                )
                extra.update(task_extra)
            for field, flag in (("Retry", "has_retry"), ("Catch", "has_catch")):
                block = state.get(field)
                if isinstance(block, list) and block:
                    extra[flag] = True
            state_ql = state.get("QueryLanguage")
            if state_ql in ("JSONPath", "JSONata"):
                extra["query_language"] = state_ql
            identity = qualified[len(file_path) + 2:]
            state_path = identity[len(machine) + 1:]
            extra["asl_role"] = "state"
            extra["state_machine"] = machine
            extra["state_path"] = state_path
            extra["is_machine_start"] = bool(extra["is_start"]) and prefix == ""
            extra["display_name"] = f"{machine}/{state_path}"
            extra["docstring"] = (
                f"Step Functions {state_type} state {machine}/{state_path} "
                f"in state machine {machine}"
            )
            nodes.append(node_cls(
                kind="Function",
                name=state_name,
                file_path=file_path,
                line_start=state.line_start,
                line_end=state.line_end,
                language="asl",
                extra=extra,
                identity_name=identity,
            ))
            machine_extra["state_count"] += 1
            edges.append(edge_cls(
                kind="CONTAINS", source=parent_qualified, target=qualified,
                file_path=file_path, line=state.line_start,
            ))
            if target_edge is not None:
                target, resource_kind, target_kind = target_edge
                edge_extra: dict[str, Any] = {"resource_kind": resource_kind}
                if "target_kind" in extra:
                    edge_extra["target_kind"] = extra["target_kind"]
                edges.append(edge_cls(
                    kind="REFERENCES", source=qualified, target=target,
                    file_path=file_path, line=state.line_start, extra=edge_extra,
                ))

            def transition(
                next_name: Any, via: str, line: int, index: Optional[int] = None,
                more: Optional[dict[str, Any]] = None,
            ) -> None:
                if not isinstance(next_name, str):
                    return
                target_qualified = qualified_by_name.get(_sanitize_asl_name(next_name))
                if target_qualified is None:
                    machine_extra["unresolved_transitions"] += 1
                    return
                edge_extra: dict[str, Any] = {"via": via}
                if index is not None:
                    edge_extra["index"] = index
                if more:
                    edge_extra.update(more)
                edges.append(edge_cls(
                    kind="TRANSITIONS_TO", source=qualified, target=target_qualified,
                    file_path=file_path, line=line, extra=edge_extra,
                ))

            transition(state.get("Next"), "next", state.line_start)
            if state_type == "Choice":
                transition(
                    state.get("Default"), "default", state.line_start,
                    more={"default": True},
                )
                choices = state.get("Choices")
                for index, rule in enumerate(choices if isinstance(choices, list) else []):
                    if isinstance(rule, _AslObj):
                        # The rule's test is never stored (see read_choice_conditions).
                        transition(rule.get("Next"), "choice", rule.line_start, index)
            catches = state.get("Catch")
            for index, rule in enumerate(catches if isinstance(catches, list) else []):
                if isinstance(rule, _AslObj):
                    transition(
                        rule.get("Next"), "catch", rule.line_start, index,
                        more={"error_equals": _asl_error_equals(rule)},
                    )

            if depth + 1 >= _MAX_ASL_NESTING:
                if state_type in ("Parallel", "Map"):
                    problems.add("truncated")
                continue
            inner_prefix = f"{prefix}{_asl_encode_segment(state_name)}"
            if state_type == "Parallel":
                branches = state.get("Branches")
                for index, branch in enumerate(branches if isinstance(branches, list) else []):
                    if isinstance(branch, _AslObj):
                        read_branch(
                            branch, f"{inner_prefix}[{index}]/", qualified,
                            "branch", index, depth + 1,
                        )
            elif state_type == "Map":
                processor = state.get("ItemProcessor")
                if not isinstance(processor, _AslObj):
                    processor = state.get("Iterator")
                if isinstance(processor, _AslObj):
                    read_branch(
                        processor, f"{inner_prefix}[map]/", qualified,
                        "map", None, depth + 1,
                    )

    def read_branch(
        definition: _AslObj, prefix: str, owner: str, via: str,
        index: Optional[int], depth: int,
    ) -> None:
        read_machine(definition, prefix, owner, depth)
        start_at = definition.get("StartAt")
        if not isinstance(start_at, str):
            return
        start_name = _asl_encode_segment(_sanitize_asl_name(start_at))
        start_identity = f"{machine}/{prefix}{start_name}"
        if start_identity not in seen_identities:
            return
        edge_extra: dict[str, Any] = {"via": via}
        if index is not None:
            edge_extra["index"] = index
        edges.append(edge_cls(
            kind="TRANSITIONS_TO", source=owner, target=f"{file_path}::{start_identity}",
            file_path=file_path, line=definition.line_start, extra=edge_extra,
        ))

    read_machine(document, "", machine_qualified, 0)
    machine_extra["docstring"] = (
        f"Step Functions state machine {machine} with {machine_extra['state_count']} states"
    )
    if problems & {"truncated", "collision"}:
        file_extra["asl_problem"] = "truncated"
    if "duplicate_keys" in problems:
        machine_extra["duplicate_keys"] = True
    return nodes, edges
