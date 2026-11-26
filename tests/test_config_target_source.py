"""Honest ``target_source`` for rows named per profile at build time or only by profile.

Synthetic Java and configuration through the real parser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_review_graph import aws_sdk_links as links
from code_review_graph.aws_config_names import resolve_config_names
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

JAVA = """\
package app;
import org.springframework.beans.factory.annotation.Value;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;
public class Svc {
  private final SqsClient sqs;
  @Value("${app.queue}") private String queue;
  @Value("${app.token.queue}") private String tokenQueue;
  @Value("${app.default.queue}") private String defQueue;
  void a() { sqs.sendMessage(SendMessageRequest.builder().queueUrl(queue).build()); }
  void b() { sqs.sendMessage(SendMessageRequest.builder().queueUrl(tokenQueue).build()); }
  void c() { sqs.sendMessage(SendMessageRequest.builder().queueUrl(defQueue).build()); }
}
"""
FILES = {
    "m/src/main/java/app/Svc.java": JAVA,
    "m/src/main/resources/application-dev.yml": (
        "app:\n  queue: q-dev\n  token:\n    queue: AKIAABCDEFGHIJKLMNOP\n"
        "  default:\n    queue: d\n"
    ),
    "m/src/main/resources/application-prod.yml": (
        "app:\n  queue: q-prod\n  token:\n    queue: Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg\n"
    ),
    "m/src/main/resources/application.yml": "app:\n  default:\n    queue: d-default\n",
}


def _store(tmp_path: Path) -> GraphStore:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    parser = CodeParser(repo)
    store = GraphStore(str(tmp_path / "graph.db"))
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return store


def _row(store: GraphStore, key: str) -> dict[str, Any]:
    [row] = [r for r in links.read_edges(store) if r["placeholder"] == key]
    return row


def test_profile_bound_differing_values_count_as_config_resolved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _row(store, "app.queue")
    store.close()
    extra = row["extra"]
    assert row["unresolved"] is True and row["name"] != "q-dev"
    assert extra["target_source"] == "config" and extra["config_profile"] == "differs"
    assert extra["config_alternatives"] == {"dev": "q-dev", "prod": "q-prod"}
    assert extra["unresolved_reason"] == "profiles_differ" and extra["config_defined"] is True


def test_secret_looking_values_never_appear(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _row(store, "app.token.queue")
    store.close()
    blob = json.dumps(row)
    assert "AKIA" not in blob and "Zm9vYmFy" not in blob
    assert "config_alternatives" not in row["extra"]
    assert row["extra"]["target_source"] == "config_key_unresolved"


def test_default_profile_row_is_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _row(store, "app.default.queue")
    store.close()
    assert row["name"] == "d-default" and row["unresolved"] is False
    assert row["extra"]["target_source"] == "config"
    assert row["extra"]["config_profile"] == "default"


def test_build_time_named_row_reports_config_without_changing_its_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolve_config_names(store._conn)
    row = _row(store, "app.queue")
    store.close()
    assert row["extra"]["resolved_names"] == {"dev": "q-dev", "prod": "q-prod"}
    assert row["extra"]["target_source"] == "config"
    assert row["unresolved"] is True and "config_profile" not in row["extra"]
