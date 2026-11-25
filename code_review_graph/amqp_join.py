"""Relate AMQP publishers and listeners of one exchange, at query time.

A publisher edge and a listener edge meet on ``external:rabbitmq:<exchange>``. The publisher
edge carries ``routing_key``; the listener edge carries ``binding_key`` (which may hold the
AMQP topic wildcards ``*`` and ``#``) and, when known, ``queue``. This module holds:

* :func:`topic_matches`, the pure AMQP topic matcher (words split on ``.``; ``*`` is exactly
  one word, ``#`` is zero or more words);
* :func:`link_kind`, which says how a listener's binding key and a publisher's routing key
  relate, so a pattern match is never read as an exact one;
* :func:`read_channel_edges` and :func:`has_amqp_edges`, read-only reads of one graph used by
  ``query_graph`` (``listeners_of`` and ``publishers_of``), by the zero-result note and by
  ``cross_repo_requests`` with ``origin="amqp"``.

The exchange type (direct, topic, fanout) is usually not declared next to a listener, so it is
never inferred. Pattern matching is applied only when a binding key really contains a wildcard
word; any other key is compared exactly. Nothing is stored and nothing is written.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from .graph import GraphStore

__all__ = [
    "EXCHANGE_PREFIX", "MATCH_EXACT", "MATCH_PATTERN", "MATCH_UNKNOWN",
    "has_amqp_edges", "has_wildcard", "link_kind", "read_channel_edges", "topic_matches",
]

EXCHANGE_PREFIX = "external:rabbitmq:"
#: ``matched_via`` of a row whose key equals the one asked for.
MATCH_EXACT = "exact_key"
#: ``matched_via`` of a row that matched only through an AMQP wildcard in a binding key.
MATCH_PATTERN = "topic_pattern"
#: ``matched_via`` of a row whose edge carries no key, so the filter could not exclude it.
MATCH_UNKNOWN = "key_unknown"

#: Keys longer than this, or with more words, are compared as plain text (never as patterns),
#: which keeps the matcher bounded on adversarial input.
_MAX_KEY_CHARS = 512
_MAX_WORDS = 64


def has_wildcard(key: str) -> bool:
    """Whether *key* has an AMQP wildcard word (``*`` or ``#`` standing alone between dots)."""
    return isinstance(key, str) and any(word in ("*", "#") for word in key.split("."))


@lru_cache(maxsize=1024)
def _words(key: str) -> tuple[str, ...] | None:
    if len(key) > _MAX_KEY_CHARS:
        return None
    words = tuple(key.split("."))
    return words if len(words) <= _MAX_WORDS else None


def topic_matches(binding_key: str, routing_key: str) -> bool:
    """AMQP topic semantics: does *routing_key* match the pattern *binding_key*?

    Words are split on ``.``. ``*`` matches exactly one word and ``#`` zero or more. A key
    without a wildcard word matches only itself. A key too long to read safely only matches
    by plain equality.
    """
    if not isinstance(binding_key, str) or not isinstance(routing_key, str):
        return False
    pattern, subject = _words(binding_key), _words(routing_key)
    if pattern is None or subject is None:
        return binding_key == routing_key
    # reachable[j]: the first i pattern words can consume the first j subject words.
    reachable = [False] * (len(subject) + 1)
    reachable[0] = True
    for word in pattern:
        nxt = [False] * (len(subject) + 1)
        if word == "#":
            seen = False
            for j in range(len(subject) + 1):
                seen = seen or reachable[j]
                nxt[j] = seen
        else:
            for j in range(1, len(subject) + 1):
                if reachable[j - 1] and (word == "*" or word == subject[j - 1]):
                    nxt[j] = True
        reachable = nxt
    return reachable[len(subject)]


def link_kind(binding_key: Any, routing_key: Any) -> str | None:
    """How a listener's *binding_key* relates to a publisher's *routing_key*.

    ``None`` when they do not match; ``MATCH_EXACT`` for equal keys; ``MATCH_PATTERN`` when
    the binding key has a wildcard word and matches only through it; ``MATCH_UNKNOWN`` when
    either side has no key (the edge cannot be excluded, and cannot be confirmed either).
    """
    if not isinstance(binding_key, str) or not binding_key \
            or not isinstance(routing_key, str) or not routing_key:
        return MATCH_UNKNOWN
    if binding_key == routing_key:
        return MATCH_EXACT
    if has_wildcard(binding_key) and topic_matches(binding_key, routing_key):
        return MATCH_PATTERN
    return None


# A publisher edge to an exchange is ``PUBLISHES`` from the Spring event parser and ``PRODUCES``
# from the messaging parser (``convertAndSend``, ``basic_publish``); both mean "publishes here".
PUBLISH_KINDS = ("PUBLISHES", "PRODUCES")


def has_amqp_edges(store: GraphStore) -> bool:
    """Whether the graph has any edge that ends at an AMQP exchange or queue."""
    row = store._conn.execute(
        "SELECT 1 FROM edges WHERE target_qualified >= ? AND target_qualified < ? "
        "AND kind IN ('PUBLISHES', 'PRODUCES', 'CONSUMES', 'HANDLES') LIMIT 1",
        (EXCHANGE_PREFIX, EXCHANGE_PREFIX[:-1] + ";"),
    ).fetchone()
    return row is not None


def read_channel_edges(
    store: GraphStore, exchange: str,
) -> Iterator[dict[str, Any]]:
    """Publisher and listener edges of one exchange, as plain rows.

    Each row has ``role`` (``publisher`` or ``listener``), ``source`` (qualified name),
    ``file_path``, ``line``, ``routing_key`` (publishers) or ``binding_key`` (listeners) and,
    for listeners, ``queue``. A listener that has both a ``CONSUMES`` and a ``HANDLES`` edge at
    the same place is one row. Values are the raw strings from the graph: sanitise them before
    they leave the process.
    """
    target = exchange if exchange.startswith(EXCHANGE_PREFIX) else EXCHANGE_PREFIX + exchange
    seen: set[tuple[Any, ...]] = set()
    for source, kind, file_path, line, extra in store._conn.execute(
        "SELECT source_qualified, kind, file_path, line, extra FROM edges "
        "WHERE target_qualified = ? AND kind IN ('PUBLISHES', 'PRODUCES', 'CONSUMES', 'HANDLES') "
        "ORDER BY file_path, line, id",
        (target,),
    ):
        try:
            info = json.loads(extra or "{}")
        except ValueError:
            info = {}
        if not isinstance(info, dict):
            info = {}
        role = "publisher" if kind in PUBLISH_KINDS else "listener"
        row: dict[str, Any] = {
            "role": role, "source": source, "file_path": file_path, "line": line,
        }
        if role == "publisher":
            row["routing_key"] = info.get("routing_key")
        else:
            row["binding_key"] = info.get("binding_key")
            row["queue"] = info.get("queue")
        # ``CONSUMES`` and its ``HANDLES`` twin are one listener.
        key = tuple(str(v) for v in row.values())
        if key in seen:
            continue
        seen.add(key)
        yield row
