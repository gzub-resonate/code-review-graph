"""Publish / listen edges from application code to SQS, SNS, RabbitMQ and JMS destinations.

Terraform and Step Functions already point at ``external:aws-sqs:<name>`` and
``external:aws-sns:<name>`` (see ``infra_references``). This module gives Java and Python
code the same targets, so a service and the infrastructure that declares its queue meet on
one node:

* a consumer (``@SqsListener``, ``@RabbitListener``/``@RabbitHandler``, ``@JmsListener``,
  boto3 ``receive_message``, pika ``basic_consume``) gets a ``CONSUMES`` edge,
* a consumer also gets a ``HANDLES`` edge to the same target (``extra.trigger = "queue"``;
  ``extra.via`` says whether a listener annotation or an SDK receive call makes it the
  trigger), so ``listeners_of`` and execution flows start from it,
* a producer (``sendMessage``, ``publish``, ``convertAndSend``, ``basic_publish``, ...) gets
  a ``PRODUCES`` edge,
* an SQS ``deleteMessage``/``deleteMessageBatch`` is the consuming side and gets a ``CONSUMES``
  edge with ``access: "delete"``,

the same kinds the Kafka edges use. Targets are ``external:aws-sqs:<queue>``,
``external:aws-sns:<topic>``, ``external:rabbitmq:<exchange or queue>`` and
``external:jms:<destination>``. ``extra`` carries ``service``, ``direction``
(``listen``/``publish``), the name under ``queue``/``topic``/``exchange`` (plus
``routing_key``) and the ``resolution`` rule. An SQS or SNS edge also carries ``operation``
(snake case), ``sdk_operation`` (``SendMessage``, ``ReceiveMessage``, ``DeleteMessage``,
``Publish`` ...) and ``access`` (``write``, ``read``, ``delete``).

A name is taken only from what the source states: a string literal, an ARN or URL (its last
segment), a constant of the same file, or a ``@Value("${key}")`` field. A property value is
never read, so a ``${key}`` placeholder, a variable, an environment lookup or a computed string
cannot be resolved: the edge is still written, to ``<prefix>:?unresolved`` (no real resource has
that name, so it never joins), with ``unresolved: true`` and what is known (``config_key``,
``env_var``, ``expr``, and ``config_getter`` for a ``props.getQueueName()`` call on a typed
bean). ``aws_config_names`` later fills in ``resolved_names`` from the configuration files.

Only the typed receivers and method names listed below produce an edge. Nothing is guessed from
a method name alone, except pika's ``basic_publish``/``basic_consume``, which are specific
enough.
"""

from __future__ import annotations

import os
import re
import stat as stat_module
import threading
from typing import Any, Optional

from .aws_sdk_calls import classify_access, sdk_operation_name
from .config_keys import normalize_spring_config_key

UNRESOLVED_NAME = "?unresolved"
_UNKNOWN = "${?}"
_MAX_DEPTH = 8
_MAX_EXPR = 80

# -- Java ---------------------------------------------------------------------------------------

#: Receiver type -> service for Java client calls.
_JAVA_CLIENT_TYPES: dict[str, str] = {
    **{t: "sqs" for t in (
        "AmazonSQS", "AmazonSQSAsync", "AmazonSQSClient", "AmazonSQSAsyncClient", "SqsClient",
        "SqsAsyncClient", "SqsTemplate", "SqsOperations", "SqsAsyncOperations",
        "QueueMessagingTemplate",
    )},
    **{t: "sns" for t in (
        "AmazonSNS", "AmazonSNSAsync", "AmazonSNSClient", "AmazonSNSAsyncClient", "SnsClient",
        "SnsAsyncClient", "SnsTemplate", "NotificationMessagingTemplate",
    )},
    **{t: "rabbitmq" for t in (
        "RabbitTemplate", "AmqpTemplate", "RabbitOperations", "AsyncRabbitTemplate",
        "RabbitMessagingTemplate",
    )},
    **{t: "jms" for t in ("JmsTemplate", "JmsOperations", "JmsMessagingTemplate")},
}
_SEND_METHODS = frozenset({
    "sendMessage", "sendMessageBatch", "sendMessageAsync", "send", "sendAsync", "convertAndSend",
    "publish", "publishBatch", "sendNotification", "convertSendAndReceive", "sendAndReceive",
})
_RECEIVE_METHODS = frozenset({
    "receiveMessage", "receive", "receiveAsync", "receiveAndConvert", "receiveSelected",
    "receiveSelectedAndConvert",
})
#: SQS calls that remove a message: the consuming side, recorded as a ``delete`` access.
_DELETE_METHODS = frozenset({"deleteMessage", "deleteMessageBatch"})
#: Java method -> snake case SDK operation, where it differs from the method name.
_JAVA_OPERATION = {
    "sendMessageAsync": "send_message", "send": "send_message", "sendAsync": "send_message",
    "receive": "receive_message", "receiveAsync": "receive_message",
    "sendNotification": "publish", "convertAndSend": "send_message",
}
#: Which of the above each service really has; the rest are ignored for that service.
_SERVICE_METHODS: dict[str, frozenset[str]] = {
    "sqs": frozenset({
        "sendMessage", "sendMessageBatch", "sendMessageAsync", "send", "sendAsync",
        "convertAndSend", "receiveMessage", "receive", "receiveAsync", "deleteMessage",
        "deleteMessageBatch",
    }),
    "sns": frozenset({"publish", "publishBatch", "sendNotification", "convertAndSend"}),
    "rabbitmq": frozenset({
        "send", "convertAndSend", "convertSendAndReceive", "sendAndReceive", "receive",
        "receiveAndConvert",
    }),
    "jms": frozenset({
        "send", "convertAndSend", "sendAndReceive", "receive", "receiveAndConvert",
        "receiveSelected", "receiveSelectedAndConvert",
    }),
}
#: Builder calls that carry the destination inside a request (``.queueUrl("..")``).
_BUILDER_KEYS: dict[str, frozenset[str]] = {
    "sqs": frozenset({"queueUrl", "queueName", "queue"}),
    "sns": frozenset({"topicArn", "targetArn", "topic"}),
}
_JAVA_SKIP_DESCENT = frozenset({
    "method_declaration", "constructor_declaration", "class_declaration", "interface_declaration",
    "enum_declaration", "record_declaration",
})
_JAVA_CLASS_NODES = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
})

_PLACEHOLDER = re.compile(r"\$\{\s*([^}:\s]+)\s*(?::[^}]*)?\}")


class Dest:
    """One destination name as far as the source states it."""

    __slots__ = ("name", "config_key", "env_var", "expr", "getter")

    def __init__(
        self,
        name: Optional[str],
        config_key: Optional[str] = None,
        env_var: Optional[str] = None,
        expr: Optional[str] = None,
        getter: Optional[tuple[str, str]] = None,
    ) -> None:
        self.getter = getter
        self.name = name
        self.config_key = config_key
        self.env_var = env_var
        self.expr = expr

    @property
    def unresolved(self) -> bool:
        return self.name is None


def _snake_case(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def name_from_reference(raw: str) -> Dest:
    """The resource name in a literal: the last segment of an ARN or a queue URL."""
    value = raw.strip()
    if value.startswith("arn:"):
        segment = value.rsplit(":", 1)[-1]
    elif "/" in value:
        segment = value.rstrip("/").rsplit("/", 1)[-1]
    else:
        segment = value
    if not segment:
        return Dest(None, expr=value[:_MAX_EXPR])
    if "${" in segment or "#{" in segment or "{" in segment:
        match = _PLACEHOLDER.search(segment.replace(_UNKNOWN, ""))
        key = normalize_spring_config_key(match.group(1)) if match else None
        return Dest(None, config_key=key, expr=value[:_MAX_EXPR])
    return Dest(segment)


def _plain_name(raw: str) -> Dest:
    """A bare destination (Rabbit exchange, JMS destination): no URL or ARN splitting."""
    value = raw.strip()
    if not value:
        return Dest("")  # the AMQP default exchange; _edge never writes it as a target
    if "${" in value or "#{" in value:
        match = _PLACEHOLDER.search(value.replace(_UNKNOWN, ""))
        key = normalize_spring_config_key(match.group(1)) if match else None
        return Dest(None, config_key=key, expr=value[:_MAX_EXPR])
    return Dest(value)


# -- Java value resolution ----------------------------------------------------------------------


def _java_string(node, consts: dict[str, set[str]], depth: int = 0) -> Optional[str]:
    """The raw string a Java expression states, ``_UNKNOWN`` for parts it does not."""
    if depth > _MAX_DEPTH or node is None:
        return None
    kind = node.type
    if kind == "string_literal":
        value = _text(node)
        if value.startswith('"""') or len(value) < 2:
            return None
        return value[1:-1]
    if kind == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        return _java_string(inner[0], consts, depth + 1) if inner else None
    if kind == "identifier":
        values = consts.get(_text(node))
        if values and len(values) == 1:
            return next(iter(values))
        return None
    if kind == "field_access":
        # ``Names.EXCHANGE``: only a view backed by the repository constant index answers a
        # dotted name; a plain dict never holds one.
        values = consts.get("".join(_text(node).split()))
        if values and len(values) == 1:
            return next(iter(values))
        return None
    if kind == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is None or _text(op) != "+":
            return None
        left = _java_string(node.child_by_field_name("left"), consts, depth + 1)
        right = _java_string(node.child_by_field_name("right"), consts, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else _UNKNOWN) + (
            right if right is not None else _UNKNOWN
        )
    return None


_BEAN_GETTER = re.compile(r"(?:get|is)([A-Z]\w*)")


def _java_getter(node, var_types: Optional[dict[str, str]]) -> Optional[tuple[str, str]]:
    """``(declared type, property)`` of a bean getter call such as ``props.getQueueName()``."""
    if not var_types or node.type != "method_invocation" or _args(node):
        return None
    name = node.child_by_field_name("name")
    owner = node.child_by_field_name("object")
    if name is None or owner is None:
        return None
    match = _BEAN_GETTER.fullmatch(_text(name))
    if match is None:
        return None
    if owner.type == "field_access":
        target = owner.child_by_field_name("object")
        field = owner.child_by_field_name("field")
        owner = field if target is not None and target.type == "this" else None
    if owner is None or owner.type != "identifier":
        return None
    declared = var_types.get(_text(owner))
    if not declared:
        return None
    prop = match.group(1)
    return declared, prop[0].lower() + prop[1:]


def _java_dest(
    node, consts: dict[str, set[str]], splitter, var_types: Optional[dict[str, str]] = None,
) -> Dest:
    raw = _java_string(node, consts)
    if raw is None:
        expr = " ".join(_text(node).split())[:_MAX_EXPR]
        key = None
        getter = _java_getter(node, var_types)
        if getter:
            return Dest(None, expr=expr, getter=getter)
        # ``@Value("${key}") String queue`` is stored as a placeholder constant, so an
        # unresolved identifier here never has one; a literal with one is handled below.
        return Dest(None, config_key=key, expr=expr)
    return splitter(raw)


def _java_collect_consts(scope, view: Optional[dict] = None) -> dict[str, set[str]]:
    """Names bound once to a string: constants, locals and ``@Value("${key}")`` fields."""
    found: dict[str, set[str]] = view if view is not None else {}

    def add(name: str, value: str) -> None:
        found.setdefault(name, set()).add(value)

    def visit(node) -> None:
        if node.type in ("field_declaration", "local_variable_declaration"):
            value_annotation = None
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            if modifiers is not None:
                for ann in modifiers.children:
                    is_ann = ann.type in ("annotation", "marker_annotation")
                    if is_ann and _ann_name(ann) == "Value":
                        value_annotation = ann
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(name_node)
                init = declarator.child_by_field_name("value")
                if value_annotation is not None:
                    values = _ann_value_nodes(value_annotation, ("value",))
                    raw = _java_string(values[0], {}) if values else None
                    add(name, raw if raw is not None else _UNKNOWN)
                elif init is not None:
                    raw = _java_string(init, found)
                    add(name, raw if raw is not None else _UNKNOWN)
        for child in node.children:
            visit(child)

    visit(scope)
    return found


# -- Java: repository-wide string constants ------------------------------------------------------

_CONST_SKIP_DIRS = frozenset({
    "node_modules", "build", "target", "out", "dist", "venv", "__pycache__",
})
_MAX_CONST_FILES = 100_000
_MAX_CONST_FILE_BYTES = 1_000_000
#: Nested type declarations followed when collecting constants. A deeper constant is left
#: unresolved (never guessed); the bound keeps a crafted file from exhausting the stack.
_MAX_CONST_NESTING = 16
_STRING_WORD = re.compile(rb"\bString\b")
_MODIFIER_WORD = re.compile(r"\w+")
_STATIC_IMPORT = re.compile(r"import\s+static\s+([\w.]+)\.(\*|\w+)\s*;")
_PLAIN_IMPORT = re.compile(r"import\s+([\w.]+)\s*;")
_PACKAGE_DECL = re.compile(r"package\s+([\w.]+)\s*;")
_JAVA_TYPE_NODES = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
})
_JAVA_BODY_NODES = frozenset({
    "class_body", "interface_body", "enum_body", "enum_body_declarations",
})

#: ``{(path, mtime_ns, size): [(keys, value or None)]}``, process-wide so every parser instance
#: of a build reads a constants file at most once per version.
_CONST_FILE_CACHE: dict[tuple[str, int, int], list[tuple[tuple[str, ...], Optional[str]]]] = {}
_CONST_FILE_CACHE_LOCK = threading.Lock()


class JavaConstIndex:
    """``static final String`` constants of a repository, by qualified and simple class name.

    A key (``Names.EXCHANGE``, ``Outer.Inner.NAME`` or ``pkg.Names.EXCHANGE``) that is bound to
    more than one distinct value, or to a value that cannot be read, answers nothing: it is
    never guessed.
    """

    def __init__(self) -> None:
        self._values: dict[str, set[str]] = {}
        self._poisoned: set[str] = set()

    def add(self, keys, value: Optional[str]) -> None:
        for key in keys:
            if value is None:
                self._poisoned.add(key)
            else:
                self._values.setdefault(key, set()).add(value)

    def lookup(self, key: str) -> Optional[set[str]]:
        if key in self._poisoned:
            return None
        return self._values.get(key)

    def __bool__(self) -> bool:
        return bool(self._values or self._poisoned)


def _java_file_constants(root) -> list[tuple[tuple[str, ...], Optional[str]]]:
    """``(keys, value)`` of each ``static final String`` field (or interface field) of a file."""
    package = ""
    for child in root.children:
        if child.type == "package_declaration":
            match = _PACKAGE_DECL.search(_text(child))
            package = match.group(1) if match else ""
    found: list[tuple[tuple[str, ...], Optional[str]]] = []

    def field(node, chain: list[str], in_interface: bool, local: dict[str, set[str]]) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is None or _text(type_node) not in ("String", "java.lang.String"):
            return
        modifiers = next((c for c in node.children if c.type == "modifiers"), None)
        mod_text = _text(modifiers) if modifiers is not None else ""
        if not in_interface:
            # Word test, not a ``.*`` pattern: a crafted modifier list must not go quadratic.
            words = set(_MODIFIER_WORD.findall(mod_text))
            if "static" not in words or "final" not in words:
                return
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            init = declarator.child_by_field_name("value")
            if name_node is None or init is None:
                continue
            name = _text(name_node)
            raw = _java_string(init, local)
            if raw is not None and _UNKNOWN in raw:
                raw = None
            if raw is not None:
                local[name] = {raw}
            dotted = ".".join(chain)
            keys = [f"{chain[-1]}.{name}", f"{dotted}.{name}"]
            if package:
                keys.append(f"{package}.{dotted}.{name}")
            found.append((tuple(dict.fromkeys(keys)), raw))

    def walk(node, chain: list[str], in_interface: bool, local: dict[str, set[str]]) -> None:
        if len(chain) > _MAX_CONST_NESTING:
            return
        for child in node.children:
            if child.type in _JAVA_TYPE_NODES:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    walk(
                        child, chain + [_text(name_node)],
                        child.type == "interface_declaration", {},
                    )
            elif child.type == "field_declaration" and chain:
                field(child, chain, in_interface, local)
            elif child.type in _JAVA_BODY_NODES:
                walk(child, chain, in_interface, local)

    walk(root, [], False, {})
    return found


def _read_const_file(path: str, parser) -> Optional[list[tuple[tuple[str, ...], Optional[str]]]]:
    """The constants of one Java file, or ``None`` when it is not a plain, small, regular file.

    The file is opened without following a symlink and without blocking (a FIFO would hang an
    ordinary open), and its type and size are read from the open handle, so a path swapped
    after the check cannot lead outside the repository. The read itself is bounded too: a
    device that reports size 0 cannot be read without end.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(info.st_mode) or info.st_size > _MAX_CONST_FILE_BYTES:
            return None
        cache_key = (path, info.st_mtime_ns, info.st_size)
        with _CONST_FILE_CACHE_LOCK:
            cached = _CONST_FILE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        data = handle.read(_MAX_CONST_FILE_BYTES + 1)
    if len(data) > _MAX_CONST_FILE_BYTES:
        return None
    cached = []
    if _STRING_WORD.search(data) and (b"static" in data or b"interface" in data):
        cached = _java_file_constants(parser.parse(data).root_node)
    with _CONST_FILE_CACHE_LOCK:
        _CONST_FILE_CACHE[cache_key] = cached
    return cached


def build_java_const_index(repo_root, get_parser) -> JavaConstIndex:
    """Index the string constants of every Java file under *repo_root* (bounded)."""
    index = JavaConstIndex()
    parser = get_parser("java") if repo_root is not None else None
    if parser is None:
        return index
    seen = 0
    for directory, dirs, files in os.walk(str(repo_root)):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _CONST_SKIP_DIRS]
        for name in files:
            if not name.endswith(".java"):
                continue
            seen += 1
            if seen > _MAX_CONST_FILES:
                return index
            path = os.path.join(directory, name)
            try:
                cached = _read_const_file(path, parser)
            except Exception:  # noqa: BLE001 - best effort: one bad file never fails the build
                continue
            if cached is None:
                continue
            for keys, value in cached:
                index.add(keys, value)
    return index


class _ConstView(dict):
    """File-local constants that also answer dotted and statically imported names.

    ``get`` looks at the file's own bindings first, then at the repository index: a dotted
    ``Names.EXCHANGE`` through the file's imports and package, a bare name through its static
    imports. Several distinct values, or none, answer ``None``.
    """

    def __init__(self, index: JavaConstIndex, root) -> None:
        super().__init__()
        self._index = index
        self._package = ""
        self._imports: dict[str, str] = {}
        self._static: list[tuple[str, str]] = []
        for child in root.children:
            text = _text(child)
            if child.type == "package_declaration":
                match = _PACKAGE_DECL.search(text)
                self._package = match.group(1) if match else ""
            elif child.type == "import_declaration":
                static = _STATIC_IMPORT.search(text)
                if static:
                    self._static.append((static.group(1), static.group(2)))
                    continue
                plain = _PLAIN_IMPORT.search(text)
                if plain:
                    self._imports[plain.group(1).rsplit(".", 1)[-1]] = plain.group(1)

    def get(self, key, default=None):  # type: ignore[override]
        local = dict.get(self, key)
        if local is not None:
            return local
        found = self._dotted(key) if "." in key else self._static_name(key)
        return found if found is not None else default

    def _dotted(self, key: str) -> Optional[set[str]]:
        head = key.split(".", 1)[0]
        qualified = self._imports.get(head)
        if qualified is not None:
            # ``import pkg.Names;`` says which class ``Names`` is; the repository may not have it.
            return self._index.lookup(f"{qualified.rsplit('.', 1)[0]}.{key}")
        if self._package:
            local = self._index.lookup(f"{self._package}.{key}")
            if local is not None:
                return local
        parts = key.split(".")
        for width in (len(parts), 3, 2):
            if width <= len(parts):
                found = self._index.lookup(".".join(parts[-width:]))
                if found is not None:
                    return found
        return None

    def _static_name(self, name: str) -> Optional[set[str]]:
        values: set[str] = set()
        for owner, member in self._static:
            if member not in ("*", name):
                continue
            found = self._index.lookup(f"{owner}.{name}")
            if found is not None:
                values |= found
        return values or None


def _ann_name(annotation) -> Optional[str]:
    for child in annotation.children:
        if child.type == "identifier":
            return _text(child)
        if child.type in ("scoped_identifier", "qualified_name"):
            return _text(child).rsplit(".", 1)[-1]
    return None


def _ann_value_nodes(annotation, keys: tuple[str, ...]) -> list:
    """Value expression nodes of the named (or sole positional) arguments of an annotation."""
    out: list = []
    for arglist in annotation.children:
        if arglist.type != "annotation_argument_list":
            continue
        for arg in arglist.children:
            if arg.type == "element_value_pair":
                named = [c for c in arg.children if c.is_named]
                if len(named) >= 2 and _text(named[0]) in keys:
                    out.extend(_flatten_array(named[-1]))
            elif arg.is_named and "value" in keys:
                out.extend(_flatten_array(arg))
    return out


def _flatten_array(node) -> list:
    if node.type in ("element_value_array_initializer", "array_initializer"):
        items: list = []
        for child in node.named_children:
            items.extend(_flatten_array(child))
        return items
    return [node]


def _nested_annotations(node, name: str) -> list:
    found: list = []
    if node.type in ("annotation", "marker_annotation") and _ann_name(node) == name:
        found.append(node)
    for child in node.children:
        found.extend(_nested_annotations(child, name))
    return found


# -- Java: types in scope -----------------------------------------------------------------------


def _java_var_types(method_node) -> dict[str, str]:
    """Variable name -> declared outer type, for the method first and then its class."""
    scopes = [method_node]
    parent = method_node.parent
    while parent is not None:
        if parent.type in _JAVA_CLASS_NODES:
            scopes.append(parent)
            break
        parent = parent.parent
    types: dict[str, str] = {}

    def outer_type(type_node) -> Optional[str]:
        if type_node is None:
            return None
        if type_node.type in ("type_identifier", "scoped_type_identifier"):
            return _text(type_node).rsplit(".", 1)[-1]
        if type_node.type == "generic_type":
            for child in type_node.children:
                if child.type in ("type_identifier", "scoped_type_identifier"):
                    return _text(child).rsplit(".", 1)[-1]
        return None

    def visit(node) -> None:
        if node.type in ("field_declaration", "local_variable_declaration"):
            declared = outer_type(node.child_by_field_name("type"))
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                kind = declared
                if kind is None:  # ``var x = SqsClient.create()``
                    init = declarator.child_by_field_name("value")
                    head = _first_identifier(init)
                    kind = head if head in _JAVA_CLIENT_TYPES else None
                if kind:
                    types.setdefault(_text(name_node), kind)
        elif node.type in ("formal_parameter", "spread_parameter"):
            kind = outer_type(node.child_by_field_name("type"))
            name_node = node.child_by_field_name("name")
            if kind and name_node is not None:
                types.setdefault(_text(name_node), kind)
        for child in node.children:
            visit(child)

    for scope in scopes:
        visit(scope)
    return types


def _first_identifier(node) -> Optional[str]:
    while node is not None:
        if node.type == "identifier":
            return _text(node)
        if node.type in ("method_invocation", "field_access"):
            target = node.child_by_field_name("object")
            if target is None:
                return None
            node = target
            continue
        return None
    return None


def _java_receiver(invocation, var_types: dict[str, str]) -> Optional[str]:
    """Declared type of the receiver when it is a variable, field or ``this.field``."""
    target = invocation.child_by_field_name("object")
    if target is None:
        return None
    if target.type == "identifier":
        return var_types.get(_text(target))
    if target.type == "field_access":
        owner = target.child_by_field_name("object")
        field = target.child_by_field_name("field")
        if owner is not None and owner.type == "this" and field is not None:
            return var_types.get(_text(field))
    return None


# -- Edge construction --------------------------------------------------------------------------

_PREFIX = {
    "sqs": "external:aws-sqs:",
    "sns": "external:aws-sns:",
    "rabbitmq": "external:rabbitmq:",
    "jms": "external:jms:",
    "stream": "external:stream:",
}
_FIELD = {
    "sqs": "queue", "sns": "topic", "rabbitmq": "queue", "jms": "destination",
    "stream": "channel",
}


def _clean(value: Any) -> Any:
    """Repository-derived text, control characters stripped and length bounded, at any depth."""
    from .graph import _sanitize_name  # graph is heavy; import on first edge

    if isinstance(value, str):
        return _sanitize_name(value)
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _edge(
    out: list,
    *,
    service: str,
    direction: str,
    dest: Dest,
    source: str,
    file_path: str,
    line: int,
    resolution: str,
    field: Optional[str] = None,
    routing_key: Optional[Dest] = None,
    extra_fields: Optional[dict[str, Any]] = None,
    operation: Optional[str] = None,
) -> None:
    from .parser import EdgeInfo  # parser imports this module

    if dest.name == "":
        dest = Dest(None, expr="<default exchange>")
    name = dest.name or UNRESOLVED_NAME
    extra: dict[str, Any] = {
        "service": service,
        "direction": direction,
        "resolution": resolution,
        field or _FIELD[service]: dest.name,
    }
    if routing_key is not None:
        extra["routing_key"] = routing_key.name
        if routing_key.unresolved:
            extra["routing_key_unresolved"] = True
            if routing_key.config_key:
                extra["routing_key_config_key"] = routing_key.config_key
    if service in ("sqs", "sns"):
        if operation is None:
            operation = (
                "receive_message" if direction == "listen"
                else "send_message" if service == "sqs" else "publish"
            )
        extra["operation"] = operation
        extra["sdk_operation"] = sdk_operation_name(operation)
        extra["access"] = classify_access(service, operation)
    if dest.unresolved:
        extra["unresolved"] = True
        if dest.getter:
            extra["config_getter"] = {
                "type": dest.getter[0][:128], "property": dest.getter[1][:128],
            }
        for key, value in (
            ("config_key", dest.config_key), ("env_var", dest.env_var), ("expr", dest.expr),
        ):
            if value:
                extra[key] = value
    if extra_fields:
        extra.update(extra_fields)
    extra = _clean(extra)
    out.append(EdgeInfo(
        kind="CONSUMES" if direction == "listen" else "PRODUCES",
        source=source,
        target=f"{_PREFIX[service]}{_clean(name)}",
        file_path=file_path,
        line=line,
        extra=extra,
    ))


# -- Java entry ---------------------------------------------------------------------------------


def _java_annotations(method_node) -> list:
    out: list = []
    for child in method_node.children:
        if child.type == "modifiers":
            out.extend(
                a for a in child.children if a.type in ("annotation", "marker_annotation")
            )
    return out


def _enclosing_class(node):
    parent = node.parent
    while parent is not None:
        if parent.type in _JAVA_CLASS_NODES:
            return parent
        parent = parent.parent
    return None


def _rabbit_binding_edges(binding, line_node, consts, source, file_path, out) -> int:
    """One consume edge on the exchange of a ``@QueueBinding`` per (exchange, key) pair."""
    queue_name: Optional[str] = None
    for queue_ann in _nested_annotations(binding, "Queue"):
        nodes = _ann_value_nodes(queue_ann, ("value", "name"))
        if nodes:
            queue_name = _java_dest(nodes[0], consts, _plain_name).name or None
            break
    exchanges: list[Dest] = []
    for node in _ann_value_nodes(binding, ("exchange",)):
        if node.type in ("annotation", "marker_annotation"):
            inner = _ann_value_nodes(node, ("value", "name"))
            node = inner[0] if inner else None
        if node is not None:
            exchanges.append(_java_dest(node, consts, _plain_name))
    keys = [_java_dest(n, consts, _plain_name) for n in _ann_value_nodes(binding, ("key",))]
    if not keys:
        keys = [Dest("")]  # a fanout or headers binding states no key
    count = 0
    seen: set[tuple[Optional[str], Optional[str], Optional[str]]] = set()
    for exchange in exchanges:
        if exchange.name == "":
            continue  # the default exchange is every queue's own name, already an edge
        for key in keys:
            marker = (exchange.name, key.name, key.expr)
            if marker in seen:
                continue
            seen.add(marker)
            fields: dict[str, Any] = {"binding_key": key.name}
            if key.unresolved:
                fields["binding_key_unresolved"] = True
                if key.config_key:
                    fields["binding_key_config_key"] = key.config_key
                if key.expr:
                    fields["binding_key_expr"] = key.expr[:_MAX_EXPR]
            if queue_name is not None:
                fields["queue"] = queue_name
            _edge(
                out, service="rabbitmq", direction="listen", dest=exchange, source=source,
                file_path=file_path, line=line_node.start_point[0] + 1,
                resolution="rabbit_listener_binding", field="exchange", extra_fields=fields,
            )
            count += 1
    return count


def _java_listener_edges(method_node, consts, source, file_path, out) -> tuple[int, str | None]:
    """Edges of the listener annotations of one method; the listener's service, if any."""
    count = 0
    listener_service: Optional[str] = None
    annotations = _java_annotations(method_node)
    by_name = {_ann_name(a): a for a in annotations}

    def emit(service: str, node, ann, resolution: str, splitter) -> None:
        nonlocal count, listener_service
        listener_service = service
        _edge(
            out, service=service, direction="listen", dest=_java_dest(node, consts, splitter),
            source=source, file_path=file_path, line=ann.start_point[0] + 1,
            resolution=resolution,
        )
        count += 1

    def emit_unnamed(service: str, ann, resolution: str) -> None:
        nonlocal count, listener_service
        listener_service = service
        _edge(
            out, service=service, direction="listen", dest=Dest(None, expr="<no destination>"),
            source=source, file_path=file_path, line=ann.start_point[0] + 1,
            resolution=resolution,
        )
        count += 1

    ann = by_name.get("SqsListener")
    if ann is not None:
        nodes = _ann_value_nodes(ann, ("value", "queueNames", "queues"))
        for node in nodes:
            emit("sqs", node, ann, "sqs_listener", name_from_reference)
        if not nodes:
            emit_unnamed("sqs", ann, "sqs_listener")

    ann = by_name.get("JmsListener")
    if ann is not None:
        nodes = _ann_value_nodes(ann, ("destination",))
        # ``@JmsListener`` is generic JMS; it is SQS only when the file uses the SQS JMS library.
        root = method_node
        while root.parent is not None:
            root = root.parent
        sqs_jms = b"com.amazonaws.services.sqs" in root.text or b"javamessaging" in root.text
        service = "sqs" if sqs_jms else "jms"
        splitter = name_from_reference if sqs_jms else _plain_name
        for node in nodes:
            emit(service, node, ann, "jms_listener", splitter)
        if not nodes:
            emit_unnamed(service, ann, "jms_listener")

    rabbit_sources: list[tuple[Any, Any]] = []
    ann = by_name.get("RabbitListener")
    if ann is not None:
        rabbit_sources.append((ann, ann))
    elif "RabbitHandler" in by_name:
        cls = _enclosing_class(method_node)
        if cls is not None:
            for cls_mod in cls.children:
                if cls_mod.type != "modifiers":
                    continue
                for cls_ann in cls_mod.children:
                    if (
                        cls_ann.type in ("annotation", "marker_annotation")
                        and _ann_name(cls_ann) == "RabbitListener"
                    ):
                        rabbit_sources.append((cls_ann, by_name["RabbitHandler"]))
    for rabbit_ann, line_node in rabbit_sources:
        nodes = _ann_value_nodes(rabbit_ann, ("queues",))
        for queue_ann in _nested_annotations(rabbit_ann, "Queue"):
            nodes.extend(_ann_value_nodes(queue_ann, ("value", "name")))
        for node in nodes:
            emit("rabbitmq", node, line_node, "rabbit_listener", _plain_name)
        if not nodes:
            emit_unnamed("rabbitmq", line_node, "rabbit_listener")
        for binding in _nested_annotations(rabbit_ann, "QueueBinding"):
            bound = _rabbit_binding_edges(binding, line_node, consts, source, file_path, out)
            if bound:
                listener_service = "rabbitmq"
                count += bound

    ann = by_name.get("StreamListener")
    if ann is not None:
        # Spring Cloud Stream's legacy binding: the channel is the annotation's value or target.
        nodes = _ann_value_nodes(ann, ("value", "target"))
        for node in nodes:
            emit("stream", node, ann, "stream_listener", _plain_name)
        if not nodes:
            emit_unnamed("stream", ann, "stream_listener")

    send_to = by_name.get("SendTo")
    if send_to is not None and listener_service is not None:
        for node in _ann_value_nodes(send_to, ("value",)):
            _edge(
                out, service=listener_service, direction="publish",
                dest=_java_dest(
                    node, consts,
                    name_from_reference if listener_service == "sqs" else _plain_name,
                ),
                source=source, file_path=file_path, line=send_to.start_point[0] + 1,
                resolution="send_to",
            )
            count += 1
    return count, listener_service


def _args(invocation) -> list:
    arguments = invocation.child_by_field_name("arguments")
    return [c for c in arguments.named_children if c.type != "comment"] if arguments else []


def _builder_destination(node, keys: frozenset[str]) -> list:
    """Arguments of ``.queueUrl(..)``-style builder calls anywhere inside *node*."""
    found: list = []
    if node.type == "method_invocation":
        name = node.child_by_field_name("name")
        if name is not None and _text(name) in keys:
            args = _args(node)
            if args:
                found.append(args[0])
    for child in node.children:
        found.extend(_builder_destination(child, keys))
    return found


def _java_call_edges(method_node, consts, var_types, source, file_path, out) -> int:
    count = 0

    def visit(node, is_root: bool) -> None:
        nonlocal count
        if not is_root and node.type in _JAVA_SKIP_DESCENT:
            return
        if node.type == "method_invocation":
            count += _java_invocation(node)
        for child in node.children:
            visit(child, False)

    def _java_invocation(invocation) -> int:
        receiver_type = _java_receiver(invocation, var_types)
        service = _JAVA_CLIENT_TYPES.get(receiver_type or "")
        name_node = invocation.child_by_field_name("name")
        if service is None or name_node is None:
            return 0
        method = _text(name_node)
        if method not in _SERVICE_METHODS[service]:
            return 0
        direction = "listen" if method in _RECEIVE_METHODS | _DELETE_METHODS else "publish"
        operation = _JAVA_OPERATION.get(method) or _snake_case(method)
        if method == "convertAndSend" and service == "sns":
            operation = "publish"
        line = invocation.start_point[0] + 1
        args = _args(invocation)
        resolution = f"{service}_client_call"

        if service in ("sqs", "sns"):
            splitter = name_from_reference
            dests = _builder_destination(invocation, _BUILDER_KEYS[service])
            if not dests and args and args[0].type not in (
                "lambda_expression", "method_reference",
            ):
                first = args[0]
                if first.type == "object_creation_expression" and _args(first):
                    first = _args(first)[0]  # ``new SendMessageRequest(queueUrl, body)``
                dests = [first]
            if not dests:
                _edge(
                    out, service=service, direction=direction,
                    dest=Dest(None, expr="<no destination>"), source=source,
                    file_path=file_path, line=line, resolution=resolution, operation=operation,
                )
                return 1
            for node in dests:
                _edge(
                    out, service=service, direction=direction,
                    dest=_java_dest(node, consts, splitter, var_types), source=source,
                    file_path=file_path, line=line, resolution=resolution, operation=operation,
                )
            return len(dests)

        if service == "rabbitmq" and direction == "publish":
            return _rabbit_publish(invocation, args, line)
        # JMS send/receive, Rabbit receive: the first argument names the destination.
        first = args[0] if args else None
        if first is not None and first.type == "object_creation_expression":
            literals = [a for a in _args(first) if a.type == "string_literal"]
            first = literals[0] if literals else first
        if first is None or first.type in ("lambda_expression", "method_reference"):
            dest = Dest(None, expr="<default destination>")
        else:
            dest = _java_dest(first, consts, _plain_name)
        _edge(
            out, service=service, direction=direction, dest=dest, source=source,
            file_path=file_path, line=line, resolution=resolution,
        )
        return 1

    def _rabbit_publish(invocation, args, line) -> int:
        resolution = "rabbit_template_call"
        post_processor = len(args) >= 3 and args[2].type in (
            "lambda_expression", "method_reference",
        )
        if len(args) >= 3 and not post_processor:
            exchange = _java_dest(args[0], consts, _plain_name)
            routing = _java_dest(args[1], consts, _plain_name)
            default_exchange = exchange.name == ""
            if default_exchange:
                exchange = routing
            _edge(
                out, service="rabbitmq", direction="publish", dest=exchange, source=source,
                file_path=file_path, line=line, resolution=resolution,
                field="queue" if default_exchange else "exchange", routing_key=routing,
            )
            return 1
        if len(args) >= 2:
            # (routingKey, message[, postProcessor]) on the template's default exchange.
            routing = _java_dest(args[0], consts, _plain_name)
            _edge(
                out, service="rabbitmq", direction="publish", dest=routing, source=source,
                file_path=file_path, line=line, resolution=resolution, field="queue",
                routing_key=routing,
            )
            return 1
        _edge(
            out, service="rabbitmq", direction="publish",
            dest=Dest(None, expr="<default exchange and routing key>"), source=source,
            file_path=file_path, line=line, resolution=resolution, field="exchange",
        )
        return 1

    body = next((c for c in method_node.children if c.type in ("block", "constructor_body")), None)
    if body is not None:
        visit(body, True)
    return count


# -- Python -------------------------------------------------------------------------------------

_PY_SERVICES = frozenset({"sqs", "sns"})
_PY_SQS_PUBLISH = frozenset({"send_message", "send_message_batch", "send_messages"})
_PY_SQS_LISTEN = frozenset({
    "receive_message", "receive_messages", "delete_message", "delete_message_batch",
    "delete_messages",
})
_PY_SNS_PUBLISH = frozenset({"publish", "publish_batch"})
_PY_ENV = re.compile(
    r"""(?:environ(?:\.get)?|getenv)\W+['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)
_PY_SKIP_DESCENT = frozenset({"function_definition", "class_definition", "lambda"})


def _py_string(node, consts: dict[str, set[str]], depth: int = 0) -> Optional[str]:
    if depth > _MAX_DEPTH or node is None:
        return None
    if node.type == "string":
        if any(c.type == "interpolation" for c in node.children):
            return None
        prefix = next((c for c in node.children if c.type == "string_start"), None)
        if prefix is not None and re.search(r"[bB]", _text(prefix)):
            return None
        return "".join(_text(c) for c in node.children if c.type == "string_content")
    if node.type == "identifier":
        values = consts.get(_text(node))
        return next(iter(values)) if values and len(values) == 1 else None
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and obj.type == "identifier" and _text(obj) == "self" and attr:
            values = consts.get(f"self.{_text(attr)}")
            return next(iter(values)) if values and len(values) == 1 else None
        return None
    if node.type == "binary_operator":
        op = node.child_by_field_name("operator")
        if op is None or _text(op) != "+":
            return None
        left = _py_string(node.child_by_field_name("left"), consts, depth + 1)
        right = _py_string(node.child_by_field_name("right"), consts, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else _UNKNOWN) + (
            right if right is not None else _UNKNOWN
        )
    if node.type == "parenthesized_expression":
        inner = node.named_children
        return _py_string(inner[0], consts, depth + 1) if inner else None
    return None


def _py_dest(node, consts, splitter) -> Dest:
    raw = _py_string(node, consts)
    if raw is not None:
        return splitter(raw)
    text = " ".join(_text(node).split())
    env = _PY_ENV.search(text)
    return Dest(None, env_var=env.group(1) if env else None, expr=text[:_MAX_EXPR])


def _py_call_target_service(call) -> Optional[str]:
    """``sqs``/``sns`` for ``<x>.client("sqs")`` / ``<x>.resource("sns")``, else ``None``."""
    function = call.child_by_field_name("function")
    if function is None or function.type != "attribute":
        return None
    attr = function.child_by_field_name("attribute")
    if attr is None or _text(attr) not in ("client", "resource"):
        return None
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return None
    for index, arg in enumerate(arguments.named_children):
        value = arg
        if arg.type == "keyword_argument":
            key = arg.child_by_field_name("name")
            if key is None or _text(key) != "service_name":
                continue
            value = arg.child_by_field_name("value")
        elif index != 0:
            continue
        literal = _py_string(value, {})
        if literal in _PY_SERVICES:
            return literal
    return None


def _py_target_name(node) -> Optional[str]:
    if node.type == "identifier":
        return _text(node)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and obj.type == "identifier" and _text(obj) == "self" and attr:
            return f"self.{_text(attr)}"
    return None


def _py_walk(node, visit, root=None) -> None:
    if root is not None and node is not root and node.type in _PY_SKIP_DESCENT:
        return
    visit(node)
    for child in node.children:
        _py_walk(child, visit, root)


def _py_file_bindings(module) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, Dest]]:
    """``(string constants, client services, queue/topic objects)`` bound anywhere in the file."""
    consts: dict[str, set[str]] = {}
    clients: dict[str, set[str]] = {}
    objects: dict[str, Dest] = {}

    def visit(node) -> None:
        if node.type != "assignment":
            return
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            return
        name = _py_target_name(left)
        if name is None:
            return
        raw = _py_string(right, consts)
        if raw is not None:
            consts.setdefault(name, set()).add(raw)
            return
        calls = [right] if right.type == "call" else []
        calls += [
            c for c in _descendants(right, "call") if c is not right
        ]
        for call in calls:
            service = _py_call_target_service(call)
            if service:
                clients.setdefault(name, set()).add(service)
                return
            function = call.child_by_field_name("function")
            attr = function.child_by_field_name("attribute") if function is not None else None
            method = _text(attr) if attr is not None else (
                _text(function) if function is not None else ""
            )
            args = call.child_by_field_name("arguments")
            arg_nodes = args.named_children if args is not None else []
            if method == "get_queue_url":
                for arg in arg_nodes:
                    if arg.type == "keyword_argument" and _text(
                        arg.child_by_field_name("name")
                    ) == "QueueName":
                        literal = _py_string(arg.child_by_field_name("value"), consts)
                        if literal:
                            consts.setdefault(name, set()).add(literal)
                            return
            if method in ("get_queue_by_name", "Queue", "Topic", "create_queue", "create_topic"):
                for arg in arg_nodes:
                    value = arg
                    if arg.type == "keyword_argument":
                        key = _text(arg.child_by_field_name("name"))
                        if key not in ("QueueName", "Name", "arn", "url"):
                            continue
                        value = arg.child_by_field_name("value")
                    objects[name] = _py_dest(value, consts, name_from_reference)
                    return

    _py_walk(module, visit)
    return consts, clients, objects


def _descendants(node, node_type: str) -> list:
    found: list = []
    for child in node.children:
        if child.type == node_type:
            found.append(child)
        found.extend(_descendants(child, node_type))
    return found


def _py_kwargs(call) -> tuple[list, dict[str, Any]]:
    args = call.child_by_field_name("arguments")
    positional: list = []
    keywords: dict[str, Any] = {}
    for arg in args.named_children if args is not None else []:
        if arg.type == "keyword_argument":
            key = arg.child_by_field_name("name")
            value = arg.child_by_field_name("value")
            if key is not None and value is not None:
                keywords[_text(key)] = value
        elif arg.type != "comment":
            positional.append(arg)
    return positional, keywords


def _python_edges(function_node, source, file_path, out) -> int:
    module = function_node
    while module.parent is not None:
        module = module.parent
    consts, clients, objects = _py_file_bindings(module)
    # A client bound inside this function wins over one bound elsewhere under the same name.
    local_consts, local_clients, local_objects = _py_file_bindings(function_node)
    consts = {**consts, **local_consts}
    count = 0

    def client_service(receiver) -> Optional[str]:
        name = _py_target_name(receiver)
        if name is not None:
            local = local_clients.get(name)
            services = local if local else clients.get(name)
            if services and len(services) == 1:
                return next(iter(services))
            return None
        if receiver.type == "call":
            return _py_call_target_service(receiver)
        return None

    def visit(node) -> None:
        nonlocal count
        if node.type != "call":
            return
        function = node.child_by_field_name("function")
        if function is None or function.type != "attribute":
            return
        attr = function.child_by_field_name("attribute")
        receiver = function.child_by_field_name("object")
        if attr is None or receiver is None:
            return
        method = _text(attr)
        line = node.start_point[0] + 1
        positional, keywords = _py_kwargs(node)

        # pika: the method names are specific to the AMQP channel.
        if method == "basic_publish":
            exchange_node = keywords.get("exchange") or (positional[0] if positional else None)
            routing_node = keywords.get("routing_key") or (
                positional[1] if len(positional) > 1 else None
            )
            if exchange_node is None and routing_node is None:
                return
            exchange = _py_dest(exchange_node, consts, _plain_name) if exchange_node else None
            routing = _py_dest(routing_node, consts, _plain_name) if routing_node else None
            default_exchange = exchange is not None and exchange.name == ""
            if default_exchange and routing is not None:
                _edge(
                    out, service="rabbitmq", direction="publish", dest=routing, source=source,
                    file_path=file_path, line=line, resolution="pika_basic_publish",
                    field="queue", routing_key=routing,
                )
            else:
                _edge(
                    out, service="rabbitmq", direction="publish",
                    dest=exchange or Dest(None, expr="<no exchange>"), source=source,
                    file_path=file_path, line=line, resolution="pika_basic_publish",
                    field="exchange", routing_key=routing,
                )
            count += 1
            return
        if method == "basic_consume":
            queue_node = keywords.get("queue") or (positional[0] if positional else None)
            dest = (
                _py_dest(queue_node, consts, _plain_name)
                if queue_node is not None else Dest(None, expr="<no queue>")
            )
            _edge(
                out, service="rabbitmq", direction="listen", dest=dest, source=source,
                file_path=file_path, line=line, resolution="pika_basic_consume", field="queue",
            )
            count += 1
            return

        # boto3: queue/topic resource objects, then clients.
        name = _py_target_name(receiver)
        bound = (local_objects.get(name) or objects.get(name)) if name else None
        if bound is not None and (method in _PY_SQS_PUBLISH | _PY_SQS_LISTEN | _PY_SNS_PUBLISH):
            service = "sns" if method in _PY_SNS_PUBLISH else "sqs"
            direction = "listen" if method in _PY_SQS_LISTEN else "publish"
            _edge(
                out, service=service, direction=direction, dest=bound, source=source,
                file_path=file_path, line=line, resolution="boto3_resource_call",
                operation=method,
            )
            count += 1
            return
        service = client_service(receiver)
        if service == "sqs" and method in _PY_SQS_PUBLISH | _PY_SQS_LISTEN:
            key, splitter = "QueueUrl", name_from_reference
            direction = "listen" if method in _PY_SQS_LISTEN else "publish"
        elif service == "sns" and method in _PY_SNS_PUBLISH:
            key, splitter, direction = "TopicArn", name_from_reference, "publish"
        else:
            return
        value = keywords.get(key) or (keywords.get("TargetArn") if service == "sns" else None)
        if value is None and service == "sqs":
            value = keywords.get("QueueName")
        dest = (
            _py_dest(value, consts, splitter) if value is not None
            else Dest(None, expr="<no destination>")
        )
        _edge(
            out, service=service, direction=direction, dest=dest, source=source,
            file_path=file_path, line=line, resolution="boto3_client_call", operation=method,
        )
        count += 1

    _py_walk(function_node, visit, function_node)
    return count


# -- Queue triggers -----------------------------------------------------------------------------

#: ``extra.resolution`` of a consumer edge that a container invokes (a listener annotation);
#: any other consumer edge is a method that polls the queue through an SDK receive call.
LISTENER_RESOLUTIONS = frozenset({
    "sqs_listener", "jms_listener", "rabbit_listener", "rabbit_listener_binding",
    "stream_listener", "kafka_listener",
})


def trigger_edge(consumer):
    """The ``HANDLES`` edge that makes a queue consumer the trigger of its method.

    It points at the same target as the ``CONSUMES`` edge, so ``listeners_of`` on a queue
    node answers with the methods that read it. ``extra.trigger`` is ``"queue"`` and
    ``extra.via`` is ``"listener"`` (the container calls the method) or ``"poll"`` (the method
    receives from the queue itself).
    """
    from .parser import EdgeInfo  # parser imports this module

    via = "listener" if consumer.extra.get("resolution") in LISTENER_RESOLUTIONS else "poll"
    return EdgeInfo(
        kind="HANDLES",
        source=consumer.source,
        target=consumer.target,
        file_path=consumer.file_path,
        line=consumer.line,
        extra={**consumer.extra, "trigger": "queue", "via": via},
    )


# -- Entry point --------------------------------------------------------------------------------


def emit_messaging_edges(
    node,
    source: str,
    file_path: str,
    language: str,
    edges: list,
    method_extra: dict,
    const_index: Optional[JavaConstIndex] = None,
) -> int:
    """Append the messaging edges of one function or method to *edges*; return their count."""
    out: list = []
    if language == "java":
        if node.type not in ("method_declaration", "constructor_declaration"):
            return 0
        scope = _enclosing_class(node) or node
        view: Optional[dict] = None
        if const_index:
            root = node
            while root.parent is not None:
                root = root.parent
            view = _ConstView(const_index, root)
        consts = _java_collect_consts(scope, view)
        var_types = _java_var_types(node)
        listeners, listener_service = _java_listener_edges(node, consts, source, file_path, out)
        calls = _java_call_edges(node, consts, var_types, source, file_path, out)
        if listener_service:
            method_extra["message_listener"] = listener_service
        total = listeners + calls
        # A delete or purge call acknowledges a message; only a receive makes a trigger.
        triggers = [
            trigger_edge(edge)
            for edge in out
            if edge.kind == "CONSUMES" and edge.extra.get("access") != "delete"
        ]
        if triggers:
            method_extra["queue_trigger"] = True
        out.extend(triggers)
    elif language == "python":
        if node.type != "function_definition":
            return 0
        total = _python_edges(node, source, file_path, out)
    else:
        return 0
    if total:
        method_extra["messaging_edge_count"] = total
        edges.extend(out)
    return total
