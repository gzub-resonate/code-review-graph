"""Lookups over a :class:`feign_links.ServerIndex` that do not scan every endpoint.

Two things made an all-repo JavaScript join cost time in proportion to (calls x endpoints):

* ``_variable_hits`` read every server route of the call's segment count for each call that no
  exact route fitted. :func:`variable_pool` answers the same question from an inverted index
  (segment count, position, fixed segment) -> routes, built once per index on first use, so it
  reads only the routes that share the call's rarest fixed segment.
* The pool of "every other repo" was rebuilt for each client repo. :class:`ExcludingIndex` is a
  view of one index over all repos that leaves one repo's endpoints out, so one index serves
  every client repo and gives the same answers as an index built without that repo.

:func:`link_js_call` and :func:`refine_no_route_match` are those two joins (same rules, same
order) over such an index. They live here, not in ``feign_links`` / ``js_route_leads``, because
those modules are part of what a graph build runs and editing them would change the index
fingerprint and force a reindex for a query-side speed-up. Tests compare them with the originals.

Nothing here changes what a call matches. It is query-time only and not imported by the build.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Optional
from weakref import WeakKeyDictionary

from . import feign_links as fl
from . import js_route_leads as jl
from . import query_cache as qc

__all__ = ["ExcludingIndex", "link_js_call", "refine_no_route_match", "variable_pool"]

_Entry = tuple[tuple[str, ...], fl.ServerEndpoint]

_LOCK = threading.Lock()
_TAIL_TABLES: "WeakKeyDictionary[fl.ServerIndex, dict[tuple[str, str], list[_Entry]]]" = (
    WeakKeyDictionary()
)
_POSITIONS: "WeakKeyDictionary[fl.ServerIndex, dict[tuple[int, int, str], list[_Entry]]]" = (
    WeakKeyDictionary()
)


def _keep(server: fl.ServerEndpoint, excluded: Optional[str]) -> bool:
    return excluded is None or server.origin != excluded


class _FilteredMap(Mapping):
    """A read-only ``route -> endpoints`` mapping with one origin left out.

    It answers like a mapping built from the other repos only: a key whose endpoints all belong to
    the excluded origin is absent, so ``get``, ``in``, iteration, ``keys``, ``values`` and ``items``
    all agree with an index that never held that repo.
    """

    def __init__(self, base: dict[Any, list[Any]], excluded: str, pair: bool) -> None:
        self._base = base
        self._excluded = excluded
        self._pair = pair

    def _origin(self, item: Any) -> Optional[str]:
        return item[1].origin if self._pair else item.origin

    def _kept(self, found: list[Any]) -> list[Any]:
        if all(self._origin(item) != self._excluded for item in found):
            return found
        return [item for item in found if self._origin(item) != self._excluded]

    def __getitem__(self, key: Any) -> list[Any]:
        found = self._base.get(key)
        if found is None:
            raise KeyError(key)
        kept = self._kept(found)
        if not kept:
            raise KeyError(key)
        return kept

    def __iter__(self) -> Iterator[Any]:
        for key, found in self._base.items():
            if self._kept(found):
                yield key

    def __len__(self) -> int:
        return sum(1 for _ in self)


class ExcludingIndex(fl.ServerIndex):
    """*base* without the endpoints of the repo at path *excluded*.

    A drop-in for a :class:`~feign_links.ServerIndex` built from every other repo: its maps are
    read-only mappings that answer ``get``, ``in`` and iteration as that index would, and
    ``catch_all`` is the filtered list.
    """

    def __init__(self, base: fl.ServerIndex, excluded: str) -> None:
        self.base = base
        self.excluded = excluded
        self.exact = _FilteredMap(base.exact, excluded, False)  # type: ignore[assignment]
        self.folded = _FilteredMap(base.folded, excluded, False)  # type: ignore[assignment]
        self.by_length = _FilteredMap(base.by_length, excluded, True)  # type: ignore[assignment]
        self.catch_all = [(p, s) for p, s in base.catch_all if _keep(s, excluded)]


def _positions(index: fl.ServerIndex) -> dict[tuple[int, int, str], list[_Entry]]:
    with _LOCK:
        built = _POSITIONS.get(index)
    if built is not None:
        return built
    built = {}
    for length, group in index.by_length.items():
        for entry in group:
            for position, segment in enumerate(entry[0]):
                built.setdefault((length, position, segment), []).append(entry)
    with _LOCK:
        _POSITIONS[index] = built
    return built


def variable_pool(index: fl.ServerIndex, wanted: tuple[str, ...]) -> Iterable[_Entry]:
    """The ``(segments, server)`` entries that can fit *wanted*: those that share its rarest fixed
    segment at its position. The caller still tests every segment; this only narrows the scan.
    A call with no fixed segment gets the whole group of its length, as before."""
    base = index.base if isinstance(index, ExcludingIndex) else index
    excluded = index.excluded if isinstance(index, ExcludingIndex) else None
    fixed = [(i, seg) for i, seg in enumerate(wanted) if seg != "{}"]
    if not fixed:
        return index.by_length.get(len(wanted), ())
    positions = _positions(base)
    best: Optional[list[_Entry]] = None
    for i, seg in fixed:
        rows = positions.get((len(wanted), i, seg))
        if rows is None:
            return ()
        if best is None or len(rows) < len(best):
            best = rows
    assert best is not None
    if excluded is None:
        return best
    return [entry for entry in best if _keep(entry[1], excluded)]


def _tails(index: fl.ServerIndex) -> dict[tuple[str, str], list[_Entry]]:
    """Exact routes of two or more segments by their last two segments (see ``js_route_leads``)."""
    base = index.base if isinstance(index, ExcludingIndex) else index
    with _LOCK:
        built = _TAIL_TABLES.get(base)
    if built is not None:
        return built
    built = {}
    for group in base.by_length.values():
        for segments, server in group:
            if len(segments) >= 2:
                built.setdefault((segments[-2], segments[-1]), []).append((segments, server))
    with _LOCK:
        _TAIL_TABLES[base] = built
    return built


def variable_hits(call: fl.ClientCall, index: fl.ServerIndex) -> Optional[fl.LinkResult]:
    """``feign_links._variable_hits`` reading :func:`variable_pool` instead of every route."""
    wanted = qc.segments(qc.normalize_route(call.route))
    fixed = sum(1 for segment in wanted if segment != "{}")
    if fixed < fl._MIN_FIXED_SEGMENTS or fixed == len(wanted):
        return None
    found: dict[object, fl.ServerEndpoint] = {}
    shapes: set[tuple[str, ...]] = set()
    for segments, server in variable_pool(index, wanted):
        if not qc.method_matches(call.http_method, server.http_method):
            continue
        if all(want == "{}" or want == have for want, have in zip(wanted, segments)):
            found.setdefault(fl._location_key(server, "/" + "/".join(segments)), server)
            shapes.add(segments)
    if not found or len(shapes) > fl._MAX_VARIABLE_ROUTES:
        return None
    return fl._js_result(fl._sorted_candidates(found), fl.VARIABLE_LITERAL_REASON, "low")


def link_js_call(
    call: fl.ClientCall, index: fl.ServerIndex, own: Optional[fl.ServerIndex] = None,
) -> fl.LinkResult:
    """:func:`feign_links.link_js_call` with the inverted index for the variable stage."""
    pools = [pool for pool in (own, index) if pool is not None]
    for pool in pools:
        result = fl._route_hits(call, pool)
        if result is not None:
            return result
    for pool in pools:
        lead = fl._case_hits(call, pool)
        if lead is not None:
            return lead
    for pool in pools:
        variable = variable_hits(call, pool)
        if variable is not None:
            return variable
    return fl.LinkResult(fl.NO_ROUTE_MATCH)


def _other_prefix(call: fl.ClientCall, index: fl.ServerIndex) -> Optional[fl.LinkResult]:
    wanted = qc.segments(qc.normalize_route(call.route))
    if len(wanted) < 2 or all(segment == "{}" for segment in wanted[-2:]):
        return None
    excluded = index.excluded if isinstance(index, ExcludingIndex) else None
    found: dict[object, fl.ServerEndpoint] = {}
    shapes: set[tuple[str, ...]] = set()
    for segments, server in _tails(index).get((wanted[-2], wanted[-1]), ()):
        if not _keep(server, excluded):
            continue
        if segments == wanted or not qc.method_matches(call.http_method, server.http_method):
            continue
        found.setdefault(fl._location_key(server, "/" + "/".join(segments)), server)
        shapes.add(segments)
    if not found or len(shapes) > jl._MAX_PREFIX_ROUTES:
        return None
    return fl.LinkResult(
        jl.PREFIX_MISMATCH, jl.PREFIX_MISMATCH_REASON, fl._sorted_candidates(found), "low",
    )


def refine_no_route_match(
    call: fl.ClientCall, result: fl.LinkResult, index: fl.ServerIndex,
    own: Optional[fl.ServerIndex] = None,
) -> fl.LinkResult:
    """:func:`js_route_leads.refine_no_route_match` over a shared tail table."""
    if result.status != fl.NO_ROUTE_MATCH:
        return result
    pools = [pool for pool in (own, index) if pool is not None]
    for pool in pools:
        lead = jl._other_method(call, pool)
        if lead is not None:
            return lead
    for pool in pools:
        lead = _other_prefix(call, pool)
        if lead is not None:
            return lead
    return result
