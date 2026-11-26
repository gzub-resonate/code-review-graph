"""List mode of the AMQP join: every exchange with who publishes to it and who listens.

Query-time only.  It reuses :mod:`amqp_join` (the edge shapes and
:func:`~amqp_join.read_channel_edges`) and adds the per-exchange roll-up, so a graph is read
once per exchange it carries and nothing is stored.
"""

from __future__ import annotations

from typing import Any

from . import amqp_join as aj
from .graph import GraphStore

__all__ = [
    "LIST_STATUSES", "MATCHED", "UNLISTENED", "UNPUBLISHED", "Exchange", "exchange_status",
    "list_exchange_names", "new_exchange", "add_edges", "render",
]

UNLISTENED = "unlistened"
UNPUBLISHED = "unpublished"
MATCHED = "matched"
#: Status of an exchange: publishers and no listener, listeners and no publisher, or both.
LIST_STATUSES = (UNLISTENED, UNPUBLISHED, MATCHED)

#: Names listed per exchange in a row (the rest is counted in ``*_total``).
MAX_LISTED = 20

Exchange = dict[str, Any]


def exchange_status(publishers: int, listeners: int) -> str:
    """``unlistened``, ``unpublished`` or ``matched`` from the two counts.

    Exchange-level only: it never checks that a publisher's key reaches a listener's binding.
    """
    if publishers and not listeners:
        return UNLISTENED
    if listeners and not publishers:
        return UNPUBLISHED
    return MATCHED


def list_exchange_names(store: GraphStore) -> list[str]:
    """Every exchange name (without the ``external:rabbitmq:`` prefix) with a role edge."""
    rows = store._conn.execute(
        "SELECT DISTINCT target_qualified FROM edges "
        "WHERE target_qualified >= ? AND target_qualified < ? "
        "AND kind IN ('PUBLISHES', 'PRODUCES', 'CONSUMES', 'HANDLES')",
        (aj.EXCHANGE_PREFIX, aj.EXCHANGE_PREFIX[:-1] + ";"),
    ).fetchall()
    cut = len(aj.EXCHANGE_PREFIX)
    return sorted(str(r[0])[cut:] for r in rows if len(str(r[0])) > cut)


def new_exchange() -> Exchange:
    return {
        "publishers": 0, "listeners": 0,
        "publisher_repos": set(), "listener_repos": set(),
        "routing_keys": set(), "binding_keys": set(),
    }


def add_edges(entry: Exchange, repo: str, edges: list[dict[str, Any]]) -> None:
    """Fold one repo's edges of one exchange into *entry* (values must be sanitised by caller)."""
    for edge in edges:
        if edge["role"] == "publisher":
            entry["publishers"] += 1
            entry["publisher_repos"].add(repo)
            key = edge.get("routing_key")
            if isinstance(key, str) and key:
                entry["routing_keys"].add(key)
        else:
            entry["listeners"] += 1
            entry["listener_repos"].add(repo)
            key = edge.get("binding_key")
            if isinstance(key, str) and key:
                entry["binding_keys"].add(key)


def has_key_containing(entry: Exchange, needle: str) -> bool:
    """Whether any routing key or binding key seen on the exchange contains *needle*."""
    return any(needle in k for k in entry["routing_keys"] | entry["binding_keys"])


def render(name: str, entry: Exchange) -> dict[str, Any]:
    """The row of one exchange; long name lists are cut at ``MAX_LISTED`` with a ``*_total``."""
    row: dict[str, Any] = {
        "exchange": name,
        "status": exchange_status(entry["publishers"], entry["listeners"]),
        "publisher_count": entry["publishers"],
        "listener_count": entry["listeners"],
    }
    for field, source in (
        ("publisher_repos", "publisher_repos"), ("listener_repos", "listener_repos"),
        ("routing_keys", "routing_keys"), ("binding_keys", "binding_keys"),
    ):
        values = sorted(entry[source])
        row[field] = values[:MAX_LISTED]
        if len(values) > MAX_LISTED:
            row[field + "_total"] = len(values)
    return row
