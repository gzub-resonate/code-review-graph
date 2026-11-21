"""Join outbound Spring Feign calls to the server endpoints they reach.

A ``@FeignClient`` interface in one repository names a service and maps its
methods to HTTP routes.  The service that answers is usually another
registered repository, which holds the matching controller.  The graph keeps
the two ends apart: a client ``Endpoint`` carries the service name, the HTTP
method and the route, and a server ``Endpoint`` carries a method and a route.
This module joins them at query time.  Nothing here reads a database or writes
anything; ``tools/registry_tools.py`` does the reading.

The rules, in the order they apply:

1. A call that cannot be joined safely is ``unmatchable`` and says why: the
   service name is a placeholder, the request goes to a ``java.net.URI``
   argument or a literal ``url``, or the route has a piece the parser could
   not resolve.  These are never guessed.
2. The service name picks the target repository: an exact registry alias or
   folder name first (and the optional alias file, through
   :class:`~code_review_graph.repo_aliases.RepoNameResolver`), then a name
   that differs only in case, ``-`` versus ``_`` and repeated separators.  The
   second step must reach exactly one registered repository.  No target is
   ``no_target_repo``.
3. A server endpoint matches when its HTTP method equals the client's (a
   server mapping that accepts every method matches any) and its route is
   equal after :func:`normalize_route`.
4. One match is ``matched``, several are ``ambiguous``, none is
   ``no_route_match``.  Mapping annotations that list the same route twice
   index twice at one source location, so matches are counted per source
   location, not per node.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "AMBIGUOUS",
    "MATCHED",
    "NO_ROUTE_MATCH",
    "NO_TARGET_REPO",
    "STATUSES",
    "UNMATCHABLE",
    "ClientCall",
    "LinkResult",
    "ServerEndpoint",
    "TargetResolution",
    "link_call",
    "normalize_route",
    "normalize_service_name",
    "resolve_service",
    "unmatchable_reason",
]

MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NO_TARGET_REPO = "no_target_repo"
NO_ROUTE_MATCH = "no_route_match"
UNMATCHABLE = "unmatchable"

#: Every status a call can have, in the order a summary lists them.
STATUSES: tuple[str, ...] = (
    MATCHED, AMBIGUOUS, NO_ROUTE_MATCH, NO_TARGET_REPO, UNMATCHABLE,
)

#: The parser renders a route piece it could not resolve as this marker.
_UNRESOLVED_MARKER = "<unresolved>"

#: A server mapping with no ``method`` is indexed with this verb.
_ANY_METHOD = "ANY"

_SEPARATORS_RE = re.compile(r"[-_]+")
_SLASHES_RE = re.compile(r"/{2,}")


@dataclass(frozen=True)
class ClientCall:
    """One Feign client method mapping, as read from a graph."""

    repo: str
    file: Optional[str]
    line: Optional[int]
    http_method: str
    route: str
    service: Optional[str] = None
    service_unresolved: bool = False
    route_dynamic: bool = False
    url_literal: bool = False
    url_from_argument: bool = False


@dataclass(frozen=True)
class ServerEndpoint:
    """One server endpoint (a mapped controller method), as read from a graph."""

    repo: str
    file: Optional[str]
    line: Optional[int]
    http_method: str
    route: str
    #: Identifies the repository beyond its display name (the registry path),
    #: so sibling checkouts that share a name are not merged into one match.
    origin: str = ""


@dataclass(frozen=True)
class TargetResolution:
    """The registered repositories a service name resolves to.

    ``indexes`` are positions in the registry list the caller resolved
    against (empty when nothing resolved); ``how`` is ``"exact"`` or
    ``"normalized"`` when something did, and ``reason`` explains an empty
    result: ``service_not_registered`` or ``service_name_ambiguous``.
    """

    indexes: tuple[int, ...]
    how: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class LinkResult:
    """The outcome for one client call.

    ``candidates`` holds the server endpoints that matched, one per source
    location and sorted, so the caller can show them whatever the status.
    """

    status: str
    reason: Optional[str] = None
    candidates: tuple[ServerEndpoint, ...] = ()


def normalize_service_name(name: str) -> str:
    """Fold a service name for the second, looser resolution step.

    Lower-cases, and treats any run of ``-`` and ``_`` as one ``-``, so
    ``Orders_Service`` and ``orders-service`` compare equal.  Nothing else is
    stripped: ``orders`` never equals ``orders-service``.
    """
    return _SEPARATORS_RE.sub("-", name.strip().lower())


def normalize_route(route: str) -> str:
    """Return a route in the form two routes are compared in.

    * Everything from the first ``?`` or ``#`` is dropped.
    * Every ``{...}`` path variable, whatever it is called or whether it has a
      regular expression, becomes ``{}``, which matches only another ``{}``
      in the same position.  Braces nest, so ``{id:[0-9]{2}}`` is one
      variable.
    * Repeated slashes collapse, a leading slash is added, and a trailing one
      is removed (the root stays ``/``).
    * Case is kept: ``/Users`` and ``/users`` are different routes.
    """
    for mark in ("?", "#"):
        cut = route.find(mark)
        if cut != -1:
            route = route[:cut]
    out: list[str] = []
    depth = 0
    for char in route:
        if char == "{":
            if depth == 0:
                out.append("{}")
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
            else:
                out.append(char)
        elif depth == 0:
            out.append(char)
    # An unbalanced ``{`` swallowed the rest of the route; compare it as given
    # rather than as a shorter route that could match something else.
    text = "".join(out) if depth == 0 else route
    text = _SLASHES_RE.sub("/", text)
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    return text


def unmatchable_reason(call: ClientCall) -> Optional[str]:
    """Return why ``call`` cannot be joined to a server endpoint, or ``None``.

    Checked in this order, so each call has one reason:
    ``service_unresolved``, ``url_from_argument``, ``url_literal``,
    ``route_dynamic``, ``placeholder_in_route``.
    """
    if call.service_unresolved or not call.service:
        return "service_unresolved"
    if call.url_from_argument:
        return "url_from_argument"
    if call.url_literal:
        return "url_literal"
    if call.route_dynamic:
        return "route_dynamic"
    if "${" in call.route or _UNRESOLVED_MARKER in call.route:
        return "placeholder_in_route"
    return None


def resolve_service(
    service: str,
    lookup: Callable[[str], Sequence[int]],
    normalized: Mapping[str, Iterable[str]],
) -> TargetResolution:
    """Resolve a service name to registry positions.

    Args:
        service: The ``@FeignClient`` name.
        lookup: ``name -> positions``; normally ``RepoNameResolver.lookup``,
            which tries a registry alias, a folder name and the alias file.
        normalized: Maps :func:`normalize_service_name` of every known name
            (registry alias, folder name, alias-file name) to the original
            names that fold to it.

    The exact step may select several positions (sibling checkouts that share
    a folder name), as ``cross_repo_search_tool`` does.  The normalized step
    must end at exactly one position, so a fold that reaches two different
    repositories is reported, never picked between.
    """
    positions = tuple(lookup(service))
    if positions:
        return TargetResolution(positions, how="exact")
    reached: set[int] = set()
    for name in normalized.get(normalize_service_name(service), ()):
        reached.update(lookup(name))
    if len(reached) == 1:
        return TargetResolution(tuple(reached), how="normalized")
    if len(reached) > 1:
        return TargetResolution((), reason="service_name_ambiguous")
    return TargetResolution((), reason="service_not_registered")


def _method_matches(client_method: str, server_method: str) -> bool:
    server = server_method.upper()
    return server == _ANY_METHOD or server == client_method.upper()


def link_call(
    call: ClientCall,
    servers: Iterable[ServerEndpoint],
) -> LinkResult:
    """Classify a call that already has a target repository.

    Args:
        call: The client call.  Call :func:`unmatchable_reason` first; this
            function only compares routes and does not re-check it.
        servers: The server endpoints of the target repository (or
            repositories).  Nothing is assumed about their order.

    Returns ``matched`` for exactly one source location, ``ambiguous`` for
    several and ``no_route_match`` for none.
    """
    wanted = normalize_route(call.route)
    by_location: dict[tuple[str, str, Optional[str], Optional[int], str, str], ServerEndpoint] = {}
    for server in servers:
        if not _method_matches(call.http_method, server.http_method):
            continue
        shape = normalize_route(server.route)
        if shape != wanted:
            continue
        key = (
            server.origin, server.repo, server.file, server.line,
            server.http_method.upper(), shape,
        )
        by_location.setdefault(key, server)
    candidates = tuple(sorted(
        by_location.values(),
        key=lambda s: (s.repo, s.file or "", s.line or 0, s.http_method, s.route),
    ))
    if not candidates:
        return LinkResult(NO_ROUTE_MATCH)
    if len(candidates) == 1:
        return LinkResult(MATCHED, candidates=candidates)
    return LinkResult(AMBIGUOUS, candidates=candidates)
