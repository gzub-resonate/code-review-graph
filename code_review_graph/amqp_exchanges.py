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
    "list_exchange_names", "new_exchange", "add_edges", "render", "in_scope_edges", "is_queue",
    "link_bound_queues", "has_role_edges",
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
        "AND kind IN ('PUBLISHES', 'PRODUCES', 'CONSUMES', 'HANDLES', 'BINDS')",
        (aj.EXCHANGE_PREFIX, aj.EXCHANGE_PREFIX[:-1] + ";"),
    ).fetchall()
    cut = len(aj.EXCHANGE_PREFIX)
    return sorted(str(r[0])[cut:] for r in rows if len(str(r[0])) > cut)


def new_exchange() -> Exchange:
    return {
        "publishers": 0, "listeners": 0,
        "publisher_repos": set(), "listener_repos": set(),
        "routing_keys": set(), "binding_keys": set(),
        # Edges that name an exchange, and edges that name only a queue.
        "exchange_edges": 0, "queue_edges": 0,
        "scope_publishers": 0, "scope_listeners": 0,
        # (repo, routing key) of each publisher; queue -> {(repo, binding key)} of each listener
        # edge that binds a queue to this exchange. Both feed :func:`link_bound_queues`.
        "publisher_keys": [], "queue_bindings": {},
        # Set on a queue by :func:`link_bound_queues`: the exchanges it is bound to.
        "bound": [], "bound_publishers": 0, "bound_publisher_repos": set(),
    }


def add_edges(
    entry: Exchange, repo: str, edges: list[dict[str, Any]], in_scope: bool = True,
) -> None:
    """Fold one repo's edges of one exchange into *entry* (values must be sanitised by caller).

    *in_scope* says the repo is one the caller selected: the totals always count every repo
    (a status is about all of them), the ``scope_*`` counts only the selected ones.
    """
    for edge in edges:
        if edge["role"] == aj.ROLE_BINDING:
            # A declaration ties a queue to this exchange; it is no publisher or listener.
            queue = edge.get("queue")
            if isinstance(queue, str) and queue:
                key = edge.get("binding_key")
                entry["queue_bindings"].setdefault(queue, set()).add(
                    (repo, key if isinstance(key, str) and key else None)
                )
            continue
        if in_scope:
            entry["scope_publishers" if edge["role"] == "publisher" else "scope_listeners"] += 1
        entry["queue_edges" if edge.get("kind") == aj.KIND_QUEUE else "exchange_edges"] += 1
        if edge["role"] == "publisher":
            entry["publishers"] += 1
            entry["publisher_repos"].add(repo)
            key = edge.get("routing_key")
            if isinstance(key, str) and key:
                entry["routing_keys"].add(key)
            entry["publisher_keys"].append((repo, key if isinstance(key, str) and key else None))
        else:
            entry["listeners"] += 1
            entry["listener_repos"].add(repo)
            key = edge.get("binding_key")
            if isinstance(key, str) and key:
                entry["binding_keys"].add(key)
            queue = edge.get("queue")
            if edge.get("kind") != aj.KIND_QUEUE and isinstance(queue, str) and queue:
                entry["queue_bindings"].setdefault(queue, set()).add(
                    (repo, key if isinstance(key, str) and key else None)
                )


def link_bound_queues(entries: dict[str, Exchange]) -> int:
    """Credit each queue with the publishers of the exchanges it is bound to.

    A listener on a bare queue (``@RabbitListener(queues=...)``) names no exchange, so on its
    own it has no publisher. Elsewhere a binding (``@QueueBinding``, ``BindingBuilder``, the
    ``QueueBinding`` data class) names the queue, an exchange and a key. When a queue-only name
    has such a binding, the publishers of that exchange whose routing key reaches the binding
    key (:func:`~amqp_join.link_kind`; a publisher with no resolved key counts as
    ``key_unknown``) are the queue's publishers. Each is recorded under ``bound`` with the
    exchange, the binding key, the publisher count and how the keys met; the queue's
    ``publishers`` and ``publisher_repos`` are raised by them. A queue nothing binds is left
    alone, so a missing publisher stays missing. Returns how many queues gained a publisher.
    """
    linked = 0
    for exchange_name, exchange in entries.items():
        if is_queue(exchange):
            continue
        for queue_name, bindings in exchange["queue_bindings"].items():
            queue = entries.get(queue_name)
            if queue is None or not is_queue(queue):
                continue
            for _repo, binding_key in sorted(bindings, key=lambda b: (b[0], b[1] or "")):
                repos: set[str] = set()
                count = 0
                via = None
                for pub_repo, routing_key in exchange["publisher_keys"]:
                    kind = aj.link_kind(binding_key, routing_key)
                    if kind is None:
                        continue
                    count += 1
                    repos.add(pub_repo)
                    if via is None or kind == aj.MATCH_EXACT:
                        via = kind
                item = {
                    "exchange": exchange_name, "binding_key": binding_key,
                    "publisher_count": count,
                }
                if via:
                    item["matched_via"] = via
                if item not in queue["bound"]:
                    queue["bound"].append(item)
                    if count:
                        if not queue["bound_publishers"]:
                            linked += 1
                        queue["bound_publishers"] += count
                        queue["bound_publisher_repos"] |= repos
    for queue in entries.values():
        if queue["bound_publishers"]:
            queue["publishers"] += queue["bound_publishers"]
            queue["publisher_repos"] |= queue["bound_publisher_repos"]
    return linked


def has_role_edges(entry: Exchange) -> bool:
    """Whether any publisher or listener edge was seen (a name with only declared bindings has
    none, and is not an exchange to list)."""
    return bool(entry["publishers"] or entry["listeners"])


def in_scope_edges(entry: Exchange) -> int:
    """How many edges of the name sit in the repos the caller selected."""
    return int(entry["scope_publishers"] + entry["scope_listeners"])


def is_queue(entry: Exchange) -> bool:
    """Whether every edge of the name only names a queue (a listener on a queue, or a publish
    on the default exchange): such a name is not an exchange and has no exchange status."""
    return entry["queue_edges"] > 0 and entry["exchange_edges"] == 0


def has_key_containing(entry: Exchange, needle: str) -> bool:
    """Whether any routing key or binding key seen on the exchange contains *needle*."""
    return any(needle in k for k in entry["routing_keys"] | entry["binding_keys"])


def render(name: str, entry: Exchange, scoped: bool = False) -> dict[str, Any]:
    """The row of one exchange; long name lists are cut at ``MAX_LISTED`` with a ``*_total``."""
    queue = is_queue(entry)
    row: dict[str, Any] = {
        ("queue" if queue else "exchange"): name,
        "kind": aj.KIND_QUEUE if queue else aj.KIND_EXCHANGE,
        "status": exchange_status(entry["publishers"], entry["listeners"]),
        "publisher_count": entry["publishers"],
        "listener_count": entry["listeners"],
    }
    if scoped:
        row["scoped_publisher_count"] = entry["scope_publishers"]
        row["scoped_listener_count"] = entry["scope_listeners"]
    if queue and entry["bound"]:
        row["bound_to"] = entry["bound"][:MAX_LISTED]
        if entry["bound_publishers"]:
            row["bound_publisher_count"] = entry["bound_publishers"]
    for field, source in (
        ("publisher_repos", "publisher_repos"), ("listener_repos", "listener_repos"),
        ("routing_keys", "routing_keys"), ("binding_keys", "binding_keys"),
    ):
        values = sorted(entry[source])
        row[field] = values[:MAX_LISTED]
        if len(values) > MAX_LISTED:
            row[field + "_total"] = len(values)
    return row
