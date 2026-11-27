"""Bounded caches for pure route functions used while answering a query, and their hit counts.

``feign_links.normalize_route`` and friends are called millions of times in one cross-repo join
on a few thousand distinct strings. They are pure (a string in, a string, tuple or bool out) so
their results can be shared, and the results are immutable so a caller cannot change one for the
next call.

They are wrapped here, not decorated in ``feign_links``, because that module is part of what a
graph build runs: editing it would change the index fingerprint and force a reindex for a
query-side speed-up. Query-side modules call these wrappers; the build keeps the originals.

:func:`snapshot` and :func:`delta` read ``cache_info()`` of every registered cache (these and the
``lru_cache`` helpers other modules register) so the tool timing log can say how much a call
leaned on them. The counters are process-wide: calls that overlap in time see each other's hits.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Any, TypeVar

from . import feign_links as fl

__all__ = [
    "delta", "format_delta", "method_matches", "normalize_route", "register", "segments",
    "snapshot",
]

F = TypeVar("F", bound=Callable[..., Any])

_LOCK = threading.Lock()
_REGISTRY: dict[str, Any] = {}

# ``lru_cache`` helpers that already exist elsewhere: (module, attribute). They are registered on
# the first snapshot, so importing this module does not import them.
_EXISTING = (
    ("code_review_graph.refactor", "_path_segments"),
    ("code_review_graph.amqp_join", "_words"),
)
_existing_done = False


def register(name: str, cached: Any) -> Any:
    """Add *cached* (anything with ``cache_info()``) to the timing log under *name*."""
    with _LOCK:
        _REGISTRY[name] = cached
    return cached


def cache(name: str, maxsize: int) -> Callable[[F], F]:
    """``lru_cache(maxsize)`` that also registers the function for the timing log."""

    def wrap(fn: F) -> F:
        return register(name, lru_cache(maxsize=maxsize)(fn))  # type: ignore[no-any-return]

    return wrap


normalize_route = cache("normalize_route", 65536)(fl.normalize_route)
segments = cache("segments", 65536)(fl._segments)
method_matches = cache("method_matches", 1024)(fl._method_matches)


def _register_existing() -> None:
    global _existing_done
    if _existing_done:
        return
    _existing_done = True
    for module, attribute in _EXISTING:
        try:
            found = getattr(importlib.import_module(module), attribute)
        except (ImportError, AttributeError):
            continue
        if hasattr(found, "cache_info"):
            register(f"{module.rsplit('.', 1)[-1]}.{attribute.lstrip('_')}", found)


def snapshot() -> dict[str, tuple[int, int, int]]:
    """``{name: (hits, misses, size)}`` for every registered cache."""
    _register_existing()
    with _LOCK:
        caches = dict(_REGISTRY)
    out: dict[str, tuple[int, int, int]] = {}
    for name, found in caches.items():
        info = found.cache_info()
        out[name] = (info.hits, info.misses, info.currsize)
    return out


def delta(
    before: dict[str, tuple[int, int, int]], after: dict[str, tuple[int, int, int]],
) -> dict[str, tuple[int, int, int]]:
    """Per cache ``(hits, misses, size)``: the hits and misses between two snapshots, and the
    size at the end. A cache nobody used between them is left out."""
    out: dict[str, tuple[int, int, int]] = {}
    for name, (hits, misses, size) in after.items():
        was_hits, was_misses, _ = before.get(name, (0, 0, 0))
        used = (hits - was_hits, misses - was_misses)
        if used != (0, 0):
            out[name] = (used[0], used[1], size)
    return out


def format_delta(used: dict[str, tuple[int, int, int]]) -> str:
    """``name:hits/misses/size`` joined by commas, ``-`` when no cache was used."""
    if not used:
        return "-"
    return ",".join(f"{name}:{h}/{m}/{s}" for name, (h, m, s) in sorted(used.items()))
