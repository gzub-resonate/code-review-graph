"""Cursor paging for tool results that a ceiling would otherwise cut off.

A list tool returns at most ``min(limit, ceiling)`` rows. Without paging the rest of a
long list can never be reached: raising ``limit`` past the ceiling changes nothing. With
paging, a capped answer carries ``next_cursor``; passing it back as ``cursor`` returns the
next rows, so the ceiling bounds the size of one reply, not what a caller can read.

The shape follows the cursor pagination the MCP specification defines for its list
operations (``cursor`` in, ``nextCursor`` out): a cursor is an opaque string, the server
chooses what a page holds, a missing next cursor means the end, and a cursor that does not
fit the request is an error. This repository spells result keys in snake_case, so the key
is ``next_cursor``.

Cursors hold no server state. One records the position, a digest of the query it belongs
to, and a stamp of the graph it was made from. A cursor from a different query, or from a
graph that has been rebuilt since, is refused instead of silently skipping or repeating
rows. Cursors are opaque, not secret: they are not signed, and one edited by hand can
only move within the same list.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "CursorError",
    "Page",
    "graph_stamp",
    "make_scope",
    "paginate",
    "shown_range",
]

_VERSION = 1
#: Longest cursor accepted, so a hostile value is rejected before it is decoded.
_MAX_CURSOR_CHARS = 512
_RESTART = "call again without a cursor to start over"


class CursorError(ValueError):
    """A cursor that is malformed, or does not belong to this query or graph."""


@dataclass(frozen=True)
class Page:
    """One page of a list: its rows, where it starts and how to reach the next."""

    items: list[Any]
    #: Rows in the whole list, not in this page.
    total: int
    #: Index of the first row of this page in the whole list.
    offset: int
    #: Opaque cursor for the next page, ``None`` on the last one.
    next_cursor: Optional[str]

    @property
    def truncated(self) -> bool:
        """Whether rows remain after this page (the same as having a next cursor)."""
        return self.next_cursor is not None

    def fields(self) -> dict[str, Any]:
        """The keys paging adds: ``next_cursor`` if more exists, ``offset`` after page 1."""
        extra: dict[str, Any] = {}
        if self.offset:
            extra["offset"] = self.offset
        if self.next_cursor is not None:
            extra["next_cursor"] = self.next_cursor
        return extra


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def make_scope(tool: str, **query: Any) -> str:
    """Digest of the arguments that decide *which* rows a list holds.

    Pass the tool name and every argument that changes the rows or their order, such as
    a sort column or a filter. Leave out the page size and the output detail level: a
    caller may change those between pages.
    """
    return _digest(json.dumps([tool, query], sort_keys=True, default=str, separators=(",", ":")))


def graph_stamp(store: Any, *extra: Any) -> str:
    """Digest of what the graph held when a cursor was made.

    It combines the last build time and the node and edge counts, so a rebuild or an
    incremental update invalidates cursors made before it. Add *extra* values for data a
    tool reads that those do not cover.
    """
    conn = store._conn
    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return _digest(json.dumps(
        [store.get_metadata("last_updated"), nodes, edges, *extra],
        default=str, separators=(",", ":"),
    ))


def _encode(offset: int, scope: str, stamp: str) -> str:
    payload = json.dumps({"v": _VERSION, "o": offset, "s": scope, "g": stamp},
                         separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def _decode(cursor: str, scope: str, stamp: str) -> int:
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorError(f"cursor is not valid; {_RESTART}") from exc
    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    offset = payload.get("o")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    if payload.get("s") != scope:
        raise CursorError(
            "cursor was made for a different query (other arguments or another tool); "
            + _RESTART
        )
    if payload.get("g") != stamp:
        raise CursorError("the graph changed since this cursor was made; " + _RESTART)
    return offset


def paginate(
    items: list[Any],
    limit: int,
    hard_cap: int,
    cursor: Optional[str] = None,
    *,
    scope: str,
    stamp: str,
) -> Page:
    """The page of *items* a cursor points at, at most ``min(limit, hard_cap)`` rows long.

    Without a cursor this is the first page, so a caller that never pages sees what
    ``_bounded`` returned, plus ``next_cursor`` when the list was cut. *items* must be in
    a fixed order for the whole list: the page is a slice of it.
    """
    total = len(items)
    offset = _decode(cursor, scope, stamp) if cursor else 0
    if offset > total:
        raise CursorError(f"cursor points past the end of the list; {_RESTART}")
    size = min(limit, hard_cap)
    end = offset + size
    more = end < total
    return Page(
        items=list(items[offset:end]),
        total=total,
        offset=offset,
        next_cursor=_encode(end, scope, stamp) if more else None,
    )


def shown_range(page: Page) -> str:
    """The summary fragment for a capped list, empty for a whole one.

    The first page reads ``", showing N of M"``, as the other capped summaries do; a later
    page names its rows, ``", showing 51-100 of 130"``.
    """
    shown = len(page.items)
    if page.offset == 0:
        return f", showing {shown} of {page.total}" if shown < page.total else ""
    if shown == 0:
        return f", none shown (offset {page.offset} of {page.total})"
    return f", showing {page.offset + 1}-{page.offset + shown} of {page.total}"
