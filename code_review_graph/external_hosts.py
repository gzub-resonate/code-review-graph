"""Group the HTTP calls that reach no registered service by host, at query time.

``cross_repo_requests`` with ``origin="external"`` answers "which third-party vendors does
each service depend on". It reads the client ``Endpoint`` nodes of the selected repos
(read-only; nothing is stored) and keeps the calls that do not end at a registered repo:

* a JavaScript/TypeScript call with an absolute URL (``https://host/path``), or whose
  ``baseURL`` or Ember adapter ``host`` is one;
* a Spring Feign client whose service name resolves to no registered repo. The graph keeps
  the service name and the configuration keys of a ``${...}`` placeholder, never the value, so
  such a call is grouped by service name unless its annotation spells the URL out (the host is
  then read from that one source line) or its mapping is an absolute URL;
* a Python ``requests`` / ``httpx`` call (``py_http_clients``). The graph keeps only the host
  of a literal URL and, for any other URL, the *name* of the variable it comes from, so a
  webhook path or a token in a query string is never seen here. Such a call whose URL is not a
  literal is ``host_unresolved``, grouped as ``${variable}`` (or ``<unresolved>``), source
  ``python_http_variable``.

Hosts are lower-cased and stripped of scheme, credentials, port, query and fragment, and a
leading ``www.``. A host or service name that is a placeholder is ``host_unresolved``, with the
property or variable key. A small static table names the vendor of well-known hosts; a tenant
or region subdomain of a vendor marked as collapsing (``*.auth0.com``) shares one group. An
unknown host stays as it is. Java and Go clients that call ``HttpClient`` or ``RestTemplate``
leave no ``Endpoint`` node, so they are not seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .graph import GraphStore
from .js_api_extract import CLIENT_TAG as JS_CLIENT_TAG
from .py_http_clients import CLIENT_TAG as PY_CLIENT_TAG

STATUS_KNOWN = "known_vendor"
STATUS_UNKNOWN = "unknown_vendor"
STATUS_UNRESOLVED = "host_unresolved"
STATUSES = (STATUS_KNOWN, STATUS_UNKNOWN, STATUS_UNRESOLVED)

#: ``(host suffix, vendor, collapse)``. A host equals the suffix or ends with ``.<suffix>``.
#: ``collapse`` folds every subdomain into one ``*.<suffix>`` group (tenants, regions).
VENDOR_HOSTS: tuple[tuple[str, str, bool], ...] = (
    ("graph.facebook.com", "Meta", False),
    ("facebook.com", "Meta", False),
    ("business-api.tiktok.com", "TikTok", False),
    ("tiktok.com", "TikTok", False),
    ("auth0.com", "Auth0", True),
    ("salesforce.com", "Salesforce", True),
    ("force.com", "Salesforce", True),
    ("adobe.io", "Adobe", True),
    ("googleapis.com", "Google", False),
    ("accounts.google.com", "Google", False),
    ("api.linkedin.com", "LinkedIn", False),
    ("api.twitter.com", "X", False),
    ("api.x.com", "X", False),
    ("adsapi.snapchat.com", "Snap", False),
    ("api.snapchat.com", "Snap", False),
    ("api.pinterest.com", "Pinterest", False),
    ("api.stripe.com", "Stripe", False),
    ("api.twilio.com", "Twilio", False),
    ("api.sendgrid.com", "Twilio SendGrid", False),
    ("hooks.slack.com", "Slack", False),
    ("slack.com", "Slack", False),
    ("api.github.com", "GitHub", False),
    ("atlassian.net", "Atlassian", True),
    ("okta.com", "Okta", True),
    ("graph.microsoft.com", "Microsoft", False),
    ("login.microsoftonline.com", "Microsoft", False),
    ("api.pagerduty.com", "PagerDuty", False),
    ("api.datadoghq.com", "Datadog", False),
    ("hooks.zapier.com", "Zapier", False),
    ("liveramp.com", "LiveRamp", True),
)

#: Tokens of a Feign service name that name a vendor (``salesforce-client``, ``shipstation``).
VENDOR_NAME_TOKENS: tuple[tuple[str, str], ...] = (
    ("auth0", "Auth0"), ("salesforce", "Salesforce"), ("sfdc", "Salesforce"),
    ("tiktok", "TikTok"), ("facebook", "Meta"), ("snapchat", "Snap"),
    ("pinterest", "Pinterest"), ("linkedin", "LinkedIn"), ("twilio", "Twilio"),
    ("sendgrid", "Twilio SendGrid"), ("stripe", "Stripe"), ("okta", "Okta"),
    ("shipstation", "ShipStation"), ("shippo", "Shippo"), ("shipengine", "ShipEngine"),
    ("liveramp", "LiveRamp"),
)

_URL_HEAD = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//(?P<authority>[^/?#]*)(?P<rest>.*)$", re.S,
)
_PLACEHOLDER = re.compile(r"\$\{([^}:]*)(?::[^}]*)?\}|\{\{?\s*([A-Za-z0-9_.\-]+)\s*\}\}?")
_UNRESOLVED = "<unresolved>"
_PORT = re.compile(r":\d{0,5}$")
_MAX_PATH = 120
_MAX_LITERAL_SPAN = 12
_MAX_FILE_BYTES = 1_000_000
_SAFE_HOST = re.compile(r"[a-z0-9][a-z0-9.\-]{0,252}")
_SAFE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_ANNOTATION_URL = re.compile(r"\burl\s*=\s*\"([A-Za-z][A-Za-z0-9+.\-]*://[^\"\s]+)\"")


@dataclass(frozen=True)
class Host:
    """A host as written in one call: ``name`` is ``None`` when it is a placeholder."""

    name: Optional[str]
    path: str
    key: Optional[str] = None


def _placeholder_key(text: str) -> Optional[str]:
    found = _PLACEHOLDER.search(text)
    if found is None:
        return None
    return (found.group(1) or found.group(2) or "").strip() or None


def _is_placeholder(text: str) -> bool:
    return "${" in text or "{" in text or _UNRESOLVED in text or "\x00" in text


def split_url(url: str) -> Optional[Host]:
    """The host and path of an absolute URL (or ``//host/path``), or ``None`` for a relative one.

    Credentials, port, query and fragment are dropped. A host that holds a placeholder gives
    ``name=None`` and the property or variable ``key`` (``None`` for ``<unresolved>``).
    """
    text = url.strip()
    found = _URL_HEAD.match(text)
    if found is None:
        return None
    authority = found.group("authority").rsplit("@", 1)[-1]
    rest = found.group("rest")
    for mark in ("?", "#"):
        cut = rest.find(mark)
        if cut != -1:
            rest = rest[:cut]
    path = rest[:_MAX_PATH] or "/"
    if _is_placeholder(authority):
        return Host(None, path, _placeholder_key(authority))
    host = _PORT.sub("", authority).strip().rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    return Host(host, path)


def leading_placeholder(route: str) -> Optional[str]:
    """The key of a ``${KEY}/path`` route that starts with a placeholder, else ``None``.

    Such a route is a base URL held in a variable, so its host is unresolved.
    """
    if not route.startswith("${"):
        return None
    end = route.find("}")
    if end == -1:
        return None
    return _placeholder_key(route[: end + 1])


def vendor_of_host(host: str) -> tuple[Optional[str], str]:
    """``(vendor or None, group key)`` for a host.

    The group key is the host, or ``*.<suffix>`` when the vendor entry collapses subdomains.
    """
    for suffix, vendor, collapse in VENDOR_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return vendor, (f"*.{suffix}" if collapse else host)
    return None, host


def vendor_of_service(service: str) -> Optional[str]:
    """The vendor a Feign service name names by one of its tokens, or ``None``."""
    folded = re.sub(r"[^a-z0-9]+", "", service.lower())
    for token, vendor in VENDOR_NAME_TOKENS:
        if token in folded:
            return vendor
    return None


# ---------------------------------------------------------------------------
# Reading one graph
# ---------------------------------------------------------------------------


def _relative(file_path: str, repo_path: Path) -> str:
    try:
        return Path(file_path).relative_to(repo_path).as_posix()
    except ValueError:
        return file_path


def _annotation_host(
    repo_path: Path, file: str, start: Optional[int], end: Optional[int],
) -> Optional[Host]:
    """The host of a literal ``@FeignClient(url = "...")``, read from the annotation's lines.

    The graph records that the URL is a literal, not what it says. At most a dozen lines of
    one file inside the repo are read; anything unreadable gives ``None``.
    """
    if not start or not file:
        return None
    try:
        root = repo_path.resolve()
        path = (root / file).resolve()
        if root not in path.parents or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None
    last = min(max(end or start, start), start + _MAX_LITERAL_SPAN)
    found = _ANNOTATION_URL.search("\n".join(lines[start - 1:last]))
    return split_url(found.group(1)) if found else None


def read_client_calls(store: GraphStore, repo_path: Path) -> list[dict[str, Any]]:
    """The client calls of one graph that can name an outside host, as plain dicts.

    Each has ``kind`` (``js`` or ``feign``), ``method``, ``file`` and ``line`` (repo-relative),
    and the fields the classifier needs. Calls that surely stay inside the registry (a relative
    JavaScript route) are left out.
    """
    out: list[dict[str, Any]] = []
    for node in store.get_nodes_by_kind(["Endpoint"]):
        extra = node.extra if isinstance(node.extra, dict) else {}
        if extra.get("role") != "client":
            continue
        route = extra.get("route")
        method = extra.get("http_method")
        if not isinstance(route, str) or not isinstance(method, str):
            continue
        file = _relative(node.file_path, repo_path)
        base: dict[str, Any] = {
            "method": method, "route": route, "file": file, "line": node.line_start,
        }
        if extra.get("client") == "feign":
            keys = [k for k in (extra.get("config_keys") or []) if isinstance(k, str)]
            service = extra.get("service")
            call = {
                **base, "kind": "feign",
                "service": service if isinstance(service, str) else None,
                "service_unresolved": bool(extra.get("service_unresolved")),
                "config_keys": keys[:3],
            }
            if extra.get("url_literal"):
                found = _annotation_host(repo_path, file, node.line_start, node.line_end)
                if found is not None:
                    call["literal_host"] = found.name
            out.append(call)
            continue
        if extra.get("client") == PY_CLIENT_TAG:
            host = extra.get("host") if isinstance(extra.get("host"), str) else None
            variable = extra.get("url_variable")
            out.append({
                **base, "kind": "py", "service": None,
                "host": host if host and _SAFE_HOST.fullmatch(host) else None,
                "url_variable": variable
                if isinstance(variable, str) and _SAFE_VARIABLE.fullmatch(variable) else None,
            })
            continue
        if extra.get("client") != JS_CLIENT_TAG:
            continue
        host = extra.get("host") if isinstance(extra.get("host"), str) else None
        base_url = extra.get("base_url") if isinstance(extra.get("base_url"), str) else None
        if (
            extra.get("absolute_url") or host or leading_placeholder(route)
            or (base_url and split_url(base_url))
        ):
            out.append({
                **base, "kind": "js", "service": None,
                "absolute_url": bool(extra.get("absolute_url")),
                "adapter_host": host, "base_url": base_url,
            })
    return out


# ---------------------------------------------------------------------------
# Classifying one call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    """Where a call goes: the group key, its vendor guess and how it was found."""

    key: str
    status: str
    source: str
    vendor: Optional[str] = None
    vendor_basis: Optional[str] = None
    host: Optional[str] = None
    unresolved_key: Optional[str] = None
    path: str = ""


def _from_host(host: Host, source: str) -> Group:
    if host.name is None:
        key = host.key or _UNRESOLVED
        return Group(
            f"${{{key}}}" if host.key else _UNRESOLVED, STATUS_UNRESOLVED, source,
            unresolved_key=host.key, path=host.path,
        )
    vendor, key = vendor_of_host(host.name)
    return Group(
        key, STATUS_KNOWN if vendor else STATUS_UNKNOWN, source, vendor,
        "host_table" if vendor else None, host.name, path=host.path,
    )


def classify_js(call: dict[str, Any]) -> Optional[Group]:
    """The group of a JavaScript call, or ``None`` when its host cannot be told."""
    route: str = call["route"]
    for url, source in (
        (route, "js_absolute_url"), (call.get("adapter_host"), "ember_adapter_host"),
        (call.get("base_url"), "js_base_url"),
    ):
        if not isinstance(url, str):
            continue
        host = split_url(url if "//" in url else f"//{url}") if source == "ember_adapter_host" \
            else split_url(url)
        if host is None:
            continue
        path = host.path
        if source != "js_absolute_url":
            path = split_url(route).path if split_url(route) else route[:_MAX_PATH]
        return _from_host(Host(host.name, path, host.key), source)
    key = leading_placeholder(route)
    if key is not None:
        return Group(f"${{{key}}}", STATUS_UNRESOLVED, "js_leading_placeholder",
                     unresolved_key=key, path=route[:_MAX_PATH])
    return None


def classify_py(call: dict[str, Any]) -> Group:
    """The group of a Python ``requests`` / ``httpx`` call: its host, or an unresolved bucket."""
    host = call.get("host")
    if isinstance(host, str):
        return _from_host(Host(host, "/"), "python_http_literal")
    key = call.get("url_variable")
    return Group(
        f"${{{key}}}" if key else _UNRESOLVED, STATUS_UNRESOLVED, "python_http_variable",
        unresolved_key=key, path="/",
    )


def classify_feign(call: dict[str, Any], registered: bool) -> Optional[Group]:
    """The group of a Feign call that resolves to no registered repo, else ``None``."""
    route: str = call["route"]
    absolute = split_url(route)
    if absolute is not None:
        return _from_host(absolute, "feign_absolute_route")
    if registered:
        return None
    literal = call.get("literal_host")
    path = route[:_MAX_PATH]
    if isinstance(literal, str):
        group = _from_host(Host(literal, path), "feign_url_literal")
        return group
    service = call.get("service")
    if call.get("service_unresolved") or not service:
        keys = call.get("config_keys") or []
        key = keys[0] if keys else None
        return Group(
            f"${{{key}}}" if key else _UNRESOLVED, STATUS_UNRESOLVED, "feign_service_placeholder",
            unresolved_key=key, path=path,
        )
    vendor = vendor_of_service(service)
    return Group(
        f"service:{service}", STATUS_KNOWN if vendor else STATUS_UNKNOWN, "feign_service",
        vendor, "service_name" if vendor else None, None, path=path,
    )
