"""AWS resource names that live in Spring configuration are resolved per profile.

Everything is synthetic: inline Java through the real parser, inline configuration files, and
hand-made Terraform declaration rows for the join.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import aws_sdk_links as links
from code_review_graph.aws_config_names import is_secret_key, resolve_config_names, sanitize_name
from code_review_graph.aws_sdk_calls import classify_access, sdk_operation_name
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing

DELIVERY_PROPS = """\
package app;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "app.delivery")
public class DeliveryProperties {
    private String queueUrl;
    private String audit;
    private String vaultBucket;
    public String getQueueUrl() { return queueUrl; }
    public String getAudit() { return audit; }
    public String getVaultBucket() { return vaultBucket; }
}
"""

SENDER = """\
package app;

import org.springframework.beans.factory.annotation.Value;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;
import software.amazon.awssdk.services.sqs.model.DeleteMessageRequest;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;

public class Sender {
    private final SqsClient sqs;
    private final S3Client s3;
    private final DeliveryProperties props;

    @Value("${app.delivery.sqs.queue.name}")
    private String queueName;
    @Value("${app.delivery.env.queue}")
    private String envQueue;
    @Value("${app.delivery.env.queue-default}")
    private String defaultedQueue;
    @Value("${app.vault.access-key}")
    private String secretNamedBucket;
    @Value("${app.batch.bucket}")
    private String batchBucket;

    public Sender(SqsClient sqs, S3Client s3, DeliveryProperties props) {
        this.sqs = sqs;
        this.s3 = s3;
        this.props = props;
    }

    void viaGetter(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl(props.getQueueUrl()).messageBody(body).build());
    }

    void viaValue(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl(queueName).messageBody(body).build());
    }

    void viaEnv(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl(envQueue).messageBody(body).build());
    }

    void viaDefault(String body) {
        sqs.sendMessage(SendMessageRequest.builder()
            .queueUrl(defaultedQueue).messageBody(body).build());
    }

    void ack(String handle) {
        sqs.deleteMessage(DeleteMessageRequest.builder().queueUrl(queueName).build());
    }

    void store() {
        s3.putObject(PutObjectRequest.builder().bucket(batchBucket).key("a").build());
        s3.getObject(GetObjectRequest.builder().bucket(props.getVaultBucket()).key("a").build());
        s3.putObject(PutObjectRequest.builder().bucket(secretNamedBucket).key("a").build());
    }
}
"""

APPLICATION = """\
app:
  delivery:
    sqs:
      queue:
        name: jobs-default
    queue-url: https://sqs.us-east-1.amazonaws.com/123456789012/orders-q
    env:
      queue: ${APP_QUEUE}
      queue-default: ${APP_QUEUE_B:fallback-q}
    note: plain-text-note-that-must-not-be-stored
  vault:
    access-key: should-never-be-stored-bucket
  batch:
    bucket: batch-default
"""

APPLICATION_DEV = """\
app:
  delivery:
    sqs:
      queue:
        name: jobs-dev
"""

APPLICATION_PROD = "app.delivery.sqs.queue.name=jobs-prod\napp.batch.bucket=batch-prod\n"

FILES = {
    "svc/src/main/java/app/DeliveryProperties.java": DELIVERY_PROPS,
    "svc/src/main/java/app/Sender.java": SENDER,
    "svc/src/main/resources/application.yml": APPLICATION,
    "svc/src/main/resources/application-dev.yml": APPLICATION_DEV,
    "svc/src/main/resources/application-prod.properties": APPLICATION_PROD,
}


def _store(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    parser = CodeParser(repo)
    store = GraphStore(str(tmp_path / "graph.db"))
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return store


def _extras(store: GraphStore) -> list[tuple[str, str, dict[str, Any]]]:
    rows = store._conn.execute(
        "SELECT kind, target_qualified, extra FROM edges "
        "WHERE target_qualified LIKE 'external:aws-%'"
    ).fetchall()
    return [(k, t, json.loads(e)) for k, t, e in rows]


@pytest.fixture
def resolved(tmp_path: Path) -> GraphStore:
    store = _store(tmp_path, FILES)
    resolve_config_names(store._conn)
    yield store
    store.close()


def _edge(store: GraphStore, **want: Any) -> dict[str, Any]:
    hits = [e for _, _, e in _extras(store) if all(e.get(k) == v for k, v in want.items())]
    assert len(hits) == 1, (want, hits)
    return hits[0]


# -- Resolution per profile ---------------------------------------------------------------------


def test_value_annotation_queue_is_resolved_per_profile(resolved: GraphStore) -> None:
    edge = _edge(resolved, config_key="app.delivery.sqs.queue.name", operation="send_message")
    assert edge["resolved_names"] == {
        "default": "jobs-default", "dev": "jobs-dev", "prod": "jobs-prod",
    }
    assert "env_placeholder" not in edge


def test_getter_url_is_reduced_to_the_queue_name(resolved: GraphStore) -> None:
    edge = _edge(resolved, config_getter={"type": "DeliveryProperties", "property": "queueUrl"})
    assert edge["resolved_names"] == {"default": "orders-q"}


def test_s3_bucket_value_edge_is_resolved(resolved: GraphStore) -> None:
    edge = _edge(resolved, placeholder="app.batch.bucket")
    assert edge["resolved_names"] == {"default": "batch-default", "prod": "batch-prod"}
    assert edge["sdk_operation"] == "PutObject" and edge["access"] == "write"


def test_env_only_placeholder_stays_unresolved_and_a_default_is_used(resolved: GraphStore) -> None:
    env = _edge(resolved, config_key="app.delivery.env.queue")
    assert "resolved_names" not in env and env["env_placeholder"] == "APP_QUEUE"
    defaulted = _edge(resolved, config_key="app.delivery.env.queueDefault")
    assert defaulted["resolved_names"] == {"default": "fallback-q"}


def test_multi_document_yaml_and_conflicts(tmp_path: Path) -> None:
    files = {
        "svc/src/main/java/app/Sender.java": SENDER,
        "svc/src/main/resources/application.yml": (
            "app:\n  delivery:\n    sqs:\n      queue:\n        name: base-q\n"
            "---\nspring:\n  config:\n    activate:\n      on-profile: qa\n"
            "app:\n  delivery:\n    sqs:\n      queue:\n        name: qa-q\n"
            "---\nspring:\n  config:\n    activate:\n      on-profile: '!prod'\n"
            "app:\n  delivery:\n    sqs:\n      queue:\n        name: not-attributed\n"
        ),
        # bootstrap defines the default profile differently: the profile is not guessed.
        "svc/src/main/resources/bootstrap.yml": (
            "app.delivery.sqs.queue.name: other-q\n"
        ),
    }
    store = _store(tmp_path, files)
    resolve_config_names(store._conn)
    edge = _edge(store, config_key="app.delivery.sqs.queue.name", operation="send_message")
    store.close()
    assert edge["resolved_names"] == {"qa": "qa-q"}


def test_test_resources_are_not_read(tmp_path: Path) -> None:
    files = {
        "svc/src/main/java/app/Sender.java": SENDER,
        "svc/src/test/resources/application.yml": (
            "app.delivery.sqs.queue.name: test-only-q\n"
        ),
    }
    store = _store(tmp_path, files)
    resolve_config_names(store._conn)
    edge = _edge(store, config_key="app.delivery.sqs.queue.name", operation="send_message")
    store.close()
    assert "resolved_names" not in edge


def test_rerun_drops_names_the_config_no_longer_yields(tmp_path: Path) -> None:
    store = _store(tmp_path, FILES)
    resolve_config_names(store._conn)
    config = tmp_path / "repo/svc/src/main/resources/application-dev.yml"
    config.write_text("app:\n  other: x\n", encoding="utf-8")
    resolve_config_names(store._conn)
    edge = _edge(store, config_key="app.delivery.sqs.queue.name", operation="send_message")
    store.close()
    assert edge["resolved_names"] == {"default": "jobs-default", "prod": "jobs-prod"}


# -- Masking ------------------------------------------------------------------------------------


def test_secret_named_key_used_as_bucket_is_masked(resolved: GraphStore) -> None:
    edge = _edge(resolved, placeholder="app.vault.accessKey")
    assert edge.get("names_masked") is True
    assert "resolved_names" not in edge


def test_no_unrelated_or_secret_value_reaches_the_graph(resolved: GraphStore) -> None:
    dump = json.dumps([list(r) for r in resolved._conn.execute(
        "SELECT * FROM edges").fetchall()]) + json.dumps([list(r) for r in resolved._conn.execute(
            "SELECT * FROM nodes").fetchall()])
    # Non-secret values now live on ConfigProperty nodes (config_values); secrets and AWS
    # account ids never do.
    for secret in ("should-never-be-stored-bucket", "123456789012"):
        assert secret not in dump
    assert "plain-text-note-that-must-not-be-stored" in dump


@pytest.mark.parametrize(
    ("kind", "key", "raw", "name"),
    [
        ("queue", "a.queue", "https://sqs.us-east-1.amazonaws.com/123456789012/q-one", "q-one"),
        ("queue", "a.queue", "arn:aws:sqs:us-east-1:123456789012:q-two", "q-two"),
        ("topic", "a.topic", "arn:aws:sns:us-east-1:123456789012:t-one", "t-one"),
        ("bucket", "a.bucket", "arn:aws:s3:::b-one/some/prefix", "b-one"),
        ("bucket", "a.bucket", "s3://b-two/x", "b-two"),
        ("queue", "a.queue", "${Q:dflt-q}", "dflt-q"),
        ("queue", "a.queue", "pre-${ENV:dev}-post", "pre-dev-post"),
        ("table", "a.table", "events_v2", "events_v2"),
    ],
)
def test_sanitize_reduces_and_accepts(kind: str, key: str, raw: str, name: str) -> None:
    assert sanitize_name(kind, key, raw) == (name, None, False)


@pytest.mark.parametrize(
    ("kind", "key", "raw"),
    [
        ("bucket", "a.password", "fine-name"),
        ("bucket", "a.apiKey", "fine-name"),
        ("bucket", "a.auth-token", "fine-name"),
        ("bucket", "a.private.bucket", "fine-name"),
        ("queue", "a.queue", "https://user:pw@sqs.us-east-1.amazonaws.com/123456789012/q"),
        ("queue", "a.queue", "has space"),
        ("queue", "a.queue", "q" * 129),
        ("queue", "a.queue", "q?x=1"),
        ("queue", "a.queue", "123456789012"),
        ("bucket", "a.bucket", "AKIAABCDEFGHIJKLMNOP"),
        ("queue", "a.queue", "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"),
        ("secret", "a.secret-id", "my/secret/id"),
        ("parameter", "a.param", "/some/param"),
        ("bucket", "a.bucket", "https://b.s3.amazonaws.com/x"),
    ],
)
def test_sanitize_refuses(kind: str, key: str, raw: str) -> None:
    name, env, _ = sanitize_name(kind, key, raw)
    assert name is None and env is None


def test_sanitize_env_placeholder_and_secret_key_flags() -> None:
    assert sanitize_name("queue", "a.queue", "${ONLY_ENV}") == (None, "ONLY_ENV", False)
    assert sanitize_name("queue", "a.queue", "${ONLY_ENV:}") == (None, "ONLY_ENV", False)
    assert sanitize_name("bucket", "a.secret", "x")[2] is True
    assert is_secret_key("app.vault.accessKey") and is_secret_key("a.api-key")
    assert not is_secret_key("app.delivery.queueName")


# -- Operations and access ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "operation", "access", "sdk_op"),
    [
        ("s3", "get_object", "read", "GetObject"), ("s3", "put_object", "write", "PutObject"),
        ("s3", "delete_objects", "delete", "DeleteObjects"),
        ("s3", "list_objects_v2", "list", "ListObjectsV2"),
        ("s3", "create_bucket", "admin", "CreateBucket"),
        ("s3", "put_bucket_policy", "admin", "PutBucketPolicy"),
        ("sqs", "send_message", "write", "SendMessage"),
        ("sqs", "receive_message", "read", "ReceiveMessage"),
        ("sqs", "delete_message", "delete", "DeleteMessage"),
        ("sqs", "purge_queue", "delete", "PurgeQueue"),
        ("sns", "publish", "write", "Publish"),
        ("dynamodb", "get_item", "read", "GetItem"), ("dynamodb", "put_item", "write", "PutItem"),
        ("dynamodb", "update_table", "admin", "UpdateTable"),
        ("lambda", "invoke", "invoke", "Invoke"),
    ],
)
def test_access_and_canonical_operation(
    service: str, operation: str, access: str, sdk_op: str,
) -> None:
    assert classify_access(service, operation) == access
    assert sdk_operation_name(operation) == sdk_op


def test_sqs_and_s3_edges_carry_operation_access_and_sdk_operation(resolved: GraphStore) -> None:
    seen = {
        (e["service"], e["sdk_operation"], e["access"])
        for kind, target, e in _extras(resolved)
        if e.get("resolution") != "configuration_properties" and "sdk_operation" in e
    }
    assert ("sqs", "SendMessage", "write") in seen
    assert ("sqs", "DeleteMessage", "delete") in seen
    assert ("s3", "PutObject", "write") in seen and ("s3", "GetObject", "read") in seen
    deletes = [(k, t) for k, t, e in _extras(resolved) if e.get("sdk_operation") == "DeleteMessage"]
    assert deletes and all(k == "CONSUMES" for k, _ in deletes)


def test_python_boto3_sqs_edges_have_operations(tmp_path: Path) -> None:
    src = (
        "import boto3\n\nsqs = boto3.client('sqs')\n\n"
        "def run():\n"
        "    sqs.send_message(QueueUrl='https://x/1/py-q', MessageBody='b')\n"
        "    sqs.receive_message(QueueUrl='https://x/1/py-q')\n"
        "    sqs.delete_message(QueueUrl='https://x/1/py-q', ReceiptHandle='h')\n"
    )
    store = _store(tmp_path, {"job.py": src})
    ops = {(e["sdk_operation"], e["access"]) for _, t, e in _extras(store) if "py-q" in t}
    store.close()
    assert ops == {
        ("SendMessage", "write"), ("ReceiveMessage", "read"), ("DeleteMessage", "delete"),
    }


# -- The join -------------------------------------------------------------------------------


def _declare(service: str, name: str, kind: str) -> dict[str, Any]:
    return {
        "file_path": "/infra/main.tf", "line": 1, "source": "main.tf", "service": service,
        "kind": kind, "name": name, "operation": "declare", "access": "declare",
        "sdk": "terraform", "unresolved": False, "placeholder": None, "extra": {},
    }


def test_join_matches_profile_names_and_keeps_other_statuses(tmp_path: Path) -> None:
    store = _store(tmp_path, FILES)
    resolve_config_names(store._conn)
    code = links.read_edges(store)
    store.close()
    repos = [{"path": str(tmp_path / "repo"), "alias": "svc"},
             {"path": str(tmp_path / "infra"), "alias": "infra"}]
    declared = [_declare("sqs", "jobs-prod", "queue"), _declare("s3", "batch-default", "bucket")]
    rows = links.resolve_links({0: code, 1: declared}, repos, lambda i, t: False)
    by_key = {(r["operation"], r.get("placeholder") or r.get("config_getter")): r for r in rows}
    queue = next(r for r in rows if r.get("placeholder") == "app.delivery.sqs.queue.name"
                 and r["operation"] == "send_message")
    assert queue["status"] == "matched" and queue["target_repo"] == "infra"
    assert queue["matched_profiles"] == ["prod"]
    assert queue["names"] == {"default": "jobs-default", "dev": "jobs-dev", "prod": "jobs-prod"}
    bucket = next(r for r in rows if r.get("placeholder") == "app.batch.bucket")
    assert bucket["status"] == "matched" and bucket["matched_profiles"] == ["default"]
    # Names resolved, but nothing declares them: not_found, never a guess.
    url_queue = next(r for r in rows if r.get("names") == {"default": "orders-q"})
    assert url_queue["status"] == "not_found" and "matched_profiles" not in url_queue
    # No resolved name at all stays unresolved.
    env = next(r for r in rows if r.get("env_placeholder") == "APP_QUEUE")
    assert env["status"] == "unresolved"
    masked = next(r for r in rows if r.get("names_masked"))
    assert masked["status"] == "unresolved" and "names" not in masked
    assert by_key


def test_join_is_ambiguous_across_two_declaring_repos(tmp_path: Path) -> None:
    store = _store(tmp_path, FILES)
    resolve_config_names(store._conn)
    code = links.read_edges(store)
    store.close()
    repos = [{"path": str(tmp_path / n), "alias": a}
             for n, a in (("repo", "svc"), ("a", "a"), ("b", "b"))]
    rows = links.resolve_links(
        {0: code, 1: [_declare("sqs", "jobs-dev", "queue")],
         2: [_declare("sqs", "jobs-prod", "queue")]},
        repos, lambda i, t: False,
    )
    queue = next(r for r in rows if r.get("placeholder") == "app.delivery.sqs.queue.name"
                 and r["operation"] == "send_message")
    assert queue["status"] == "ambiguous" and queue["candidates"] == ["a", "b"]


# -- Query surfaces -----------------------------------------------------------------------------


def test_external_targets_lists_names_compactly(tmp_path: Path) -> None:
    store = _store(tmp_path, FILES)
    resolve_config_names(store._conn)
    from code_review_graph.tools.query import _query_external_targets

    result = _query_external_targets(store, "external:aws-sqs", False, 50)
    store.close()
    unresolved = [r for r in result["results"] if r["unresolved"]]
    names: dict[str, list[str]] = {}
    for r in unresolved:
        for p, n in r.get("names", {}).items():
            names.setdefault(p, []).extend(n)
    assert {"jobs-default", "orders-q", "fallback-q"} <= set(names["default"])
    assert names["prod"] == ["jobs-prod"]
    assert all(len(v) <= 5 for r in unresolved for v in r.get("names", {}).values())


def test_postprocessing_runs_the_resolution(tmp_path: Path) -> None:
    store = _store(tmp_path, FILES)
    result = run_post_processing(store)
    edge = _edge(store, config_key="app.delivery.sqs.queue.name", operation="send_message")
    store.close()
    assert result["config_names"]["resolved"] >= 1
    assert "prod" in edge["resolved_names"]


def test_bootstrap_files_are_indexed_as_config() -> None:
    from code_review_graph.config_keys import is_spring_config_path

    assert is_spring_config_path(Path("bootstrap.yml"))
    assert is_spring_config_path(Path("bootstrap-dev.properties"))
