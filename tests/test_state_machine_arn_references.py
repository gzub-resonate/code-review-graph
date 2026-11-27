"""A module constant that names a state machine is joined to the machine's definition.

``project-disrupt`` (``mcp/tools/disrupt/_env.py``) holds ``_PROD_NBC_STATE_MACHINE_ARN =
'arn:aws:states:us-east-1:694585954309:stateMachine:disrupt-next-best-customer'`` and its
launchers start it through ``_sfn.get_client().start_execution(stateMachineArn=<parameter>)``.
The definition is ``step_functions/disrupt/disrupt_next_best_customer/statemachine/
disrupt_next_best_customer.asl.json`` in ``mle-aws-infra``. Before, the leading underscore kept the
constant out of the graph and no call edge carried the name, so the ``awssdk`` origin had no row
for the machine.
"""

# ruff: noqa: E501  (the fixture reproduces the one-line constants of project-disrupt)
from __future__ import annotations

import pytest

from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import make_repo, registry  # noqa: F401

ENV_PY = """\
_DEV_NBC_STATE_MACHINE_ARN = 'arn:aws:states:us-east-1:126345656468:stateMachine:disrupt-next-best-customer'
_PROD_NBC_STATE_MACHINE_ARN = 'arn:aws:states:us-east-1:694585954309:stateMachine:disrupt-next-best-customer'
_PROD_ETL_STATE_MACHINE_ARN = 'arn:aws:states:us-east-1:694585954309:stateMachine:disrupt-etl-2'
_PROD_DECILE_FILTER_LAMBDA = 'arn:aws:lambda:us-east-1:694585954309:function:disrupt-decile-filter'
ORPHAN_STATE_MACHINE_NAME = 'not-defined-anywhere'


def default_next_best_customer_state_machine_arn() -> str:
    return _PROD_NBC_STATE_MACHINE_ARN
"""
ASL = '{"StartAt": "Only", "States": {"Only": {"Type": "Succeed"}}}\n'
INFRA = {
    "step_functions/disrupt/disrupt_next_best_customer/statemachine/"
    "disrupt_next_best_customer.asl.json": ASL,
}


@pytest.fixture
def repos(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "project-disrupt", {"mcp/tools/disrupt/_env.py": ENV_PY})
    make_repo(tmp_path, registry, "mle-aws-infra", INFRA)
    return tmp_path


def _rows(**kwargs):
    reply = cross_repo_requests_func(origin="awssdk", repos=["project-disrupt"], **kwargs)
    return reply, {(r["file"], r["line"]): r for r in reply["results"]}


def test_the_arn_constant_is_joined_to_the_machine_defined_in_another_repo(repos) -> None:
    reply, _ = _rows(route="states/disrupt_next_best_customer")
    assert reply["status"] == "ok"
    rows = reply["results"]
    assert len(rows) == 2  # the dev and the prod constant
    for row in rows:
        assert row["status"] == "matched"
        assert row["target_repo"] == "mle-aws-infra"
        assert row["target"] == "states/disrupt-next-best-customer"
        assert row["operation"] == "arn_reference"
        assert row["sdk"] == "arn-constant"
        assert row["evidence"] == "state_machine_arn_constant"
        assert row["access"] == "unknown"
    assert {r["constant"] for r in rows} == {
        "_DEV_NBC_STATE_MACHINE_ARN", "_PROD_NBC_STATE_MACHINE_ARN",
    }


def test_a_machine_nobody_registered_says_why_it_is_not_found(repos) -> None:
    _, rows = _rows(route="states/disrupt-etl-2")
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["status"] == "not_found"
    assert row["not_found_reason"] == "not_declared_in_registry"


def test_a_name_constant_of_a_state_machine_is_a_reference_too(repos) -> None:
    _, rows = _rows(route="states/not-defined-anywhere")
    row = next(iter(rows.values()))
    assert row["evidence"] == "state_machine_name_constant"
    assert row["status"] == "not_found"


def test_a_lambda_arn_constant_is_not_a_state_machine_reference(repos) -> None:
    reply, _ = _rows(route="states")
    targets = {r["target"] for r in reply["results"]}
    assert "states/disrupt-decile-filter" not in targets


def test_the_deployed_spelling_finds_the_same_rows(repos) -> None:
    by_route, _ = _rows(route="states/disrupt-next-best-customer")
    by_service, _ = _rows(service="disrupt_next_best_customer")
    assert len(by_route["results"]) == 2
    assert len(by_service["results"]) == 2
