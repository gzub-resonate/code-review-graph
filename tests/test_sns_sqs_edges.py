"""SQS / SNS publish and listen edges from Java and Python application code."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _java(source: str):
    return CodeParser().parse_bytes(Path("Svc.java"), source.encode())


def _python(source: str):
    return CodeParser().parse_bytes(Path("svc.py"), source.encode())


def _msg(edges, service=None):
    return [
        e for e in edges
        if e.kind in ("CONSUMES", "PRODUCES") and e.target.startswith("external:")
        and (service is None or e.extra.get("service") == service)
    ]


def test_sqs_listener_attributes_and_entry_point_marker() -> None:
    nodes, edges = _java(
        """
        class L {
            @SqsListener("orders-q")
            void a(String m) {}
            @SqsListener(queueNames = {"q-one", "q-two"})
            void b(String m) {}
            @SqsListener(value = "https://sqs.us-east-1.amazonaws.com/123456789012/url-q")
            void c(String m) {}
        }
        """
    )
    got = {(e.target, e.kind, e.extra["direction"]) for e in _msg(edges, "sqs")}
    assert got == {
        ("external:aws-sqs:orders-q", "CONSUMES", "listen"),
        ("external:aws-sqs:q-one", "CONSUMES", "listen"),
        ("external:aws-sqs:q-two", "CONSUMES", "listen"),
        ("external:aws-sqs:url-q", "CONSUMES", "listen"),
    }
    method = next(n for n in nodes if n.name == "a")
    assert method.extra["message_listener"] == "sqs"
    assert "SqsListener" in ",".join(method.extra["decorators"])


def test_sqs_listener_not_flagged_dead_or_missing_from_entry_points() -> None:
    from code_review_graph.flows import _FRAMEWORK_DECORATOR_PATTERNS

    for deco in ("SqsListener", "SqsListener(\"q\")", "RabbitHandler"):
        assert any(p.search(deco) for p in _FRAMEWORK_DECORATOR_PATTERNS), deco


def test_java_sqs_and_sns_client_calls() -> None:
    _, edges = _java(
        """
        class P {
            private final SqsClient sqs;
            private final AmazonSNS sns;
            void go() {
                sqs.sendMessage(SendMessageRequest.builder()
                    .queueUrl("https://sqs.eu-west-1.amazonaws.com/1/out-q").build());
                sns.publish("arn:aws:sns:us-east-1:123:events-topic", "body");
                sns.publish(PublishRequest.builder().topicArn("arn:aws:sns:us-east-1:1:t2").build());
                sqs.receiveMessage(r -> r.queueUrl("in-q"));
            }
        }
        """
    )
    got = {(e.target, e.kind) for e in _msg(edges)}
    assert got == {
        ("external:aws-sqs:out-q", "PRODUCES"),
        ("external:aws-sns:events-topic", "PRODUCES"),
        ("external:aws-sns:t2", "PRODUCES"),
        ("external:aws-sqs:in-q", "CONSUMES"),
    }
    sns = next(e for e in edges if e.target == "external:aws-sns:t2")
    assert sns.extra["topic"] == "t2" and sns.extra["service"] == "sns"


def test_java_templates_and_constants() -> None:
    _, edges = _java(
        """
        class T {
            static final String Q = "const-q";
            private QueueMessagingTemplate template;
            private SqsTemplate sqsTemplate;
            private NotificationMessagingTemplate notifications;
            void go() {
                template.convertAndSend(Q, "x");
                sqsTemplate.send(to -> to.queue("tmpl-q").payload("p"));
                notifications.sendNotification("notify-topic", "m", "s");
            }
        }
        """
    )
    assert {e.target for e in _msg(edges)} == {
        "external:aws-sqs:const-q", "external:aws-sqs:tmpl-q", "external:aws-sns:notify-topic",
    }


def test_untyped_receiver_and_other_methods_produce_nothing() -> None:
    _, edges = _java(
        """
        class N {
            private Foo foo;
            void go() { foo.publish("x"); foo.send("y"); other.sendMessage("z"); }
        }
        """
    )
    assert _msg(edges) == []


def test_placeholder_and_unresolved_names_are_flagged_not_dropped() -> None:
    _, edges = _java(
        """
        class U {
            @Value("${app.queue-name}") private String queue;
            private final AmazonSQS sqs;
            @SqsListener("${app.listen_queue}")
            void l(String m) {}
            void go(String dynamic) {
                sqs.sendMessage(queue, "b");
                sqs.sendMessage(dynamic, "b");
                sqs.sendMessage("prefix-" + dynamic, "b");
            }
        }
        """
    )
    msgs = _msg(edges, "sqs")
    assert len(msgs) == 4
    assert all(e.target == "external:aws-sqs:?unresolved" for e in msgs)
    assert all(e.extra["unresolved"] is True for e in msgs)
    keys = {e.extra.get("config_key") for e in msgs}
    assert {"app.queueName", "app.listenQueue"} <= keys
    assert any(e.extra.get("expr") == "dynamic" for e in msgs)


def test_python_boto3_clients() -> None:
    _, edges = _python(
        '''
import boto3
import os
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123/py-queue"
sqs = boto3.client("sqs")
sns = boto3.client(service_name="sns")

def send():
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody="x")
    sns.publish(TopicArn="arn:aws:sns:us-east-1:123:py-topic", Message="m")

def poll():
    sqs.receive_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123/py-in")

def dynamic():
    sqs.send_message(QueueUrl=os.environ["OUT_QUEUE"], MessageBody="x")
    boto3.client("sns").publish(TopicArn=f"arn:aws:sns:us-east-1:1:{os.getenv('E')}", Message="m")

class Svc:
    def __init__(self):
        self.q = boto3.client("sqs")
    def go(self):
        self.q.send_message(QueueUrl="self-q", MessageBody="b")
'''
    )
    got = {(e.source.rsplit("::", 1)[-1], e.target, e.kind) for e in _msg(edges)}
    assert ("send", "external:aws-sqs:py-queue", "PRODUCES") in got
    assert ("send", "external:aws-sns:py-topic", "PRODUCES") in got
    assert ("poll", "external:aws-sqs:py-in", "CONSUMES") in got
    assert ("Svc.go", "external:aws-sqs:self-q", "PRODUCES") in got
    unresolved = [e for e in _msg(edges) if e.extra.get("unresolved")]
    assert len(unresolved) == 2
    assert {e.extra.get("env_var") for e in unresolved} == {"OUT_QUEUE", "E"}


def test_python_non_boto_publish_is_ignored() -> None:
    _, edges = _python(
        """
def f(bus, client):
    bus.publish(TopicArn="x")
    client.send_message(QueueUrl="y")
"""
    )
    assert _msg(edges) == []


def test_python_sqs_resource_queue_object() -> None:
    _, edges = _python(
        """
import boto3
def f():
    sqs = boto3.resource("sqs")
    queue = sqs.get_queue_by_name(QueueName="res-q")
    queue.send_message(MessageBody="b")
    queue.receive_messages()
"""
    )
    assert {(e.target, e.kind) for e in _msg(edges)} == {
        ("external:aws-sqs:res-q", "PRODUCES"), ("external:aws-sqs:res-q", "CONSUMES"),
    }
