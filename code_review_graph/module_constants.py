"""URL constants of Python modules: an allow-list is data the graph should hold.

``ALLOWED_REFRESH_URLS = frozenset({'https://a.example/x', ...})`` at the top of a module
decides where a lambda may send a token, yet it is neither a function nor a call, so a graph
of definitions and calls says nothing about it. This reads such constants into ``Variable``
nodes (``extra.url_values``) and links the functions that use them with ``REFERENCES`` edges,
so ``references_to`` on the constant finds the code a list of URLs governs.

Only what is safe and useful is kept: a top-level ``UPPER_SNAKE`` name whose value is a
``http(s)`` URL string, or a set, list, tuple, ``frozenset(...)`` or dict of them. A name that
looks like a secret (or a webhook), and any URL with credentials, a query string, a webhook
host or a token-shaped path segment, is left out whole. At most :data:`MAX_CONSTANTS`
constants of :data:`MAX_VALUES` URLs each are kept per file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from .aws_sdk_calls import _Scopes, _walk

RESOLUTION = "module_constant"
MAX_CONSTANTS = 50
MAX_VALUES = 20
MAX_URL_CHARS = 300

_CONSTANT_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,80}")
#: A resource constant may be module-private (``_PROD_NBC_STATE_MACHINE_ARN`` in
#: ``project-disrupt``'s ``_env.py``): one leading underscore is allowed there, nowhere else.
_RESOURCE_CONSTANT_NAME = re.compile(r"_?[A-Z][A-Z0-9_]{1,80}")
_SECRET_NAME = re.compile(
    r"SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|CREDENTIAL|PRIVATE|WEBHOOK|HOOK|SIGNED|SIGNATURE|SAS",
    re.I,
)
_URL = re.compile(r"https?://[^\s'\"<>?#@]{1,%d}" % MAX_URL_CHARS)
#: Hosts whose URLs carry the secret in the path (incoming webhooks and bot APIs), whatever
#: the constant is called.
_WEBHOOK_HOST = re.compile(
    r"(?:^|\.)(?:hooks\.[a-z0-9.\-]+|webhook\.office\.com|discord(?:app)?\.com|"
    r"api\.telegram\.org|[a-z0-9\-]*webhooks?[a-z0-9.\-]*)$",
)
_WEBHOOK_SEGMENT = re.compile(r"web-?hooks?|hooks?|bot\d+:.*", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _url_is_sensitive(url: str) -> bool:
    """Whether a URL may hold a secret: a webhook, or a path piece shaped like a token.

    Such a URL is dropped whole, not trimmed. An allow-list's URLs are plain host and path
    (``/v2/admin/verticals/refresh``); a long mixed letter-digit piece, a UUID, an account id
    or a ``hooks``/``webhook`` host or piece is what a credential in a URL looks like.

    What is checked is exactly what is stored: a URL with any non-ASCII character is dropped,
    so no normalisation can change it between the check and the store. The host is matched
    without port, case or trailing dot; the path is percent-decoded until it stops changing
    (``%2F`` and ``%252F`` become separators) and every ``/`` and ``;`` piece is checked, as
    is every piece of the undecoded path. A URL that cannot be parsed is dropped.
    """
    from urllib.parse import unquote, urlsplit

    from .aws_config_names import _looks_like_token

    if not url.isascii():
        return True
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host or _WEBHOOK_HOST.search(host):
        return True
    decoded = parts.path
    for _ in range(3):
        again = unquote(decoded)
        if again == decoded:
            break
        decoded = again
    pieces = {p for text in (parts.path, decoded) for p in re.split(r"[/;]", text) if p}
    for piece in pieces:
        if _WEBHOOK_SEGMENT.fullmatch(piece) or _UUID.fullmatch(piece):
            return True
        if _looks_like_token(piece) or (
            len(piece) >= 20 and not re.search(r"[._\-]", piece)
            and re.search(r"[A-Za-z]", piece) and re.search(r"[0-9]", piece)
        ):
            return True
    return False


_COLLECTION_TYPES = frozenset({"set", "list", "tuple", "dictionary", "parenthesized_expression"})
_COLLECTION_CALLS = frozenset({"frozenset", "set", "tuple", "list", "dict"})


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _string_value(node: Any, source: bytes) -> Optional[str]:
    """The text of a plain string literal; ``None`` for an f-string, bytes or anything else."""
    if node.type != "string":
        return None
    start = next((c for c in node.children if c.type == "string_start"), None)
    if start is not None and re.search(r"[fFbB]", _text(start, source)):
        return None
    if any(c.type == "interpolation" for c in node.children):
        return None
    return "".join(_text(c, source) for c in node.children if c.type == "string_content")


def _urls_in(node: Any, source: bytes, found: list[str], depth: int = 0) -> None:
    """Collect URL strings from a literal, a collection of them, or ``frozenset({...})``."""
    if len(found) >= MAX_VALUES or depth > 3:
        return
    if node.type == "string":
        value = _string_value(node, source)
        if (
            value and _URL.fullmatch(value) and value not in found
            and not _url_is_sensitive(value)
        ):
            found.append(value)
        return
    if node.type == "call":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and fn.type == "identifier" and _text(fn, source) in _COLLECTION_CALLS \
                and args is not None:
            for child in args.named_children:
                _urls_in(child, source, found, depth + 1)
        return
    if node.type in _COLLECTION_TYPES:
        for child in node.named_children:
            if node.type == "dictionary" and child.type == "pair":
                child = child.child_by_field_name("value") or child
            _urls_in(child, source, found, depth + 1)


def _rebinds(function: Any, name: str, source: bytes) -> bool:
    """Whether a function takes *name* as a parameter or assigns it, so it is not the constant."""
    params = function.child_by_field_name("parameters")
    if params is not None:
        for node in _walk(params):
            if node.type == "identifier" and _text(node, source) == name:
                return True
    for node in _walk(function):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier" and _text(left, source) == name:
                return True
    return False


def python_url_constants(
    path: Path, source: bytes, nodes: list[Any], edges: list[Any],
    get_parser: Callable[[str], Any],
) -> tuple[list[Any], list[Any]]:
    """The ``Variable`` nodes, ``CONTAINS`` and ``REFERENCES`` edges for one Python file."""
    if path.suffix.lower() != ".py" or b"http" not in source:
        return [], []
    file_node = next((n for n in nodes if n.kind == "File"), None)
    parser = get_parser("python")
    if file_node is None or parser is None:
        return [], []
    from .parser import EdgeInfo, NodeInfo

    root = parser.parse(source).root_node
    file_path = file_node.file_path
    out_nodes: list[Any] = []
    out_edges: list[Any] = []
    names: set[str] = set()
    for top in root.children:
        if len(out_nodes) >= MAX_CONSTANTS:
            break
        # Depending on the grammar version a top-level assignment sits directly under the
        # module or inside an ``expression_statement``; the parser accepts both.
        if top.type == "assignment":
            assignment = top
        elif top.type == "expression_statement":
            assignment = next((c for c in top.children if c.type == "assignment"), None)
        else:
            continue
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            continue
        name = _text(left, source)
        if not _CONSTANT_NAME.fullmatch(name) or _SECRET_NAME.search(name) or name in names:
            continue
        urls: list[str] = []
        _urls_in(right, source, urls)
        if not urls:
            continue
        names.add(name)
        line_start, line_end = top.start_point[0] + 1, top.end_point[0] + 1
        out_nodes.append(NodeInfo(
            kind="Variable", name=name, file_path=file_path, line_start=line_start,
            line_end=line_end, language="python",
            extra={"constant": True, "url_values": urls, "value_count": len(urls),
                   "truncated": len(urls) >= MAX_VALUES},
        ))
        out_edges.append(EdgeInfo(
            kind="CONTAINS", source=file_path, target=f"{file_path}::{name}",
            file_path=file_path, line=line_start,
        ))
    if not names:
        return [], []

    scopes = _Scopes(nodes, edges, file_path)
    seen: set[tuple[str, str]] = set()
    for function in _walk(root):
        if function.type != "function_definition":
            continue
        for node in _walk(function):
            if node.type != "identifier":
                continue
            name = _text(node, source)
            if name not in names:
                continue
            line = node.start_point[0] + 1
            owner = scopes.source(line)
            if owner == file_path or (owner, name) in seen or _rebinds(function, name, source):
                continue
            seen.add((owner, name))
            out_edges.append(EdgeInfo(
                kind="REFERENCES", source=owner, target=f"{file_path}::{name}",
                file_path=file_path, line=line, extra={"resolution": RESOLUTION},
            ))
    return out_nodes, out_edges


# -- Resource-like constants ----------------------------------------------------------------

MAX_RESOURCE_VALUE_CHARS = 200
_RESOURCE_NAME = re.compile(
    r"(?:^|_)(?:BUCKET|QUEUE|TOPIC|STREAM|TABLE|STATE_?MACHINE|LAMBDA|FUNCTION|LOG_?GROUP|"
    r"ARN|RULE|CLUSTER|JOB_?QUEUE|JOB_?DEFINITION)(?:_NAME|_ARN)?(?:$|_)",
)
_RESOURCE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{1,%d}" % MAX_RESOURCE_VALUE_CHARS)


def _looks_like_resource(name: str, value: str) -> bool:
    if value.startswith(("arn:aws", "s3://")):
        return True
    return bool(_RESOURCE_NAME.search(name) and _RESOURCE_VALUE.fullmatch(value))


def python_resource_constants(
    path: Path, source: bytes, nodes: list[Any], get_parser: Callable[[str], Any],
) -> tuple[list[Any], list[Any]]:
    """``Variable`` nodes (and ``CONTAINS`` edges) of module-level constants that name a resource.

    A constant is kept when its value is an ARN or ``s3://`` location, or its name says it holds
    a bucket, queue, topic, stream, table, state machine, function, log group or the like and
    the value is a plain name. The value goes through
    :func:`~code_review_graph.config_keys.storable_config_value` (secret-looking names and
    values are dropped, a 12-digit account id becomes ``ACCOUNT``, 200 characters at most) and
    is stored as ``extra.resource_value``. Nothing else of the file is stored. A name already
    stored by :func:`python_url_constants` is skipped.
    """
    from .config_keys import storable_config_value

    if path.suffix.lower() != ".py":
        return [], []
    file_node = next((n for n in nodes if n.kind == "File"), None)
    parser = get_parser("python")
    if file_node is None or parser is None or not re.search(rb"[A-Z][A-Z0-9_]{1,}\s*=", source):
        return [], []
    from .parser import EdgeInfo, NodeInfo

    root = parser.parse(source).root_node
    file_path = file_node.file_path
    taken = {n.name for n in nodes if n.kind == "Variable" and n.file_path == file_path}
    seen: dict[str, int] = {}
    found: list[tuple[str, str, bool, int, int]] = []
    for top in root.children:
        assignment = top if top.type == "assignment" else next(
            (c for c in top.children if c.type == "assignment"), None,
        ) if top.type == "expression_statement" else None
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            continue
        name = _text(left, source)
        seen[name] = seen.get(name, 0) + 1
        value = _string_value(right, source) if right.type == "string" else None
        if value is None or not _RESOURCE_CONSTANT_NAME.fullmatch(name):
            continue
        value = value.strip()
        stored = storable_config_value(name, value)
        if stored is None or not _looks_like_resource(name, stored[0]):
            continue
        found.append((name, stored[0], stored[1], top.start_point[0] + 1, top.end_point[0] + 1))
    out_nodes: list[Any] = []
    out_edges: list[Any] = []
    for name, value, truncated, start, end in found:
        # A name assigned twice in the module has no single value.
        if seen[name] != 1 or name in taken or len(out_nodes) >= MAX_CONSTANTS:
            continue
        extra: dict[str, Any] = {"constant": True, "resource_value": value}
        if truncated:
            extra["value_truncated"] = True
        out_nodes.append(NodeInfo(
            kind="Variable", name=name, file_path=file_path, line_start=start, line_end=end,
            language="python", extra=extra,
        ))
        out_edges.append(EdgeInfo(
            kind="CONTAINS", source=file_path, target=f"{file_path}::{name}",
            file_path=file_path, line=start,
        ))
    return out_nodes, out_edges
