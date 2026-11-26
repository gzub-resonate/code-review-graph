"""Compact transitions and file-level state listing for Step Functions definitions."""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.query import query_graph


def _machine() -> dict:
    return {
        "StartAt": "Choose",
        "States": {
            "Choose": {
                "Type": "Choice",
                "Choices": [{"Variable": "$.mode", "StringEquals": "a", "Next": "Scale"}],
                "Default": "Resize",
            },
            "Scale": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Next": "Resize",
                "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Fail"}],
            },
            "Resize": {"Type": "Pass", "End": True},
            "Fail": {"Type": "Fail"},
        },
    }


def _built(tmp_path: Path) -> tuple[str, Path]:
    (tmp_path / ".git").mkdir(exist_ok=True)
    folder = tmp_path / "pipelines" / "statemachine"
    folder.mkdir(parents=True)
    machine_file = folder / "m.asl.json"
    machine_file.write_bytes(json.dumps(_machine(), indent=2).encode())
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    full_build(tmp_path, store)
    store.close()
    return str(tmp_path), machine_file


def test_minimal_next_states_keeps_condition_via_and_line(tmp_path: Path) -> None:
    root, _ = _built(tmp_path)
    minimal = query_graph("next_states_of", "Choose", repo_root=root, detail_level="minimal")
    full = query_graph("next_states_of", "Choose", repo_root=root)
    by_target = {e["target"]: e for e in minimal["edges"]}
    assert by_target["m/Scale"]["via"] == "choice"
    assert by_target["m/Scale"]["condition"] == '$.mode StringEquals "a"'
    assert by_target["m/Resize"]["via"] == "default"
    assert all(isinstance(e["line"], int) for e in minimal["edges"])
    assert set(by_target["m/Scale"]) <= {"target", "via", "condition", "conditions", "line"}
    assert len(json.dumps(minimal)) < len(json.dumps(full)) / 2


def test_minimal_previous_states_names_the_source(tmp_path: Path) -> None:
    root, _ = _built(tmp_path)
    minimal = query_graph("previous_states_of", "Resize", repo_root=root, detail_level="minimal")
    assert {e["target"] for e in minimal["edges"]} == {"m/Choose", "m/Scale"}
