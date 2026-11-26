"""Client ``Endpoint`` nodes for Python ``requests`` / ``httpx`` calls.

A call such as ``requests.post(url, ...)``, ``requests.request("GET", url)``, ``httpx.get(url)``
or ``session.get(url)`` (``session`` made by ``requests.Session()``, ``httpx.Client()`` or
``httpx.AsyncClient()``) in a module that imports ``requests`` or ``httpx`` becomes one client
``Endpoint`` (``role="client"``, ``client="py_http"``) and a ``REQUESTS`` edge from the calling
function. ``cross_repo_requests`` with ``origin="external"`` groups them by host.

What is stored, and what never is:

* the **host** only, lower-cased, without scheme, port, userinfo, path, query or fragment. The
  route kept on the node is always ``/``. A webhook or token in the path or query string, and
  credentials in the URL, are therefore never stored;
* a host is kept only if it is a plain host name that does not look like a credential
  (:func:`~code_review_graph.config_keys.value_looks_secret`) and holds no placeholder;
* when the URL is not a literal (or a same-file constant that is), the node says
  ``host_unresolved`` and keeps only the *name* of the variable, attribute or environment
  variable the URL comes from (``webhook_url``, ``self.base``, ``SLACK_URL``), never a value;
* ``sensitive_url`` records that :func:`~code_review_graph.module_constants._url_is_sensitive`
  judged the literal URL to carry a secret (a webhook host or token-shaped path); the host is
  still the only part kept.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

from .aws_sdk_calls import _DYN, _Python, _Scopes, _walk
from .config_keys import value_looks_secret
from .module_constants import _url_is_sensitive

CLIENT_TAG = "py_http"
REQUESTS_EDGE = "REQUESTS"
MAX_CLIENT_CALLS = 200
#: The one route every node carries: the path of a URL is never stored.
ROUTE = "/"

_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_MODULES = frozenset({"requests", "httpx"})
_SESSION_CLASSES = frozenset({"Session", "Client", "AsyncClient"})
_SKIP_DIRS = frozenset({
    "tests", "test", "testing", "fixtures", "venv", ".venv", "site-packages", "node_modules",
    "build", "dist",
})
_HOST = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?)*$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_QUICK = (b"requests", b"httpx")


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def host_of(url: str) -> Optional[str]:
    """The host of an absolute http(s) URL, or ``None`` when it is not a safe plain host name.

    Only the host name is returned. A placeholder, a host that looks like a credential, an
    over-long name and anything but ``[a-z0-9.-]`` give ``None``.
    """
    text = url.strip()
    if not re.match(r"^https?://", text, re.I) or _DYN in text:
        return None
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
        raw = parts.netloc.rsplit("@", 1)[-1]  # keeps the case a token prefix is matched on
    except ValueError:
        return None
    if value_looks_secret(raw):
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host or len(host) > 253 or not _HOST.match(host) or value_looks_secret(host):
        return None
    return host


class _Imports:
    """What this module binds from ``requests`` / ``httpx``."""

    def __init__(self, root: Any, src: bytes) -> None:
        self.modules: dict[str, str] = {}  # local name -> "requests" | "httpx"
        self.functions: dict[str, tuple[str, str]] = {}  # local name -> (library, verb|request)
        self.classes: dict[str, str] = {}  # local name -> library (Session, Client, ...)
        for node in _walk(root):
            if node.type == "import_statement":
                for child in node.named_children:
                    name, alias = self._dotted(child, src)
                    if name in _MODULES:
                        self.modules[alias or name] = name
            elif node.type == "import_from_statement":
                module = node.child_by_field_name("module_name")
                lib = _text(module, src) if module is not None else ""
                if lib not in _MODULES:
                    continue
                for child in node.named_children:
                    if child == module:
                        continue
                    name, alias = self._dotted(child, src)
                    local = alias or name
                    if name in _VERBS or name == "request":
                        self.functions[local] = (lib, name)
                    elif name in _SESSION_CLASSES:
                        self.classes[local] = lib

    @staticmethod
    def _dotted(node: Any, src: bytes) -> tuple[str, str]:
        if node.type == "aliased_import":
            name = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            return (_text(name, src) if name is not None else "",
                    _text(alias, src) if alias is not None else "")
        return _text(node, src), ""

    def any(self) -> bool:
        return bool(self.modules or self.functions or self.classes)


class _Finder:
    def __init__(self, root: Any, src: bytes) -> None:
        self.root, self.src = root, src
        self.imports = _Imports(root, src)
        self.py = _Python(src, root, None)  # type: ignore[arg-type]
        #: session variable text -> (library, literal base host or None)
        self.sessions: dict[str, tuple[str, Optional[str]]] = {}

    # sessions
    def _session_of(self, call: Any) -> Optional[tuple[str, Optional[str]]]:
        """``(library, base host)`` when *call* builds a Session/Client, else ``None``."""
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        lib: Optional[str] = None
        if fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            attr = fn.child_by_field_name("attribute")
            if obj is not None and attr is not None and _text(obj, self.src) in \
                    self.imports.modules and _text(attr, self.src) in _SESSION_CLASSES:
                lib = self.imports.modules[_text(obj, self.src)]
        elif fn.type == "identifier":
            lib = self.imports.classes.get(_text(fn, self.src))
        if lib is None:
            return None
        base: Optional[str] = None
        args = call.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                if arg.type != "keyword_argument":
                    continue
                name = arg.child_by_field_name("name")
                value = arg.child_by_field_name("value")
                if name is not None and value is not None and _text(name, self.src) in (
                    "base_url", "base"
                ):
                    template = self.py.val(value).template
                    if template and _DYN not in template:
                        base = host_of(template)
        return lib, base

    def collect_sessions(self) -> None:
        for node in _walk(self.root):
            target = value = None
            if node.type == "assignment":
                target = node.child_by_field_name("left")
                value = node.child_by_field_name("right")
            elif node.type == "as_pattern" and node.named_child_count >= 2:
                value, target = node.named_children[0], node.named_children[-1]
                if target.type == "as_pattern_target" and target.named_child_count:
                    target = target.named_children[0]
            if target is None or value is None or value.type != "call":
                continue
            if target.type not in ("identifier", "attribute"):
                continue
            found = self._session_of(value)
            if found is not None:
                self.sessions[_text(target, self.src)] = found

    # calls
    def client_of(self, call: Any) -> Optional[tuple[str, str, Optional[str]]]:
        """``(library, verb or "request", session base host)`` of a client call, else ``None``."""
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            found = self.imports.functions.get(_text(fn, self.src))
            return (found[0], found[1], None) if found else None
        if fn.type != "attribute":
            return None
        attr = _text(fn.child_by_field_name("attribute"), self.src)
        if attr not in _VERBS and attr != "request":
            return None
        obj = fn.child_by_field_name("object")
        if obj is None:
            return None
        text = _text(obj, self.src)
        if text in self.imports.modules:
            return self.imports.modules[text], attr, None
        if text in self.sessions:
            lib, base = self.sessions[text]
            return lib, attr, base
        if obj.type == "call":
            inline = self._session_of(obj)
            if inline is not None:
                return inline[0], attr, inline[1]
        return None

    def _args(self, call: Any) -> tuple[list[Any], dict[str, Any]]:
        pos: list[Any] = []
        kws: dict[str, Any] = {}
        args = call.child_by_field_name("arguments")
        if args is None:
            return pos, kws
        for arg in args.named_children:
            if arg.type == "keyword_argument":
                name = arg.child_by_field_name("name")
                value = arg.child_by_field_name("value")
                if name is not None and value is not None:
                    kws[_text(name, self.src)] = value
            elif arg.type not in ("comment", "list_splat", "dictionary_splat"):
                pos.append(arg)
        return pos, kws

    def lead_name(self, node: Any, depth: int = 0) -> Optional[str]:
        """The name of the variable, attribute or environment variable a URL starts from."""
        if node is None or depth > 6:
            return None
        t = node.type
        if t in ("identifier", "attribute"):
            text = _text(node, self.src)
            return text if _KEY.match(text) else None
        if t == "string":
            for child in node.children:
                if child.type == "interpolation":
                    inner = child.child_by_field_name("expression")
                    return self.lead_name(inner, depth + 1) if inner is not None else None
                if child.type == "string_content" and _text(child, self.src):
                    return None
            return None
        if t == "binary_operator":
            return self.lead_name(node.child_by_field_name("left"), depth + 1)
        if t == "parenthesized_expression" and node.named_child_count:
            return self.lead_name(node.named_children[0], depth + 1)
        if t in ("call", "subscript"):
            placeholder = self.py.val(node).placeholder
            if placeholder and _KEY.match(placeholder):
                return placeholder
            if t == "call":
                fn = node.child_by_field_name("function")
                if fn is not None and fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    obj = fn.child_by_field_name("object")
                    if attr is not None and _text(attr, self.src) == "format" and obj is not None:
                        return self.lead_name(obj, depth + 1)
        return None

    def target(
        self, call: Any, verb: str, base: Optional[str],
    ) -> tuple[str, Optional[str], Optional[str], bool]:
        """``(method, host, variable name, sensitive)`` for one call."""
        pos, kws = self._args(call)
        method = verb.upper()
        url_node: Optional[Any]
        if verb == "request":
            method_node = kws.get("method") or (pos[0] if pos else None)
            literal = self.py.val(method_node).template if method_node is not None else None
            method = literal.upper() if literal and literal.isalpha() and len(literal) < 10 \
                else "ANY"
            url_node = kws.get("url") or (pos[1] if len(pos) > 1 else None)
        else:
            url_node = kws.get("url") or (pos[0] if pos else None)
        if url_node is None:
            return method, base, None, False
        template = self.py.val(url_node).template
        if template is not None and _DYN not in template.strip():
            host = host_of(template)
            if host is not None:
                return method, host, None, _url_is_sensitive(template.strip())
            if base and not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", template.strip()):
                return method, base, None, False
            return method, None, None, False
        if template is not None:
            lead = template.strip()
            match = re.match(r"^https?://([^/?#\x00]+)(?:[/?#]|$)", lead, re.I)
            if match and _DYN not in match.group(1):
                host = host_of(lead.split("\x00", 1)[0])
                if host is not None:
                    return method, host, None, False
        if base and template is not None and template.startswith("/"):
            return method, base, None, False
        return method, None, self.lead_name(url_node), False


def extract(
    root: Any, source: bytes, file_path: str, rel_path: str, nodes: list[Any],
    edges: list[Any], node_cls: Any, edge_cls: Any, language: str, is_test: bool,
) -> int:
    """Append client ``Endpoint`` nodes and ``REQUESTS`` edges; returns how many nodes."""
    if is_test or any(part in _SKIP_DIRS for part in rel_path.split("/")[:-1]):
        return 0
    if not any(marker in source for marker in _QUICK):
        return 0
    finder = _Finder(root, source)
    if not finder.imports.any():
        return 0
    finder.collect_sessions()
    scopes = _Scopes(nodes, edges, file_path)
    emitted = 0
    for node in _walk(root):
        if node.type != "call" or emitted >= MAX_CLIENT_CALLS:
            continue
        found = finder.client_of(node)
        if found is None:
            continue
        lib, verb, base = found
        method, host, variable, sensitive = finder.target(node, verb, base)
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        caller = scopes.source(line)
        label = caller.split("::", 1)[1] if "::" in caller else "<module>"
        name = f"{label}@py-http[{emitted}] {method} {host or '?'}"
        meta: dict[str, Any] = {
            "annotation": "py-http", "client_method": label, "http_method": method,
            "route": ROUTE, "role": "client", "client": CLIENT_TAG, "client_kind": lib,
            "origin": "py",
        }
        if host is not None:
            meta["host"] = host
            meta["absolute_url"] = True
        else:
            meta["host_unresolved"] = True
            if variable:
                meta["url_variable"] = variable
        if sensitive:
            meta["sensitive_url"] = True
        nodes.append(node_cls(
            kind="Endpoint", name=name, file_path=file_path, line_start=line,
            line_end=end_line, language=language, extra=meta,
        ))
        qualified = f"{file_path}::{name}"
        confidence = (
            {"confidence": 1.0, "confidence_tier": "EXTRACTED"} if host is not None
            else {"confidence": 0.6, "confidence_tier": "INFERRED"}
        )
        edges.append(edge_cls(
            kind=REQUESTS_EDGE, source=caller, target=qualified, file_path=file_path,
            line=line, extra={**meta, "resolution": "py_http_client", **confidence},
        ))
        emitted += 1
    return emitted
