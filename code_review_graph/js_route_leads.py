"""Leads for a JavaScript/TypeScript call that :func:`feign_links.link_js_call` left unmatched.

Query-time only.  It reads the :class:`~code_review_graph.feign_links.ServerIndex` the join
already built and never changes how a call matches: it only looks again at a call whose result
is ``no_route_match`` and, when something nearly fits, says so with a distinct status.

``method_not_served``
    The call's normalised route exists on a registered server, but only under other HTTP
    methods.  The row's candidates are those endpoints, so the methods that *are* served and
    the serving repo and file are visible.  Many Ember Data calls are inferred for all five
    CRUD verbs of a model, so this is often a call the client never makes.

``prefix_mismatch``
    No route of the right method fits, but one has the same last two path segments under a
    different prefix (client ``/v1/admin/projects/{id}`` against server
    ``/admin/projects/{id}``).  A lead, never a match: ``confidence`` is ``low`` and nothing
    is claimed beyond the shared tail.

Anything else stays ``no_route_match``.
"""

from __future__ import annotations

from typing import Optional
from weakref import WeakKeyDictionary

from . import feign_links as fl

__all__ = [
    "ALL_STATUSES", "METHOD_NOT_SERVED", "METHOD_NOT_SERVED_REASON", "PREFIX_MISMATCH",
    "PREFIX_MISMATCH_REASON", "refine_no_route_match", "served_methods",
]

METHOD_NOT_SERVED = "method_not_served"
PREFIX_MISMATCH = "prefix_mismatch"
METHOD_NOT_SERVED_REASON = "route_served_with_other_method"
PREFIX_MISMATCH_REASON = "same_last_two_segments_under_other_prefix"

#: Every status a call can have: the join's own, with the two leads placed before
#: ``no_route_match`` so a summary lists them in order of strength.
ALL_STATUSES: tuple[str, ...] = tuple(
    status
    for base in fl.STATUSES
    for status in (
        (METHOD_NOT_SERVED, PREFIX_MISMATCH, base) if base == fl.NO_ROUTE_MATCH else (base,)
    )
)

#: More distinct server routes than this share the call's last two segments: too open to report.
_MAX_PREFIX_ROUTES = 10

_Tail = tuple[str, str]
_ByTail = dict[_Tail, list[tuple[tuple[str, ...], fl.ServerEndpoint]]]
_TAILS: "WeakKeyDictionary[fl.ServerIndex, _ByTail]" = WeakKeyDictionary()


def _tails(index: fl.ServerIndex) -> _ByTail:
    """Exact server routes of two or more segments, keyed by their last two segments."""
    built = _TAILS.get(index)
    if built is None:
        built = {}
        for group in index.by_length.values():
            for segments, server in group:
                if len(segments) >= 2:
                    built.setdefault((segments[-2], segments[-1]), []).append((segments, server))
        _TAILS[index] = built
    return built


def served_methods(candidates: tuple[fl.ServerEndpoint, ...]) -> list[str]:
    """The distinct HTTP methods of *candidates*, sorted."""
    return sorted({c.http_method.upper() for c in candidates})


def _other_method(call: fl.ClientCall, index: fl.ServerIndex) -> Optional[fl.LinkResult]:
    wanted = fl.normalize_route(call.route)
    found: dict[object, fl.ServerEndpoint] = {}
    for server in index.exact.get(wanted, ()):
        found.setdefault(fl._location_key(server, wanted), server)
    if not found:
        return None
    return fl.LinkResult(
        METHOD_NOT_SERVED, METHOD_NOT_SERVED_REASON, fl._sorted_candidates(found),
    )


def _other_prefix(call: fl.ClientCall, index: fl.ServerIndex) -> Optional[fl.LinkResult]:
    wanted = fl._segments(fl.normalize_route(call.route))
    if len(wanted) < 2 or all(segment == "{}" for segment in wanted[-2:]):
        return None
    found: dict[object, fl.ServerEndpoint] = {}
    shapes: set[tuple[str, ...]] = set()
    for segments, server in _tails(index).get((wanted[-2], wanted[-1]), ()):
        if segments == wanted or not fl._method_matches(call.http_method, server.http_method):
            continue
        found.setdefault(fl._location_key(server, "/" + "/".join(segments)), server)
        shapes.add(segments)
    if not found or len(shapes) > _MAX_PREFIX_ROUTES:
        return None
    return fl.LinkResult(
        PREFIX_MISMATCH, PREFIX_MISMATCH_REASON, fl._sorted_candidates(found), "low",
    )


def refine_no_route_match(
    call: fl.ClientCall,
    result: fl.LinkResult,
    index: fl.ServerIndex,
    own: Optional[fl.ServerIndex] = None,
) -> fl.LinkResult:
    """*result* unchanged unless it is ``no_route_match`` and a lead exists.

    As in the join, the caller's own repository (*own*) is consulted before *index*, and a
    method lead anywhere beats a prefix lead.
    """
    if result.status != fl.NO_ROUTE_MATCH:
        return result
    pools = [pool for pool in (own, index) if pool is not None]
    for finder in (_other_method, _other_prefix):
        for pool in pools:
            lead = finder(call, pool)
            if lead is not None:
                return lead
    return result
