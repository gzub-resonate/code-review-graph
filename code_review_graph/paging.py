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
    "content_stamp",
    "make_scope",
    "paginate",
    "paginate_groups",
    "shown_range",
]

_VERSION = 1
#: Longest cursor accepted, so a hostile value is rejected before it is decoded.
_MAX_CURSOR_CHARS = 2048
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
    #: Set by :func:`paginate_groups` only: this page's rows of each group, and the index
    #: in each group where they start. ``items`` holds the same rows, flattened.
    groups: tuple[list[Any], ...] = ()
    group_offsets: tuple[int, ...] = ()

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


def content_stamp(content: Any) -> str:
    """Digest of the exact rows a list holds, for a list that is recomputed on every call.

    Use it in place of :func:`graph_stamp` when the list does not come from one graph
    database, so a cursor is only honoured while every row is still the same.
    """
    return _digest(json.dumps(content, default=str, separators=(",", ":"), sort_keys=True))


def _encode(position: Any, scope: str, stamp: str) -> str:
    payload = json.dumps({"v": _VERSION, "o": position, "s": scope, "g": stamp},
                         separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def _decode(cursor: str, scope: str, stamp: str) -> Any:
    """The position a cursor holds, once it is known to belong to this query and graph."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorError(f"cursor is not valid; {_RESTART}") from exc
    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    if payload.get("s") != scope:
        raise CursorError(
            "cursor was made for a different query (other arguments or another tool); "
            + _RESTART
        )
    if payload.get("g") != stamp:
        raise CursorError(
            "the graph changed since this cursor was made (a re-index or update "
            "invalidates every earlier cursor); " + _RESTART
        )
    return payload.get("o")


def _count(value: Any, most: int) -> int:
    """A non-negative integer no larger than *most*, else a refused cursor."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= most:
        raise CursorError(f"cursor is not valid; {_RESTART}")
    return value


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
    offset = 0
    if cursor:
        position = _decode(cursor, scope, stamp)
        if isinstance(position, int) and not isinstance(position, bool) and position > total:
            raise CursorError(f"cursor points past the end of the list; {_RESTART}")
        offset = _count(position, total)
    size = min(limit, hard_cap)
    end = offset + size
    more = end < total
    return Page(
        items=list(items[offset:end]),
        total=total,
        offset=offset,
        next_cursor=_encode(end, scope, stamp) if more else None,
    )


def paginate_groups(
    groups: list[list[Any]],
    per_group: int,
    limit: int,
    hard_cap: int,
    cursor: Optional[str] = None,
    *,
    scope: str,
    stamp: str,
) -> Page:
    """One page over several lists read in turn, each at most *per_group* rows at a time.

    A page takes up to *per_group* rows from each group, in group order, and keeps the
    first ``min(limit, hard_cap)`` of them. The cursor remembers how far each group has
    been read, so a group that gave rows to this page continues where it stopped. The
    first page holds what a plain per-group cap followed by an overall cap would.
    ``offset`` is the number of rows read before this page, across every group.
    """
    total = sum(len(group) for group in groups)
    offsets = [0] * len(groups)
    if cursor:
        position = _decode(cursor, scope, stamp)
        if not isinstance(position, dict):
            raise CursorError(f"cursor is not valid; {_RESTART}")
        for key, value in position.items():
            if not isinstance(key, str) or not key.isdigit() or int(key) >= len(groups):
                raise CursorError(f"cursor is not valid; {_RESTART}")
            offsets[int(key)] = _count(value, len(groups[int(key)]))
    size = min(limit, hard_cap)
    items: list[Any] = []
    advanced = list(offsets)
    by_group: list[list[Any]] = [[] for _ in groups]
    for index, group in enumerate(groups):
        room = size - len(items)
        if room <= 0:
            break
        taken = group[offsets[index]:offsets[index] + per_group][:room]
        by_group[index] = taken
        items.extend(taken)
        advanced[index] += len(taken)
    more = any(done < len(group) for done, group in zip(advanced, groups))
    return Page(
        items=items,
        total=total,
        offset=sum(offsets),
        next_cursor=_encode(
            {str(i): done for i, done in enumerate(advanced) if done}, scope, stamp,
        ) if more else None,
        groups=tuple(by_group),
        group_offsets=tuple(offsets),
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
