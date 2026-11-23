"""A Task state is linked to the Terraform resource that defines the Lambda or ECS task it runs."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from code_review_graph.asl_resolver import RESOLUTION, resolve_asl_local_targets
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.tools import query_graph

MACHINE = {
    "StartAt": "Validate",
    "States": {
        "Validate": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:123456789012:function:order-validator:live",
            "Next": "Enrich",
        },
        "Enrich": {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:order-enricher"},
            "Next": "Report",
        },
        "Report": {
            "Type": "Task",
            "Resource": "arn:aws:states:::ecs:runTask.sync",
            "Parameters": {
                "Cluster": "arn:aws:ecs:us-east-1:1:cluster/batch",
                "TaskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/report-job:7",
            },
            "Next": "Remote",
        },
        "Remote": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:1:function:in-another-repo",
            "End": True,
        },
    },
}

TERRAFORM = '''
resource "aws_lambda_function" "order_validator" {
  function_name = "order-validator"
}
resource "aws_lambda_function" "order_enricher" {
  function_name = "order-enricher"
}
resource "aws_ecs_task_definition" "report_job" {
  family = "report-job"
}
resource "aws_ecs_task_definition" "unrelated" {
  family = "unrelated"
}
'''


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / "sfn").mkdir()
    (root / "sfn" / "order.asl.json").write_text(json.dumps(MACHINE))
    (root / "main.tf").write_text(TERRAFORM)
    yield root
    shutil.rmtree(tmp, ignore_errors=True)


def build(root):
    store = GraphStore(get_db_path(root))
    full_build(root, store)
    return store


def links(store):
    """(state, resource, original external target) for every derived link."""
    found = set()
    for edge in store.get_all_edges():
        if edge.kind == "REFERENCES" and edge.extra.get("resolution") == RESOLUTION:
            found.add((
                edge.source_qualified.rsplit("::", 1)[-1].rsplit("/", 1)[-1],
                edge.target_qualified.rsplit("::", 1)[-1],
                edge.extra["external_target"],
            ))
    return found


EXPECTED = {
    ("Validate", "resource.aws_lambda_function.order_validator",
     "external:aws-lambda:order-validator"),
    ("Enrich", "resource.aws_lambda_function.order_enricher",
     "external:aws-lambda:order-enricher"),
    ("Report", "resource.aws_ecs_task_definition.report_job",
     "external:aws-ecs:report-job"),
}


def test_states_are_linked_to_the_terraform_that_defines_what_they_run(repo):
    store = build(repo)
    try:
        assert links(store) == EXPECTED
    finally:
        store.close()


def test_the_link_is_inferred_and_the_external_edge_stays(repo):
    store = build(repo)
    try:
        edges = [e for e in store.get_all_edges() if e.kind == "REFERENCES"]
        derived = [e for e in edges if e.extra.get("resolution") == RESOLUTION]
        assert {(e.extra["confidence"], e.extra["confidence_tier"]) for e in derived} == {
            (0.6, "INFERRED"),
        }
        externals = {
            e.target_qualified for e in edges if e.target_qualified.startswith("external:")
        }
        assert externals == {
            "external:aws-lambda:order-validator", "external:aws-lambda:order-enricher",
            "external:aws-ecs:report-job", "external:aws-lambda:in-another-repo",
        }
    finally:
        store.close()


def test_a_function_defined_elsewhere_keeps_only_its_external_edge(repo):
    store = build(repo)
    try:
        assert not any(state == "Remote" for state, _r, _t in links(store))
    finally:
        store.close()


def test_the_resource_can_be_asked_who_runs_it(repo):
    store = build(repo)
    store.close()
    reply = query_graph(
        pattern="references_to", target="resource.aws_lambda_function.order_validator",
        repo_root=str(repo),
    )
    assert reply["status"] == "ok", reply
    assert [r["name"] for r in reply["results"]] == ["Validate"]


def test_names_match_whatever_the_case_and_separators(repo):
    (repo / "main.tf").write_text(
        'resource "aws_lambda_function" "OrderValidator" {}\n'
        'resource "aws_ecs_task_definition" "report-job" {}\n'
    )
    store = build(repo)
    try:
        assert {(s, r.rsplit(".", 1)[-1]) for s, r, _t in links(store)} == {
            ("Validate", "OrderValidator"), ("Report", "report-job"),
        }
    finally:
        store.close()


def test_a_name_two_resources_share_is_not_linked(repo):
    (repo / "extra.tf").write_text('resource "aws_lambda_function" "order-validator" {}\n')
    store = build(repo)
    try:
        assert {s for s, _r, _t in links(store)} == {"Enrich", "Report"}
        assert resolve_asl_local_targets(store)["ambiguous"] == 1
    finally:
        store.close()


def test_only_the_matching_resource_type_counts(repo):
    (repo / "main.tf").write_text(
        'resource "aws_lambda_function" "report_job" {}\n'
        'resource "aws_ecs_task_definition" "order_validator" {}\n'
    )
    store = build(repo)
    try:
        assert links(store) == set()
    finally:
        store.close()


def test_running_the_pass_again_changes_nothing(repo):
    store = build(repo)
    try:
        before = links(store)
        edges = len(store.get_all_edges())
        assert resolve_asl_local_targets(store)["links"] == 3
        assert links(store) == before and len(store.get_all_edges()) == edges
    finally:
        store.close()


def test_a_terraform_only_update_gives_what_a_full_build_gives(repo):
    store = build(repo)
    try:
        (repo / "main.tf").write_text(
            TERRAFORM.replace("order_enricher", "enricher_renamed")
        )
        incremental_update(repo, store, changed_files=["main.tf"])
        updated = links(store)
        assert {s for s, _r, _t in updated} == {"Validate", "Report"}
    finally:
        store.close()
    fresh = build(repo)
    try:
        assert links(fresh) == updated
    finally:
        fresh.close()


def test_a_state_machine_only_update_gives_what_a_full_build_gives(repo):
    store = build(repo)
    try:
        edited = json.loads(json.dumps(MACHINE))
        del edited["States"]["Enrich"]
        edited["States"]["Validate"]["Next"] = "Report"
        (repo / "sfn" / "order.asl.json").write_text(json.dumps(edited))
        incremental_update(repo, store, changed_files=["sfn/order.asl.json"])
        updated = links(store)
        assert {s for s, _r, _t in updated} == {"Validate", "Report"}
    finally:
        store.close()
    fresh = build(repo)
    try:
        assert links(fresh) == updated
    finally:
        fresh.close()


def test_a_graph_without_terraform_or_state_machines_is_untouched(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    store = build(tmp_path)
    try:
        assert resolve_asl_local_targets(store) == {"links": 0, "ambiguous": 0}
    finally:
        store.close()
