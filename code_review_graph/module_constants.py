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
    """Whether a URL may hold a secret: a webhook, or a path segment shaped like a token.

    Such a URL is dropped whole, not trimmed. An allow-list's URLs are plain host and path
    (``/v2/admin/verticals/refresh``); a long mixed letter-digit segment, a UUID, an account
    id or a ``hooks``/``webhook`` host or segment is what a credential in a URL looks like.
    """
    from .aws_config_names import _looks_like_token

    rest = url.partition("://")[2]
    host, _, path = rest.partition("/")
    if _WEBHOOK_HOST.search(host.lower()):
        return True
    for segment in (s for s in path.split("/") if s):
        if _WEBHOOK_SEGMENT.fullmatch(segment) or _UUID.fullmatch(segment):
            return True
        if _looks_like_token(segment) or (
            len(segment) >= 20 and not re.search(r"[._\-]", segment)
            and re.search(r"[A-Za-z]", segment) and re.search(r"[0-9]", segment)
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
