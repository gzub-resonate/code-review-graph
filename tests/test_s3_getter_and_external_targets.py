"""A bucket read from a bean getter names its property; ``external_targets`` lists targets."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.aws_sdk_links import read_edges
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools import query_graph

ANNOUNCER = """\
package app;

import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;

public class Announcer {
    private final S3Client s3Client;
    private final AnnounceProperties announceProperties;

    public Announcer(S3Client s3Client, AnnounceProperties announceProperties) {
        this.s3Client = s3Client;
        this.announceProperties = announceProperties;
    }

    void upload(String body) {
        PutObjectRequest request = PutObjectRequest.builder()
            .bucket(announceProperties.getS3Bucket())
            .key("batch.json")
            .build();
        s3Client.putObject(request);
    }
}
"""

PROPERTIES = """\
package app;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "announce")
public class AnnounceProperties {
    private String s3Bucket;
    public String getS3Bucket() { return s3Bucket; }
}
"""

YAML = "announce:\n  s3-bucket: some-bucket\n"


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


FILES = {
    "src/main/java/app/Announcer.java": ANNOUNCER,
    "src/main/java/app/AnnounceProperties.java": PROPERTIES,
    "src/main/resources/application.yml": YAML,
}


def test_getter_is_recorded_on_the_unresolved_edge(tmp_path):
    store = _store(tmp_path, FILES)
    try:
        edges = read_edges(store)
    finally:
        store.close()
    assert len(edges) == 1
    edge = edges[0]
    assert edge["unresolved"] and edge["service"] == "s3"
    assert edge["operation"] == "put_object"
    # The property key is joined from the class prefix and the getter, and the YAML defines it.
    assert edge["placeholder"] == "announce.s3Bucket"
    assert edge["extra"]["config_defined"] is True


def test_key_not_defined_in_config_files_is_flagged(tmp_path):
    files = {k: v for k, v in FILES.items() if not k.endswith(".yml")}
    store = _store(tmp_path, files)
    try:
        edge = read_edges(store)[0]
    finally:
        store.close()
    assert edge["placeholder"] == "announce.s3Bucket"
    assert edge["extra"]["config_defined"] is False


def test_unknown_owner_type_keeps_the_name_unresolved(tmp_path):
    files = {"src/main/java/app/Announcer.java": ANNOUNCER}
    store = _store(tmp_path, files)
    try:
        edge = read_edges(store)[0]
    finally:
        store.close()
    assert edge["unresolved"] and not edge["placeholder"]
    assert "config_defined" not in edge["extra"]


@pytest.fixture
def messaging_repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    path = (root / "Pub.java").as_posix()
    store.upsert_node(NodeInfo(
        kind="File", name="Pub.java", file_path=path, line_start=1, line_end=20, language="java",
    ))
    for name in ("publishA", "publishB", "listen"):
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=path, line_start=2, line_end=3, language="java",
        ))
    rows = [
        ("publishA", "PRODUCES", "external:rabbitmq:attribute.exchange", {}),
        ("publishB", "PRODUCES", "external:rabbitmq:attribute.exchange", {}),
        ("listen", "CONSUMES", "external:rabbitmq:permutation.queue", {"unresolved": True}),
        ("publishA", "REFERENCES", "external:aws-s3:data-bkt", {}),
    ]
    for source, kind, target, extra in rows:
        store.upsert_edge(EdgeInfo(
            kind=kind, source=f"{path}::{source}", target=target, file_path=path, line=2,
            extra=extra,
        ))
    store.commit()
    store.close()
    return root


def test_external_targets_lists_a_family_with_counts(messaging_repo):
    result = query_graph("external_targets", "external:rabbitmq", repo_root=str(messaging_repo))
    assert result["status"] == "ok"
    by_target = {r["target"]: r for r in result["results"]}
    assert set(by_target) == {
        "external:rabbitmq:attribute.exchange", "external:rabbitmq:permutation.queue",
    }
    exchange = by_target["external:rabbitmq:attribute.exchange"]
    assert exchange["edge_kinds"] == {"PRODUCES": 2}
    assert exchange["source_count"] == 2 and exchange["unresolved"] is False
    assert by_target["external:rabbitmq:permutation.queue"]["unresolved"] is True
    assert result["families"] == {"rabbitmq": 2}


def test_external_targets_without_prefix_lists_every_family(messaging_repo):
    result = query_graph("external_targets", "", repo_root=str(messaging_repo))
    assert result["families"] == {"rabbitmq": 2, "aws-s3": 1}
    assert result["result_count"] == 3


def test_external_targets_rejects_a_prefix_that_is_not_external(messaging_repo):
    result = query_graph("external_targets", "rabbitmq", repo_root=str(messaging_repo))
    assert result["status"] == "error"
