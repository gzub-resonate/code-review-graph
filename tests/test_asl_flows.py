"""Step Functions state machines start flows; states say which machine they are in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.flows import (
    detect_entry_points,
    get_flows,
    incremental_trace_flows,
    store_flows,
    trace_flows,
)
from code_review_graph.graph import GraphStore, node_to_dict
from code_review_graph.incremental import full_build, get_db_path, incremental_update

MACHINE = {
    "StartAt": "Validate",
    "States": {
        "Validate": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:123456789012:function:order-validator",
            "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Notify Failure"}],
            "Next": "Route",
        },
        "Route": {
            "Type": "Choice",
            "Choices": [{"Variable": "$.ok", "BooleanEquals": True, "Next": "Fan Out"}],
            "Default": "Notify Failure",
        },
        "Fan Out": {
            "Type": "Parallel",
            "Branches": [{
                "StartAt": "Ship",
                "States": {
                    "Ship": {
                        "Type": "Task",
                        "Resource": "arn:aws:lambda:us-east-1:1:function:order-shipper",
                        "End": True,
                    },
                },
            }],
            "End": True,
        },
        "Notify Failure": {"Type": "Fail"},
    },
}

TERRAFORM = '''
resource "aws_lambda_function" "order_validator" {
  function_name = "order-validator"
}
'''

PYTHON = (
    "class Thing:\n    def __init__(self):\n        self.x = 1\n\n"
    "    def go(self):\n        return 2\n"
)


@pytest.fixture
def store(tmp_path: Path):
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "sfn").mkdir()
    (root / "sfn" / "orders.asl.json").write_text(json.dumps(MACHINE))
    (root / "other.asl.json").write_text(json.dumps(
        {"StartAt": "Notify Failure", "States": {"Notify Failure": {"Type": "Fail"}}},
    ))
    (root / "main.tf").write_text(TERRAFORM)
    (root / "thing.py").write_text(PYTHON)
    s = GraphStore(get_db_path(root))
    full_build(root, s)
    store_flows(s, trace_flows(s))
    yield s
    s.close()


def _node(store, name, kind):
    return next(n for n in store.get_all_nodes() if n.name == name and n.kind == kind)


def test_kinds_are_unchanged_and_the_role_is_recorded(store):
    machine = _node(store, "orders", "Class")
    state = _node(store, "Validate", "Function")
    assert machine.extra["asl_role"] == "state_machine"
    assert state.extra["asl_role"] == "state"
    assert state.extra["state_machine"] == "orders"
    assert state.extra["state_path"] == "Validate"
    assert state.extra["is_machine_start"] is True
    ship = _node(store, "Ship", "Function")
    assert ship.extra["state_path"] == "Fan Out[0]/Ship"
    assert ship.extra["is_start"] is True
    assert ship.extra["is_machine_start"] is False


def test_same_state_name_in_two_machines_is_told_apart(store):
    failures = [n for n in store.get_all_nodes() if n.name == "Notify Failure"]
    assert len(failures) == 2
    assert {node_to_dict(n)["display_name"] for n in failures} == {
        "orders/Notify Failure", "other/Notify Failure",
    }
    assert all("Step Functions" in n.extra["docstring"] for n in failures)
    assert node_to_dict(_node(store, "orders", "Class"))["display_name"] == "statemachine orders"


def test_transitions_are_still_stored(store):
    edges = [e for e in store.get_all_edges() if e.kind == "TRANSITIONS_TO"]
    pairs = {
        (e.source_qualified.rsplit("/", 1)[-1], e.target_qualified.rsplit("/", 1)[-1])
        for e in edges
    }
    assert {("Validate", "Route"), ("Validate", "Notify Failure"), ("Route", "Fan Out"),
            ("Route", "Notify Failure")} <= pairs
    assert any(e.extra.get("via") == "branch" for e in edges)


def test_only_the_start_state_is_an_entry_point_and_dunders_are_not(store):
    entries = {(n.language, n.name) for n in detect_entry_points(store)}
    assert ("asl", "Validate") in entries
    assert ("asl", "Notify Failure") in entries  # the start state of the second machine
    assert not {n for n in entries if n[0] == "asl" and n[1] in ("Route", "Fan Out", "Ship")}
    assert not any(name == "__init__" for _lang, name in entries)


def test_the_flow_is_named_by_the_machine_and_follows_transitions_and_the_lambda(store):
    flows = {f["name"]: f for f in get_flows(store, limit=100)}
    assert "orders" in flows
    flow = flows["orders"]
    names = [store.get_node_by_id(i).name for i in flow["path"]]
    assert names[0] == "Validate"
    assert {"Route", "Fan Out", "Ship", "Notify Failure"} <= set(names)
    # The Task state reaches the Terraform resource it runs.
    assert "resource.aws_lambda_function.order_validator" in names
    assert store.get_node_by_id(flow["entry_point_id"]).extra["state_machine"] == "orders"


def test_incremental_update_keeps_the_flow(store, tmp_path):
    root = tmp_path.resolve()
    before = {f["name"] for f in get_flows(store, limit=100)}
    (root / "sfn" / "orders.asl.json").write_text(json.dumps(MACHINE) + "\n")
    incremental_update(root, store)
    incremental_trace_flows(store, [str(root / "sfn" / "orders.asl.json")], repo_root=root)
    after = [f["name"] for f in get_flows(store, limit=100)]
    assert "orders" in before and after.count("orders") == 1
