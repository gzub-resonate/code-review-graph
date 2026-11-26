"""ConfigProperty values show in file_summary rows and search hits, and only the safe ones."""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph, semantic_search_nodes

YAML = """\
ship:
  delivery:
    sqs:
      queue:
        name: ship-delivery-local
    api-token: tok-visible-only-if-leaked
    client-secret: hunter2-secret-value
    callback: https://user:pw-leak@host.example/cb
    long: %s
spring:
  config:
    activate:
      on-profile: local
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    path = root / "src" / "main" / "resources" / "application-local.yml"
    path.parent.mkdir(parents=True)
    path.write_text(YAML % ("x" * 300), encoding="utf-8")
    nodes, edges = CodeParser(root).parse_file(path)
    graph_dir = root / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "")
        # An index written before a filter existed: a secret that got stored anyway.
        store._conn.execute(
            "UPDATE nodes SET extra = ? WHERE kind = 'ConfigProperty' AND name LIKE '%client%'",
            (json.dumps({"value": "legacy-secret-value-9"}),),
        )
        store._conn.execute(
            "UPDATE nodes SET extra = ? WHERE kind = 'ConfigProperty' AND name LIKE '%callback'",
            (json.dumps({"value": "https://user:pw-leak@host.example/cb"}),),
        )
        store._conn.commit()
    return root


def _rows(reply: dict) -> dict[str, dict]:
    return {r["name"]: r for r in reply["results"] if r["kind"] == "ConfigProperty"}


def test_file_summary_shows_values_and_never_secrets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    reply = query_graph(
        "file_summary", "src/main/resources/application-local.yml", repo_root=str(root),
        max_results=100,
    )
    rows = _rows(reply)
    assert rows["ship.delivery.sqs.queue.name"]["value"] == "ship-delivery-local"
    long_row = rows["ship.delivery.long"]
    assert len(long_row["value"]) == 200 and long_row["value_truncated"] is True
    assert "value_truncated" not in rows["ship.delivery.sqs.queue.name"]
    for name in ("ship.delivery.apiToken", "ship.delivery.clientSecret", "ship.delivery.callback"):
        assert "value" not in rows[name], name
    dump = json.dumps(reply)
    for leaked in ("tok-visible-only-if-leaked", "hunter2-secret-value", "legacy-secret-value-9",
                   "pw-leak"):
        assert leaked not in dump


def test_semantic_search_shows_the_value_and_never_secrets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    reply = semantic_search_nodes(
        "ship.delivery.sqs.queue.name", kind="ConfigProperty", repo_root=str(root),
    )
    rows = _rows(reply)
    assert rows["ship.delivery.sqs.queue.name"]["value"] == "ship-delivery-local"
    broad = semantic_search_nodes("ship delivery", kind="ConfigProperty", repo_root=str(root))
    dump = json.dumps(broad)
    for leaked in ("tok-visible-only-if-leaked", "hunter2-secret-value", "legacy-secret-value-9",
                   "pw-leak"):
        assert leaked not in dump
    # A summary stays a summary.
    minimal = semantic_search_nodes(
        "ship.delivery.sqs.queue.name", kind="ConfigProperty", repo_root=str(root),
        detail_level="minimal",
    )
    assert all("value" not in r for r in minimal["results"])
