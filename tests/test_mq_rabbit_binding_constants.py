"""RabbitMQ listener bindings and constants declared in another class."""

from pathlib import Path

from code_review_graph.parser import CodeParser

NAMES = """
package com.example.shared;
public final class OrderNames {
    public static final String EXCHANGE = "orders.exchange";
    public static final String CREATED = "orders.created";
    public static final String PATTERN = "orders.#";
    public static final String DUP = "one";
    public static final String QUEUE = "orders.audit.queue";
}
"""

OTHER = """
package com.example.other;
public final class OrderNames {
    public static final String DUP = "two";
}
"""

LISTENER = """
package com.example.app;
import com.example.shared.OrderNames;
class OrderListener {
    @RabbitListener(bindings = @QueueBinding(
        value = @Queue(name = OrderNames.QUEUE),
        exchange = @Exchange(name = OrderNames.EXCHANGE, type = "topic"),
        key = {OrderNames.PATTERN, OrderNames.CREATED}))
    void onOrder(String m) {}

    @RabbitListener(bindings = @QueueBinding(
        value = @Queue("amb.queue"), exchange = @Exchange("amb.exchange"),
        key = Dup.DUP))
    void ambiguous(String m) {}
}
"""

PUBLISHER = """
package com.example.pub;
import com.example.shared.OrderNames;
class OrderPublisher {
    private RabbitTemplate rabbit;
    void go(Object p) {
        rabbit.convertAndSend(OrderNames.EXCHANGE, OrderNames.CREATED, p);
        rabbit.convertAndSend(OrderNames.EXCHANGE, OrderNames.CREATED + ".v2", p);
    }
}
"""

DUP_A = """
package com.example.a;
public class Dup { public static final String DUP = "one"; }
"""
DUP_B = """
package com.example.b;
public class Dup { public static final String DUP = "two"; }
"""


def _repo(tmp_path: Path) -> CodeParser:
    files = {
        "shared/OrderNames.java": NAMES,
        "app/OrderListener.java": LISTENER,
        "pub/OrderPublisher.java": PUBLISHER,
        "a/Dup.java": DUP_A,
        "b/Dup.java": DUP_B,
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return CodeParser(tmp_path)


def _edges(parser: CodeParser, tmp_path: Path, rel: str):
    _, edges = parser.parse_file(tmp_path / rel)
    return [e for e in edges if e.kind in ("CONSUMES", "PRODUCES")]


def test_binding_and_publisher_meet_on_one_exchange(tmp_path: Path) -> None:
    parser = _repo(tmp_path)
    consumed = [
        e for e in _edges(parser, tmp_path, "app/OrderListener.java")
        if e.extra.get("resolution") == "rabbit_listener_binding"
        and e.target == "external:rabbitmq:orders.exchange"
    ]
    assert {e.extra["binding_key"] for e in consumed} == {"orders.#", "orders.created"}
    assert all(e.extra["queue"] == "orders.audit.queue" for e in consumed)
    assert all(e.kind == "CONSUMES" for e in consumed)

    published = [
        e for e in _edges(parser, tmp_path, "pub/OrderPublisher.java")
        if e.target == "external:rabbitmq:orders.exchange"
    ]
    keys = {e.extra["routing_key"] for e in published}
    assert "orders.created" in keys and "orders.created.v2" in keys
    assert all(e.kind == "PRODUCES" for e in published)


def test_existing_queue_keyed_edge_is_kept(tmp_path: Path) -> None:
    parser = _repo(tmp_path)
    queue_edges = [
        e for e in _edges(parser, tmp_path, "app/OrderListener.java")
        if e.extra.get("resolution") == "rabbit_listener"
    ]
    assert {e.target for e in queue_edges} >= {"external:rabbitmq:orders.audit.queue"}


def test_ambiguous_constant_stays_unresolved(tmp_path: Path) -> None:
    parser = _repo(tmp_path)
    ambiguous = [
        e for e in _edges(parser, tmp_path, "app/OrderListener.java")
        if e.extra.get("resolution") == "rabbit_listener_binding"
        and e.target == "external:rabbitmq:amb.exchange"
    ]
    assert len(ambiguous) == 1
    assert ambiguous[0].extra["binding_key"] is None
    assert ambiguous[0].extra["binding_key_unresolved"] is True


def test_same_simple_name_in_two_packages_uses_the_import(tmp_path: Path) -> None:
    parser = _repo(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "OrderNames.java").write_text(OTHER)
    parser = CodeParser(tmp_path)
    edges = [
        e for e in _edges(parser, tmp_path, "pub/OrderPublisher.java")
        if e.target == "external:rabbitmq:orders.exchange"
    ]
    assert edges and all(e.extra["routing_key"].startswith("orders.created") for e in edges)
