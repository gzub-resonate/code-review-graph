"""RabbitMQ and generic JMS publish and listen edges."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _java(source: str):
    return CodeParser().parse_bytes(Path("Svc.java"), source.encode())


def _python(source: str):
    return CodeParser().parse_bytes(Path("svc.py"), source.encode())


def _msg(edges):
    return [
        e for e in edges
        if e.kind in ("CONSUMES", "PRODUCES") and e.target.startswith("external:")
    ]


def test_rabbit_listener_handler_and_jms_listener() -> None:
    nodes, edges = _java(
        """
        class L {
            @RabbitListener(queues = {"q-a", "q-b"})
            void a(String m) {}
            @JmsListener(destination = "jms.in")
            void b(String m) {}
        }
        @RabbitListener(queues = "class-q")
        class H {
            @RabbitHandler void h(String m) {}
            void notHandler(String m) {}
        }
        """
    )
    got = {(e.target, e.kind, e.extra["service"]) for e in _msg(edges)}
    assert got == {
        ("external:rabbitmq:q-a", "CONSUMES", "rabbitmq"),
        ("external:rabbitmq:q-b", "CONSUMES", "rabbitmq"),
        ("external:jms:jms.in", "CONSUMES", "jms"),
        ("external:rabbitmq:class-q", "CONSUMES", "rabbitmq"),
    }
    handler = next(n for n in nodes if n.name == "h")
    assert handler.extra["message_listener"] == "rabbitmq"
    assert not any(e.source.endswith("notHandler") for e in _msg(edges))


def test_jms_listener_in_sqs_jms_file_is_sqs() -> None:
    _, edges = _java(
        """
        import com.amazon.sqs.javamessaging.SQSConnectionFactory;
        class L {
            @JmsListener(destination = "sqs-jms-q") void a(String m) {}
        }
        """
    )
    assert {e.target for e in _msg(edges)} == {"external:aws-sqs:sqs-jms-q"}


def test_rabbit_template_publishers() -> None:
    _, edges = _java(
        """
        class P {
            private RabbitTemplate rabbit;
            private JmsTemplate jms;
            void go() {
                rabbit.convertAndSend("ex.orders", "orders.created", payload);
                rabbit.convertAndSend("direct-q", payload);
                rabbit.convertAndSend("", "default-q", payload);
                rabbit.convertAndSend("rk-only", payload, msg -> msg);
                jms.convertAndSend("jms.out", payload);
                jms.send(new ActiveMQQueue("mq.out"), creator);
            }
        }
        """
    )
    got = {(e.target, e.extra.get("routing_key")) for e in _msg(edges)}
    assert ("external:rabbitmq:ex.orders", "orders.created") in got
    assert ("external:rabbitmq:direct-q", "direct-q") in got
    assert ("external:rabbitmq:default-q", "default-q") in got
    assert ("external:rabbitmq:rk-only", "rk-only") in got
    assert ("external:jms:jms.out", None) in got
    assert ("external:jms:mq.out", None) in got
    first = next(e for e in _msg(edges) if e.target == "external:rabbitmq:ex.orders")
    assert first.extra["exchange"] == "ex.orders" and first.kind == "PRODUCES"


def test_send_to_and_unresolved_placeholder() -> None:
    _, edges = _java(
        """
        class S {
            @RabbitListener(queues = "${app.rabbit.in}")
            @SendTo("replies")
            String reply(String m) { return m; }
        }
        """
    )
    kinds = {(e.kind, e.target) for e in _msg(edges)}
    assert ("PRODUCES", "external:rabbitmq:replies") in kinds
    unresolved = [e for e in _msg(edges) if e.extra.get("unresolved")]
    assert len(unresolved) == 1
    assert unresolved[0].target == "external:rabbitmq:?unresolved"
    assert unresolved[0].extra["config_key"] == "app.rabbit.in"


def test_pika_publish_and_consume() -> None:
    _, edges = _python(
        """
def pub(ch):
    ch.basic_publish(exchange="ex", routing_key="rk", body=b"x")
    ch.basic_publish("", "default-q", b"x")

def sub(ch, name):
    ch.basic_consume(queue="work", on_message_callback=cb)
    ch.basic_consume(queue=name, on_message_callback=cb)
"""
    )
    got = {(e.target, e.kind) for e in _msg(edges)}
    assert ("external:rabbitmq:ex", "PRODUCES") in got
    assert ("external:rabbitmq:default-q", "PRODUCES") in got
    assert ("external:rabbitmq:work", "CONSUMES") in got
    assert ("external:rabbitmq:?unresolved", "CONSUMES") in got


def test_kombu_and_celery_are_left_alone() -> None:
    _, edges = _python(
        """
def f(producer, task):
    producer.publish({"a": 1}, exchange="x", routing_key="y")
    task.apply_async(queue="z")
"""
    )
    assert _msg(edges) == []
