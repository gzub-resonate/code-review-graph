"""Java extractors follow one same-file hop to a configuration getter or ``@Value`` field.

Synthetic Java through the real parser. The hops: a local alias, a no-argument own-class method
that only returns an expression, a resource-lookup wrapper, a chained getter and a request
variable built earlier in the method. Anything ambiguous yields no key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_review_graph import aws_sdk_links as links
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

HEAD = """\
package app;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.ConfigurationProperties;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.dynamodb.model.GetItemRequest;
public class Svc {
  private final SqsClient sqsClient;
  private final QueueProps queueProperties;
  private final SqsHelper sqsHelper;
  private final S3Props s3p;
  private final AwsProps awsProps;
  @Value("${amazon.cookieJarQueueUrl}") private String cookieJarQueueUrl;
"""


def _edges(tmp_path: Path, body: str, name: str = "Svc.java") -> list[Any]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    path = repo / name
    path.write_text(HEAD + body + "}\n", encoding="utf-8")
    _, edges = CodeParser(repo).parse_file(path)
    return [e for e in edges if (e.target or "").startswith("external:aws-")
            and e.extra.get("resolution") != "configuration_properties"]


def _line_of(edges: list[Any], fragment: str) -> list[Any]:
    return [e for e in edges if fragment in str(e.extra)]


def test_direct_getter_is_unchanged(tmp_path: Path) -> None:
    [edge] = _edges(tmp_path, """
  void a() { sqsClient.sendMessage(SendMessageRequest.builder()
      .queueUrl(queueProperties.getA()).build()); }
""")
    assert edge.extra["config_getter"] == {"type": "QueueProps", "property": "a"}
    assert edge.extra["expr"] == "queueProperties.getA()"
    assert edge.extra["unresolved"] is True


def test_own_method_wrapper_and_getter(tmp_path: Path) -> None:
    [edge] = _edges(tmp_path, """
  void a() { sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(getQueueUrl()).build()); }
  private String getQueueUrl() { return sqsHelper.getQueueUrl(queueProperties.getB()); }
""")
    assert edge.extra["config_getter"] == {"type": "QueueProps", "property": "b"}
    assert edge.extra["expr"] == "getQueueUrl()"


def test_own_method_returning_value_field(tmp_path: Path) -> None:
    [edge] = _edges(tmp_path, """
  void a() { sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(url()).build()); }
  private String url() { return cookieJarQueueUrl; }
""")
    assert edge.extra["config_key"] == "amazon.cookieJarQueueUrl"


def test_local_alias_and_request_variable(tmp_path: Path) -> None:
    edges = _edges(tmp_path, """
  void a() {
    String u = sqsHelper.getQueueUrl(queueProperties.getC());
    ReceiveMessageRequest r = ReceiveMessageRequest.builder().queueUrl(u).build();
    sqsClient.receiveMessage(r);
  }
  void b() {
    var bucketName = s3p.getBucketName();
    PutObjectRequest p = PutObjectRequest.builder().bucket(bucketName).key("k").build();
  }
""")
    queue = [e for e in edges if e.target.startswith("external:aws-sqs:")]
    assert queue and all(
        e.extra["config_getter"] == {"type": "QueueProps", "property": "c"} for e in queue
    )
    [s3] = [e for e in edges if e.target.startswith("external:aws-s3:")]
    assert s3.extra["config_getter"] == {"type": "S3Props", "property": "bucketName"}


def test_chained_getter(tmp_path: Path) -> None:
    [edge] = _edges(tmp_path, """
  void a() { GetItemRequest.builder().tableName(awsProps.getDynamo().getTableName()).build(); }
""")
    assert edge.extra["config_getter"] == {"type": "AwsProps", "property": "dynamo.tableName"}


def test_ambiguous_and_parameter_cases_emit_nothing(tmp_path: Path) -> None:
    edges = _edges(tmp_path, """
  void param(String p) { sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(p).build()); }
  void reassigned() {
    String x = queueProperties.getA(); x = other();
    sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(x).build());
  }
  void shadow(String x) {
    sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(x).build());
  }
  void late() {
    sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(y).build());
    String y = queueProperties.getA();
  }
  void overloaded() { sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(ov()).build()); }
  private String ov() { return queueProperties.getA(); }
  private String ov(int n) { return queueProperties.getB(); }
  void twoStatements() {
    sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(two()).build());
  }
  private String two() { log(); return queueProperties.getA(); }
  void transform() {
    sqsClient.sendMessage(SendMessageRequest.builder().queueUrl(
        String.valueOf(queueProperties.getA())).build());
  }
""")
    assert edges
    assert not [e for e in edges if "config_getter" in e.extra or "config_key" in e.extra]


def _store(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    parser = CodeParser(repo)
    store = GraphStore(str(tmp_path / "graph.db"))
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return store


PROPS = """\
package app;
import org.springframework.boot.context.properties.ConfigurationProperties;
@ConfigurationProperties(prefix = "app.aws")
public class AwsProps {
  private Dyn dynamo;
  public Dyn getDynamo() { return dynamo; }
  public static class Dyn {
    private String tableName;
    public String getTableName() { return tableName; }
  }
}
"""
USER = """\
package app;
import software.amazon.awssdk.services.dynamodb.model.GetItemRequest;
public class Repo {
  private final AwsProps awsProps;
  void a() { GetItemRequest.builder().tableName(awsProps.getDynamo().getTableName()).build(); }
  void b() { GetItemRequest.builder().tableName(awsProps.getOther().getName()).build(); }
}
"""


def test_nested_chain_becomes_a_placeholder_only_when_config_defines_it(tmp_path: Path) -> None:
    store = _store(tmp_path, {
        "src/main/java/app/AwsProps.java": PROPS,
        "src/main/java/app/Repo.java": USER,
        "src/main/resources/application.yml": "app:\n  aws:\n    dynamo:\n      table-name: t1\n",
    })
    rows = links.read_edges(store)
    store.close()
    by_line = {r["line"]: r for r in rows if r["file_path"].endswith("Repo.java")}
    assert by_line[5]["placeholder"] == "app.aws.dynamo.tableName"
    assert by_line[5]["extra"]["config_defined"] is True
    assert not by_line[6]["placeholder"]
    assert json.dumps(by_line[5])  # serialisable
