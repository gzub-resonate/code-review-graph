"""A state machine that two repos define: the repo that deploys it owns it.

``project-disrupt`` still holds the definition files (``disrupt_segmentation.asl.json``) of
machines that ``mle-aws-infra`` deploys from its own Terragrunt units (the unit in
``project-disrupt`` says ``skip = true``, "owned by mle-aws-infra"). A call to such a machine
was ``ambiguous`` between the two repos. A repo whose Terragrunt unit or Terraform declaration
names the machine beats a repo that only holds a definition file; the other repo stays visible
(``also_defined_in``, ``also_defined_via``) and the row says why (``tie_break``). Two repos that
both deploy it, or that both only hold the file, stay ``ambiguous``. Read-side only: nothing
here changes what a build writes.
"""

from __future__ import annotations

import pytest

from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import MACHINES, make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401

APP = {
    "svc/launch.py": (
        "import boto3\nsfn = boto3.client('stepfunctions')\n\n\ndef go():\n"
        "    sfn.start_execution(\n"
        "        stateMachineArn='arn:aws:states:us-east-1:1:stateMachine:orders')\n"
    ),
}
ASL_ONLY = {
    "step-functions/orders/statemachine/orders.asl.json": MACHINES[
        "step-functions/orders/statemachine/orders.asl.json"
    ],
}


def _row(**kwargs):
    reply = cross_repo_requests_func(
        origin="awssdk", repos=["app"], route="states/orders", **kwargs,
    )
    assert reply["status"] == "ok"
    rows = [r for r in reply["results"] if r["operation"] == "start_execution"]
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
def world(tmp_path, registry):
    make_repo(tmp_path, registry, "app", APP)
    return tmp_path, registry


def test_the_deploying_repo_beats_the_repo_with_only_the_definition_file(world) -> None:
    tmp_path, registry = world
    make_repo(tmp_path, registry, "infra", MACHINES)
    make_repo(tmp_path, registry, "legacy", ASL_ONLY)
    row = _row()
    assert row["status"] == "matched"
    assert row["target_repo"] == "infra"
    assert row["also_defined_in"] == ["legacy"]
    assert row["also_defined_via"] == {"legacy": "state_machine_file"}
    assert row["tie_break"] == "deployed_declaration_over_definition_file"
    assert "candidates" not in row


def test_two_repos_that_both_deploy_it_stay_ambiguous(world) -> None:
    tmp_path, registry = world
    make_repo(tmp_path, registry, "infra", MACHINES)
    make_repo(tmp_path, registry, "infra-copy", MACHINES)
    row = _row()
    assert row["status"] == "ambiguous"
    assert sorted(row["candidates"]) == ["infra", "infra-copy"]
    assert "also_defined_in" not in row and "tie_break" not in row


def test_two_repos_that_both_only_hold_the_file_stay_ambiguous(world) -> None:
    tmp_path, registry = world
    make_repo(tmp_path, registry, "legacy", ASL_ONLY)
    make_repo(tmp_path, registry, "legacy-copy", ASL_ONLY)
    row = _row()
    assert row["status"] == "ambiguous"
    assert sorted(row["candidates"]) == ["legacy", "legacy-copy"]


def test_a_single_owner_is_unchanged(world) -> None:
    tmp_path, registry = world
    make_repo(tmp_path, registry, "legacy", ASL_ONLY)
    row = _row()
    assert row["status"] == "matched" and row["target_repo"] == "legacy"
    assert "also_defined_in" not in row and "tie_break" not in row
