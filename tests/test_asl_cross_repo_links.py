"""What Step Functions states call is joined to the registered repo that defines it.

``cross_repo_requests_tool(origin="stepfunction")`` lists each Task target of the registered
repos (a name written in the definition, or one the deploying Terragrunt unit's replacement
maps give a ``${placeholder}``) with the repo that defines that Lambda, ECS task definition,
SNS topic or state machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.asl_resolver import resolve_asl_local_targets
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .asl_links_helpers import FILES, HANDLER, LAMBDA_HCL, MACHINE_PATH, lambda_unit

#: The machines repo: the ``orders`` machine and its units, without the Lambda it runs.
MACHINES = {
    rel: text for rel, text in FILES.items()
    if "python-lambdas" not in rel and rel != "terraform/lambda.hcl"
}
#: The repo that owns the ``validateOrder`` and ``audit-trail`` Lambdas.
LAMBDAS = {
    "terraform/lambda.hcl": LAMBDA_HCL,
    "terraform/python-lambdas/validateOrder/prod/terragrunt.hcl": lambda_unit(
        "validate_order.handler",
    ),
    "python-lambdas/validateOrder/validate_order.py": HANDLER,
    "terraform/python-lambdas/auditFn/prod/terragrunt.hcl": lambda_unit(
        "audit.record", '  function_name = "audit-trail"\n',
    ),
    "python-lambdas/auditFn/audit.py": "def record(event, context):\n    return 1\n",
}
#: Plain Terraform: a topic labelled ``alerts`` and the child machine's definition file.
PLATFORM = {
    "sns.tf": 'resource "aws_sns_topic" "alerts" {\n  name = "x"\n}\n',
    "machines/child-machine.asl.json": (
        '{"StartAt": "Only", "States": {"Only": {"Type": "Succeed"}}}\n'
    ),
}
#: A second repo that also labels a topic ``alerts_dev``, as ``other`` does.
OTHER = {"topics.tf": 'resource "aws_sns_topic" "alerts_dev" {\n  name = "y"\n}\n'}


def make_repo(tmp_path: Path, registry: Registry, name: str, files: dict[str, str]) -> Path:
    root = (tmp_path / name).resolve()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True, exist_ok=True)
    parser = CodeParser(root)
    with GraphStore(directory / "graph.db") as store:
        for rel in files:
            nodes, edges = parser.parse_file(root / rel)
            store.store_file_nodes_edges(str(root / rel), nodes, edges)
        resolve_asl_local_targets(store)
    registry.register(str(root), alias=name)
    return root


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


@pytest.fixture
def repos(tmp_path, registry):
    make_repo(tmp_path, registry, "machines", MACHINES)
    make_repo(tmp_path, registry, "lambdas", LAMBDAS)
    make_repo(tmp_path, registry, "platform", PLATFORM)
    make_repo(tmp_path, registry, "other", OTHER)
    make_repo(tmp_path, registry, "other-two", OTHER)
    return tmp_path


def by_target(reply):
    return {row["target"]: row for row in reply["results"]}


class TestStepFunctionLinks:
    def test_each_target_is_joined_to_the_repo_that_defines_it(self, repos):
        reply = cross_repo_requests_func(origin="stepfunction", repos=["machines"])
        assert reply["status"] == "ok" and reply["origin"] == "stepfunction"
        rows = by_target(reply)
        assert set(rows) == {
            "lambda/validateOrder", "lambda/audit-trail", "sns/alerts", "sns/alerts-dev",
            "states/child-machine",
        }
        validate = rows["lambda/validateOrder"]
        assert validate["status"] == "matched" and validate["target_repo"] == "lambdas"
        assert validate["repo"] == "machines" and validate["file"] == MACHINE_PATH
        assert validate["machine"] == "orders" and validate["state"] == "Validate"
        assert validate["target_kind"] == "lambda" and validate["target_name"] == "validateOrder"
        assert validate["named_by"] == "replacement_vars" and validate["line"] > 0
        # ``function_name`` is the deployed name, not the directory ``auditFn``.
        assert rows["lambda/audit-trail"]["target_repo"] == "lambdas"
        # A Terraform resource label, and a definition file, by the naming convention.
        assert rows["sns/alerts"]["target_repo"] == "platform"
        assert rows["states/child-machine"]["target_repo"] == "platform"
        # Two registered repos label a topic ``alerts_dev``.
        assert rows["sns/alerts-dev"]["status"] == "ambiguous"
        assert rows["sns/alerts-dev"]["candidates"] == ["other", "other-two"]
        assert reply["counts"] == {"matched": 4, "ambiguous": 1}

    def test_a_target_no_registered_repo_defines_is_not_found(self, tmp_path, registry):
        make_repo(tmp_path, registry, "machines", MACHINES)
        reply = cross_repo_requests_func(origin="stepfunction")
        assert reply["counts"] == {"not_found": 5}
        assert all("target_repo" not in row for row in reply["results"])

    def test_a_name_written_in_the_definition_is_listed_too(self, tmp_path, registry):
        make_repo(tmp_path, registry, "literal", {
            "flow.asl.json": (
                '{"StartAt": "Run", "States": {"Run": {"Type": "Task", "End": true, '
                '"Resource": "arn:aws:lambda:us-east-1:111111111111:function:validateOrder"}}}\n'
            ),
        })
        make_repo(tmp_path, registry, "lambdas", LAMBDAS)
        reply = cross_repo_requests_func(origin="stepfunction")
        (row,) = reply["results"]
        assert row["named_by"] == "definition" and row["status"] == "matched"
        assert row["target_repo"] == "lambdas"
        assert "111111111111" not in str(reply)

    def test_filters_by_target_name_kind_and_status(self, repos):
        by_name = cross_repo_requests_func(origin="stepfunction", service="validateOrder")
        assert [r["state"] for r in by_name["results"]] == ["Validate"]
        by_kind = cross_repo_requests_func(origin="stepfunction", route="lambda")
        assert {r["target_name"] for r in by_kind["results"]} == {"validateOrder", "audit-trail"}
        exact = cross_repo_requests_func(origin="stepfunction", route="sns/alerts")
        assert [r["target"] for r in exact["results"]] == ["sns/alerts"]
        ambiguous = cross_repo_requests_func(origin="stepfunction", status="ambiguous")
        assert [r["target"] for r in ambiguous["results"]] == ["sns/alerts-dev"]
        assert ambiguous["counts"] == {"matched": 4, "ambiguous": 1}

    def test_an_unknown_status_or_origin_is_refused(self, repos):
        with pytest.raises(ValueError, match="matched, not_found, ambiguous"):
            cross_repo_requests_func(origin="stepfunction", status="repo_not_registered")
        with pytest.raises(ValueError, match="stepfunction"):
            cross_repo_requests_func(origin="nope")

    def test_the_join_writes_nothing(self, repos):
        db = repos / "machines" / ".code-review-graph" / "graph.db"
        with GraphStore(db, read_only=True) as store:
            before = store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        cross_repo_requests_func(origin="stepfunction")
        with GraphStore(db, read_only=True) as store:
            assert store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before
