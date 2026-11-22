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
5. Routes are compared case-sensitively, because servers do (``/Users`` and
   ``/users`` are different routes).  A call with no exact match whose route
   equals a server route once case is ignored is reported as
   ``case_mismatch`` instead of ``no_route_match``, with those endpoints as
   ``candidates`` and ``confidence`` ``low``.  It is a lead to check, never a
   match: a server that really is case-sensitive does not serve it.

JavaScript and TypeScript calls (``origin == "js"``) have no service name.
:func:`link_js_call` matches them by HTTP method and normalised route against
the server endpoints of every registered repository except the caller's, and
reports ``ambiguous`` when more than one location (usually more than one
repository) fits.  A call that is not a statically known route is
``unmatchable`` for one of: ``non_literal_url``, ``absolute_url``,
``method_dynamic``, ``placeholder_in_route``.  A server route that takes the
rest of the path (a Next.js ``[...path]`` route, an Express ``*``) matches
only by exact method and literal prefix, and only when no exact route fits;
that match is reported with ``confidence`` ``low``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "AMBIGUOUS",
    "CASE_MISMATCH",
    "CASE_MISMATCH_REASON",
    "MATCHED",
    "NO_ROUTE_MATCH",
    "NO_TARGET_REPO",
    "STATUSES",
    "UNMATCHABLE",
    "ClientCall",
    "LinkResult",
    "ServerEndpoint",
    "ServerIndex",
    "TargetResolution",
    "link_call",
    "link_js_call",
    "normalize_route",
    "normalize_service_name",
    "resolve_service",
    "unmatchable_reason",
]

MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NO_TARGET_REPO = "no_target_repo"
NO_ROUTE_MATCH = "no_route_match"
CASE_MISMATCH = "case_mismatch"
UNMATCHABLE = "unmatchable"

#: Every status a call can have, in the order a summary lists them.
STATUSES: tuple[str, ...] = (
    MATCHED, AMBIGUOUS, CASE_MISMATCH, NO_ROUTE_MATCH, NO_TARGET_REPO, UNMATCHABLE,
)

#: The reason a ``case_mismatch`` result carries.
CASE_MISMATCH_REASON = "route_differs_by_case"

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
    #: ``"feign"`` (a Spring client method) or ``"js"`` (a JavaScript/TypeScript call).
    origin: str = "feign"
    #: For ``origin == "js"``: ``axios``, ``fetch``, ``jquery``, ``ember-ajax`` or ``ember-data``.
    client_kind: Optional[str] = None
    method_dynamic: bool = False
    absolute_url: bool = False
    #: Why the call is only a likely match (for example ``ember_data_convention``).
    notes: tuple[str, ...] = ()


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
    #: The route ends in a rest-of-path wildcard (``/api/proxy/{...path}``).
    catch_all: bool = False
    #: The wildcard may also match nothing (``[[...path]]``).
    optional_catch_all: bool = False


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
    #: ``"low"`` when the match rests on a wildcard server route.
    confidence: str = "high"


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


def _fold_route(route: str) -> str:
    """:func:`normalize_route` with case ignored, for ``case_mismatch`` leads."""
    return normalize_route(route).lower()


def unmatchable_reason(call: ClientCall) -> Optional[str]:
    """Return why ``call`` cannot be joined to a server endpoint, or ``None``.

    Checked in this order, so each call has one reason:
    ``service_unresolved``, ``url_from_argument``, ``url_literal``,
    ``route_dynamic``, ``placeholder_in_route``.  A JavaScript call has its
    own list: ``non_literal_url``, ``absolute_url``, ``method_dynamic``,
    ``placeholder_in_route``.
    """
    if call.origin == "js":
        if call.route_dynamic:
            return "non_literal_url"
        if call.absolute_url:
            return "absolute_url"
        if call.method_dynamic:
            return "method_dynamic"
        if "${" in call.route or _UNRESOLVED_MARKER in call.route:
            return "placeholder_in_route"
        return None
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
        if server.catch_all or not _method_matches(call.http_method, server.http_method):
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
        return _case_lead(call, servers)
    if len(candidates) == 1:
        return LinkResult(MATCHED, candidates=candidates)
    return LinkResult(AMBIGUOUS, candidates=candidates)


def _case_lead(call: ClientCall, servers: Iterable[ServerEndpoint]) -> LinkResult:
    """``case_mismatch`` when a server route equals the call's apart from case."""
    wanted = _fold_route(call.route)
    found: dict[object, ServerEndpoint] = {}
    for server in servers:
        if server.catch_all or not _method_matches(call.http_method, server.http_method):
            continue
        if _fold_route(server.route) == wanted:
            found.setdefault(_location_key(server, normalize_route(server.route)), server)
    if not found:
        return LinkResult(NO_ROUTE_MATCH)
    return LinkResult(
        CASE_MISMATCH, reason=CASE_MISMATCH_REASON,
        candidates=_sorted_candidates(found), confidence="low",
    )


def _location_key(
    server: ServerEndpoint, shape: str,
) -> tuple[str, str, Optional[str], Optional[int], str, str]:
    return (
        server.origin, server.repo, server.file, server.line,
        server.http_method.upper(), shape,
    )


def _sorted_candidates(found: Mapping[object, ServerEndpoint]) -> tuple[ServerEndpoint, ...]:
    return tuple(sorted(
        found.values(),
        key=lambda s: (s.repo, s.file or "", s.line or 0, s.http_method, s.route),
    ))


def _catch_all_prefix(route: str) -> str:
    """The literal part of a wildcard route, normalised: ``/a/{...p}`` -> ``/a``."""
    cut = route.find("{...")
    head = route if cut == -1 else route[:cut]
    star = head.find("*")
    if star != -1:
        head = head[:star]
    return normalize_route(head)


class ServerIndex:
    """Server endpoints indexed by normalised route, for many lookups.

    Building it is one pass; :func:`link_js_call` then answers each call
    without scanning every endpoint.  Wildcard (catch-all) routes are kept
    apart: they are consulted only when no exact route fits.
    """

    def __init__(self, servers: Iterable[ServerEndpoint]) -> None:
        self.exact: dict[str, list[ServerEndpoint]] = {}
        self.catch_all: list[tuple[str, ServerEndpoint]] = []
        #: The same exact routes, keyed with case ignored, for ``case_mismatch`` leads.
        self.folded: dict[str, list[ServerEndpoint]] = {}
        for server in servers:
            if server.catch_all:
                prefix = _catch_all_prefix(server.route)
                # A root wildcard would match every route; it matches none.
                if prefix != "/":
                    self.catch_all.append((prefix, server))
                continue
            shape = normalize_route(server.route)
            self.exact.setdefault(shape, []).append(server)
            self.folded.setdefault(shape.lower(), []).append(server)


def link_js_call(call: ClientCall, index: ServerIndex) -> LinkResult:
    """Classify a JavaScript/TypeScript call against every server endpoint.

    Exact routes win: a wildcard route is considered only when no exact route
    of the right method fits, must have the same HTTP method (a server that
    accepts every method does not count), a literal prefix that is a whole
    leading path segment run of the call, and at least one more segment
    unless the wildcard is optional.  Such a match has ``confidence`` ``low``.
    When nothing fits, a server route that differs only in letter case gives
    ``case_mismatch`` (see the module notes) rather than ``no_route_match``.
    Call :func:`unmatchable_reason` first.
    """
    wanted = normalize_route(call.route)
    found: dict[object, ServerEndpoint] = {}
    for server in index.exact.get(wanted, ()):
        if _method_matches(call.http_method, server.http_method):
            found.setdefault(_location_key(server, wanted), server)
    if found:
        candidates = _sorted_candidates(found)
        return LinkResult(MATCHED if len(candidates) == 1 else AMBIGUOUS, candidates=candidates)
    method = call.http_method.upper()
    for prefix, server in index.catch_all:
        if server.http_method.upper() != method:
            continue
        below = wanted.startswith(prefix + "/")
        if below or (server.optional_catch_all and wanted == prefix):
            found.setdefault(_location_key(server, prefix + "/**"), server)
    if not found:
        for server in index.folded.get(wanted.lower(), ()):
            if _method_matches(call.http_method, server.http_method):
                found.setdefault(_location_key(server, normalize_route(server.route)), server)
        if not found:
            return LinkResult(NO_ROUTE_MATCH)
        return LinkResult(
            CASE_MISMATCH, reason=CASE_MISMATCH_REASON,
            candidates=_sorted_candidates(found), confidence="low",
        )
    candidates = _sorted_candidates(found)
    return LinkResult(
        MATCHED if len(candidates) == 1 else AMBIGUOUS,
        reason="catch_all_route", candidates=candidates, confidence="low",
    )
