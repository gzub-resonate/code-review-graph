"""A Step Functions machine that starts another machine by name.

``next_states_of`` follows the transitions inside one machine, by design; the link from a
``startExecution`` state to the machine it starts is a ``REFERENCES`` edge to
``external:aws-states:<name>``, and ``cross_repo_requests`` with ``origin="stepfunction"``
joins it to the machine definition.
"""

from __future__ import annotations

import json

import pytest

from code_review_graph.asl_resolver import resolve_asl_local_targets
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.query import query_graph
from code_review_graph.tools.registry_tools import cross_repo_requests_func

PARENT = {
    "StartAt": "Run Child",
    "States": {
        "Run Child": {
            "Type": "Task",
            "Resource": "arn:aws:states:::states:startExecution.sync:2",
            "Parameters": {
                "StateMachineArn": (
                    "arn:aws:states:us-east-1:111111111111:stateMachine:child-machine"
                ),
            },
            "Next": "Done",
        },
        "Done": {"Type": "Succeed"},
    },
}
CHILD = {"StartAt": "Only", "States": {"Only": {"Type": "Succeed"}}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    root = (tmp_path / "machines").resolve()
    (root / ".git").mkdir(parents=True)
    (root / "machines").mkdir()
    parser = CodeParser(root)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        for name, definition in (("parent-machine", PARENT), ("child-machine", CHILD)):
            path = root / "machines" / f"{name}.asl.json"
            path.write_text(json.dumps(definition), encoding="utf-8")
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)
        resolve_asl_local_targets(store)
    Registry().register(str(root), alias="machines")
    return root


def test_a_start_execution_state_references_the_machine_it_starts(repo):
    with GraphStore(repo / ".code-review-graph" / "graph.db", read_only=True) as store:
        rows = [tuple(r) for r in store._conn.execute(
            "SELECT kind, target_qualified FROM edges "
            "WHERE source_qualified LIKE '%Run Child' AND kind != 'CONTAINS'"
        )]
    assert ("REFERENCES", "external:aws-states:child-machine") in rows


def test_next_states_of_stays_inside_the_machine(repo):
    reply = query_graph("next_states_of", "Run Child", repo_root=str(repo))
    assert [row["name"] for row in reply["results"]] == ["Done"]


def test_the_cross_repo_tool_shows_the_machine_to_machine_link(repo):
    reply = cross_repo_requests_func(origin="stepfunction", service="child-machine")
    assert reply["total"] == 1
    row = reply["results"][0]
    assert row["machine"] == "parent-machine" and row["state"] == "Run Child"
    assert row["target_kind"] == "states" and row["target_name"] == "child-machine"
    assert row["status"] == "matched" and row["target_repo"] == "machines"
