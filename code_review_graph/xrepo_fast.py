"""Query-side helpers that make the cross-repo Feign join and its cold start cheaper.

Nothing here is read by a graph build (``index_fingerprint`` does not reach it), and every
answer equals the one the plain functions give:

* :class:`ShapeIndex` and :func:`link_call_indexed` answer ``feign_links.link_call`` from
  dicts keyed by the normalised (and the case-folded) route instead of normalising every
  server route again for every call. :func:`feign_row_reason` is the same for the
  ``served_by_caller_repo`` note of ``not_found_reasons.cross_repo_row_reason``.
* :func:`prefetch_stamps` reads the graph stamps of all repos on the shared worker threads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from . import feign_links as fl

__all__ = [
    "ShapeIndex",
    "feign_row_reason",
    "link_call_indexed",
    "prefetch_stamps",
]


class ShapeIndex:
    """The non-catch-all server endpoints of one repo, keyed by normalised and folded route.

    Each bucket keeps the order of the endpoints it was built from, so a lookup visits the
    endpoints ``link_call`` would accept in the same order.
    """

    __slots__ = ("by_fold", "by_shape", "count")

    def __init__(self, servers: Iterable[fl.ServerEndpoint]) -> None:
        self.by_shape: dict[str, list[tuple[fl.ServerEndpoint, str]]] = {}
        self.by_fold: dict[str, list[tuple[fl.ServerEndpoint, str]]] = {}
        count = 0
        for server in servers:
            count += 1
            if server.catch_all:
                continue
            shape = fl.normalize_route(server.route)
            self.by_shape.setdefault(shape, []).append((server, shape))
            self.by_fold.setdefault(shape.lower(), []).append((server, shape))
        #: How many endpoints the repo has, catch-alls included (``not pool`` in the old code).
        self.count = count


def link_call_indexed(call: fl.ClientCall, pools: Sequence[ShapeIndex]) -> fl.LinkResult:
    """``feign_links.link_call(call, endpoints of every pool, in order)``, from the indexes."""
    wanted = fl.normalize_route(call.route)
    method = call.http_method
    by_location: dict[Any, fl.ServerEndpoint] = {}
    for pool in pools:
        for server, shape in pool.by_shape.get(wanted, ()):
            if not fl._method_matches(method, server.http_method):
                continue
            by_location.setdefault(fl._location_key(server, shape), server)
    if by_location:
        candidates = fl._sorted_candidates(by_location)
        if len(candidates) == 1:
            return fl.LinkResult(fl.MATCHED, candidates=candidates)
        return fl.LinkResult(fl.AMBIGUOUS, candidates=candidates)
    folded = wanted.lower()
    found: dict[Any, fl.ServerEndpoint] = {}
    for pool in pools:
        for server, shape in pool.by_fold.get(folded, ()):
            if not fl._method_matches(method, server.http_method):
                continue
            found.setdefault(fl._location_key(server, shape), server)
    if not found:
        return fl.LinkResult(fl.NO_ROUTE_MATCH)
    return fl.LinkResult(
        fl.CASE_MISMATCH, reason=fl.CASE_MISMATCH_REASON,
        candidates=fl._sorted_candidates(found), confidence="low",
    )


def feign_row_reason(
    call: fl.ClientCall, result: fl.LinkResult, own: Optional[ShapeIndex],
) -> Optional[str]:
    """``not_found_reasons.cross_repo_row_reason`` for a call that is not JavaScript."""
    if result.status != fl.NO_ROUTE_MATCH or result.reason:
        return None
    if call.route_dynamic or own is None or not own.count:
        return None
    served = link_call_indexed(call, (own,)).status in (fl.MATCHED, fl.AMBIGUOUS)
    return "served_by_caller_repo" if served else None


def prefetch_stamps(entries: Sequence[Mapping[str, Any]], workers: int) -> None:
    """Read (and keep) the graph stamp of every entry on up to *workers* threads.

    A stamp is a few ``COUNT(*)`` reads of the graph file the first time it is asked for;
    asked one repo after another that is a serial wait per repo, on a network disk a long
    one. The stamps are kept by ``repo_cache`` as before, so this changes only when they
    are read.
    """
    from .repo_cache import graph_stamp, parallel_map

    if len(entries) > 1 and workers > 1:
        parallel_map(graph_stamp, list(entries), workers)
