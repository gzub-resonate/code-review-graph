"""A repository's HTTP endpoints, worked out once when the graph is built.

``cross_repo_requests_tool`` joins the calls one repository makes to the endpoints of the
others. Reading a repository's side of that join means turning every ``Endpoint`` node into
a client call or a server endpoint and, for FastAPI and Flask, joining each route to the
prefixes of the routers that mount it, in any file. None of that depends on the other
repositories, so it is done here, at build time, and stored as one row of the graph's
``metadata`` table. The join itself depends on the whole registry and is still computed on
every call.

The stored index is only an accelerator. It carries a stamp of the ``Endpoint`` rows it was
made from; a reader that finds the stamp changed (an update that did not refresh it, a
graph built before this existed, a row it cannot read) works the endpoints out from the
nodes instead and gets the same answer. Nothing here changes what the tool reports.

The rows hold the repository-relative file and leave out the registry's display name and
path, which belong to the registry and not to the graph, so the same index serves a
repository under any alias.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import feign_links as fl
from . import js_api_extract, py_api_extract, py_route_prefixes

if TYPE_CHECKING:
    from .graph import GraphNode, GraphStore

logger = logging.getLogger(__name__)

__all__ = [
    "METADATA_KEY",
    "build_endpoint_index",
    "endpoint_stamp",
    "extract_endpoints",
    "read_endpoint_index",
]

METADATA_KEY = "endpoint_index"
#: Bump when the shape of the stored rows changes; a reader ignores any other value.
FORMAT = 1

Endpoints = tuple[list[fl.ClientCall], list[fl.ServerEndpoint]]


def relative_file(repo_path: Path, file_path: Optional[str]) -> Optional[str]:
    """Return ``file_path`` relative to the repo, or ``None`` when it is not inside.

    A path outside the repository is dropped rather than echoed: the response
    only ever names files the registry entry owns.
    """
    if not file_path:
        return None
    try:
        return Path(file_path).relative_to(repo_path).as_posix()
    except ValueError:
        pass
    try:
        return Path(file_path).resolve().relative_to(repo_path.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def extract_endpoints(nodes: list[GraphNode], repo_path: Path) -> Endpoints:
    """Client calls and server endpoints held by a repository's ``Endpoint`` nodes.

    ``repo`` is left empty on every call and endpoint, and ``origin`` on every server
    endpoint: both are the registry's, and :func:`attach` fills them in.
    """
    clients: list[fl.ClientCall] = []
    servers: list[fl.ServerEndpoint] = []
    py_maps: dict[str, py_route_prefixes.RouterMap] = {}
    py_routes: list[tuple[GraphNode, dict[str, Any], str]] = []
    for node in nodes:
        extra = node.extra if isinstance(node.extra, dict) else {}
        if extra.get("role") == py_api_extract.MAP_ROLE:
            rel = relative_file(repo_path, node.file_path)
            if rel:
                py_maps[rel] = py_route_prefixes.RouterMap(
                    rel, extra.get("routers") or {}, extra.get("mounts") or [],
                    extra.get("imports") or {},
                )
            continue
        route = extra.get("route")
        method = extra.get("http_method")
        if not isinstance(route, str) or not isinstance(method, str):
            continue
        file = relative_file(repo_path, node.file_path)
        if extra.get("role") == "client":
            client_tag = extra.get("client")
            if client_tag == js_api_extract.CLIENT_TAG:
                notes: list[str] = []
                if extra.get("inferred"):
                    notes.append("ember_data_convention")
                if extra.get("receiver_inferred"):
                    notes.append("receiver_inferred")
                if extra.get("base_url_dynamic"):
                    notes.append("base_url_not_literal")
                kind = extra.get("client_kind")
                clients.append(fl.ClientCall(
                    repo="", file=file, line=node.line_start,
                    http_method=method, route=route, origin="js",
                    client_kind=kind if isinstance(kind, str) else None,
                    route_dynamic=bool(extra.get("route_dynamic")),
                    method_dynamic=bool(extra.get("method_dynamic")),
                    absolute_url=bool(extra.get("absolute_url")),
                    notes=tuple(notes),
                ))
                continue
            if client_tag != "feign":
                continue
            service = extra.get("service")
            clients.append(fl.ClientCall(
                repo="", file=file, line=node.line_start,
                http_method=method, route=route,
                service=service if isinstance(service, str) else None,
                service_unresolved=bool(extra.get("service_unresolved")),
                route_dynamic=bool(extra.get("route_dynamic")),
                url_literal=bool(extra.get("url_literal")),
                url_from_argument=bool(extra.get("url_from_argument")),
            ))
        elif extra.get("py_route") and file:
            py_routes.append((node, extra, file))
        else:
            servers.append(fl.ServerEndpoint(
                repo="", file=file, line=node.line_start,
                http_method=method, route=route,
                catch_all=bool(extra.get("catch_all")),
                optional_catch_all=bool(extra.get("optional_catch_all")),
            ))
    servers.extend(_python_servers(py_routes, py_maps))
    return clients, servers


def _python_servers(
    routes: list[tuple[GraphNode, dict[str, Any], str]],
    maps: dict[str, py_route_prefixes.RouterMap],
) -> list[fl.ServerEndpoint]:
    """Server endpoints for FastAPI/Flask routes, with their prefixes joined.

    A router's prefix is its own plus that of every router that mounts it, in
    any file of the repo.  A route whose chain has a prefix that is not a
    literal is left out rather than guessed.
    """
    if not routes:
        return []
    files = set(maps) | {file for _n, _e, file in routes}
    resolver = py_route_prefixes.PrefixResolver(maps, files)
    found: list[fl.ServerEndpoint] = []
    for node, extra, file in routes:
        if extra.get("prefix_dynamic"):
            continue
        own_path = extra.get("route_path")
        path = str(own_path if own_path is not None else extra.get("route") or "")
        for prefix in resolver.prefixes(file, str(extra.get("router_var") or "")):
            if prefix is None:
                continue
            found.append(fl.ServerEndpoint(
                repo="", file=file, line=node.line_start,
                http_method=str(extra.get("http_method")),
                route=py_api_extract.join_path(prefix, path),
                catch_all=bool(extra.get("catch_all")),
            ))
    return found


def attach(label: str, repo_path: Path, endpoints: Endpoints) -> Endpoints:
    """Give each call and endpoint the registry's name and path for its repository."""
    clients, servers = endpoints
    return (
        [dataclasses.replace(c, repo=label) for c in clients],
        [dataclasses.replace(s, repo=label, origin=str(repo_path)) for s in servers],
    )


# ---------------------------------------------------------------------------
# The stored rows
# ---------------------------------------------------------------------------


def _row(item: Any, skip: tuple[str, ...]) -> dict[str, Any]:
    """The fields of *item* that differ from their default, for a compact row."""
    row: dict[str, Any] = {}
    for field in dataclasses.fields(item):
        if field.name in skip:
            continue
        value = getattr(item, field.name)
        if field.default is not dataclasses.MISSING and value == field.default:
            continue
        row[field.name] = list(value) if isinstance(value, tuple) else value
    return row


def _client(row: dict[str, Any]) -> fl.ClientCall:
    fields = dict(row)
    fields["notes"] = tuple(fields.get("notes", ()))
    return fl.ClientCall(repo="", **fields)


def _server(row: dict[str, Any]) -> fl.ServerEndpoint:
    return fl.ServerEndpoint(repo="", **row)


def endpoint_stamp(store: GraphStore) -> str:
    """Digest of the ``Endpoint`` rows, so an index made from other rows is not trusted.

    It covers the count, the last write time, the row ids and the size of every row's
    ``extra``, which together move when an endpoint is added, removed or edited, even
    by an update that finishes within the second ``last_updated`` is recorded in.
    """
    row = store._conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), 0), COALESCE(SUM(id), 0), "
        "COALESCE(SUM(LENGTH(extra)), 0) FROM nodes WHERE kind = 'Endpoint'"
    ).fetchone()
    return hashlib.sha256(json.dumps(list(row)).encode("utf-8")).hexdigest()[:20]


def build_endpoint_index(store: GraphStore, repo_root: Path) -> dict[str, int]:
    """Work out the repository's endpoints and store them in the graph's metadata.

    A repository with no ``Endpoint`` node has its stored index removed, so most
    repositories carry nothing extra. Returns ``{"clients": n, "servers": n}``.
    """
    nodes = store.get_nodes_by_kind(["Endpoint"])
    if not nodes:
        store._conn.execute("DELETE FROM metadata WHERE key = ?", (METADATA_KEY,))
        store.commit()
        return {"clients": 0, "servers": 0}
    clients, servers = extract_endpoints(nodes, Path(repo_root))
    payload = {
        "v": FORMAT,
        "stamp": endpoint_stamp(store),
        "clients": [_row(c, ("repo",)) for c in clients],
        "servers": [_row(s, ("repo", "origin")) for s in servers],
    }
    store.set_metadata(METADATA_KEY, json.dumps(payload, separators=(",", ":")))
    result = {"clients": len(clients), "servers": len(servers)}
    logger.info("Endpoint index stored: %s", result)
    return result


def read_endpoint_index(store: GraphStore) -> Optional[Endpoints]:
    """The stored endpoints, or ``None`` when there is nothing current to use.

    ``None`` covers a graph with no index, one made by another format, one whose
    ``Endpoint`` rows changed since, and a row that does not parse; the caller then
    reads the nodes.
    """
    raw = store.get_metadata(METADATA_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("v") != FORMAT:
            return None
        if data.get("stamp") != endpoint_stamp(store):
            return None
        return (
            [_client(row) for row in data["clients"]],
            [_server(row) for row in data["servers"]],
        )
    except (ValueError, TypeError, KeyError):
        logger.warning("Stored endpoint index is unreadable; reading the nodes instead")
        return None
