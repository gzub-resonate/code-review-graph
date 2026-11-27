"""``not_found`` rows of the ``awssdk`` and ``stepfunction`` origins say why (``not_found_reason``).

``status`` and ``counts`` keep their old values; ``counts_by_reason`` and the summary add the
split, and ``status=<reason>`` selects those rows.
"""

from __future__ import annotations

import pytest

from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import make_repo


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def machine(*targets: tuple[str, str]) -> str:
    """A state machine whose Task states call ``(service, name)`` through literal ARNs."""
    import json

    states = {}
    for i, (service, name) in enumerate(targets):
        state: dict = {"Type": "Task"}
        if service == "lambda":
            state["Resource"] = f"arn:aws:lambda:us-east-1:111111111111:function:{name}"
        elif service == "sns":
            state["Resource"] = "arn:aws:states:::sns:publish"
            state["Parameters"] = {"TopicArn": f"arn:aws:sns:us-east-1:111111111111:{name}"}
        else:
            state["Resource"] = "arn:aws:states:::ecs:runTask.sync"
            state["Parameters"] = {
                "Cluster": f"arn:aws:ecs:us-east-1:111111111111:cluster/{name}",
                "TaskDefinition": name,
            }
        if i == len(targets) - 1:
            state["End"] = True
        else:
            state["Next"] = f"S{i + 1}"
        states[f"S{i}"] = state
    return json.dumps({"StartAt": "S0", "States": states})


def reasons(reply):
    return {r["target_name"]: r.get("not_found_reason") for r in reply["results"]}


class TestStepFunctionReasons:
    @pytest.fixture
    def world(self, tmp_path, registry):
        make_repo(tmp_path, registry, "machines", {
            "flow.asl.json": machine(
                ("sns", "datapipeline-fail-qa-topic"), ("sns", "TechOps"),
                ("lambda", "analyticsCanary"), ("sns", "nowhere-topic"),
                ("sns", "plain-topic"),
            ),
        })
        make_repo(tmp_path, registry, "infra", {
            "sns.tf": (
                'data "aws_sns_topic" "datapipeline_fail_qa_topic" {\n  name = "x"\n}\n'
                'module "techops" {\n  source = "./sns"\n  name = "TechOps"\n}\n'
                'resource "aws_sns_topic" "plain_topic" {\n  name = "p"\n}\n'
            ),
            "python-lambdas/analyticsCanary/canary.py": "def handler(e, c):\n    return 1\n",
        })

    def test_each_reason(self, world):
        reply = cross_repo_requests_func(origin="stepfunction", max_results=100)
        got = reasons(reply)
        assert got["datapipeline-fail-qa-topic"] == "data_source_only"
        assert got["TechOps"] == "declared_via_module"
        assert got["analyticsCanary"] == "source_without_unit"
        assert got["nowhere-topic"] == "not_declared_in_registry"
        assert got["plain-topic"] is None  # matched: no reason
        assert reply["counts"] == {"not_found": 4, "matched": 1}
        assert reply["counts_by_reason"] == {
            "data_source_only": 1, "declared_via_module": 1, "source_without_unit": 1,
            "not_declared_in_registry": 1,
        }
        assert "4 not found (" in reply["summary"]
        assert all(r["status"] == "not_found" for r in reply["results"] if "not_found_reason" in r)

    def test_status_filter_accepts_a_reason(self, world):
        reply = cross_repo_requests_func(
            origin="stepfunction", status="data_source_only", max_results=100,
        )
        assert [r["target_name"] for r in reply["results"]] == ["datapipeline-fail-qa-topic"]
        assert reply["counts"] == {"not_found": 4, "matched": 1}
        old = cross_repo_requests_func(origin="stepfunction", status="not_found")
        assert old["total"] == 4

    def test_environment_cluster(self, tmp_path, registry):
        make_repo(tmp_path, registry, "m", {
            "f.asl.json": machine(("ecs", "prodB")),
        })
        reply = cross_repo_requests_func(origin="stepfunction")
        (row,) = reply["results"]
        assert row["status"] == "not_found"
        assert row["not_found_reason"] == "environment_cluster"

    def test_unknown_status_message_lists_reasons(self, world):
        with pytest.raises(ValueError, match="data_source_only"):
            cross_repo_requests_func(origin="stepfunction", status="bogus")

    def test_no_not_found_rows_means_no_counts_by_reason(self, tmp_path, registry):
        make_repo(tmp_path, registry, "m", {"f.asl.json": machine(("sns", "plain-topic"))})
        make_repo(tmp_path, registry, "i", {
            "s.tf": 'resource "aws_sns_topic" "plain_topic" {\n  name = "p"\n}\n',
        })
        reply = cross_repo_requests_func(origin="stepfunction")
        assert reply["counts"] == {"matched": 1}
        assert "counts_by_reason" not in reply

    def test_hint_for_a_state_machine_name(self, world):
        reply = cross_repo_requests_func(origin="stepfunction", service="flow")
        assert reply["total"] == 0 and "origin=awssdk" in reply["hint"]
        other = cross_repo_requests_func(origin="stepfunction", service="not-a-machine")
        assert "hint" not in other


APP = """\
import boto3

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
ssm.get_parameter(Name="/app/setting")
boto3.client("secretsmanager").get_secret_value(SecretId="app/secret")
s3.get_object(Bucket="mock-bucket", Key="k")
s3.get_object(Bucket="data-bucket", Key="k")
s3.get_object(Bucket="lonely-bucket", Key="k")
"""


class TestAwsSdkReasons:
    def build(self, tmp_path, registry, infra):
        make_repo(tmp_path, registry, "app", {"app.py": APP})
        if infra is not None:
            make_repo(tmp_path, registry, "infra", infra)

    def test_no_owner_lookup_and_test_double_and_plain_miss(self, tmp_path, registry):
        self.build(tmp_path, registry, None)
        reply = cross_repo_requests_func(origin="awssdk", max_results=100)
        got = reasons(reply)
        assert got["app/secret"] == "no_owner_lookup"
        assert got["/app/setting"] == "not_declared_in_registry"
        assert got["mock-bucket"] == "test_double"
        assert got["lonely-bucket"] == "not_declared_in_registry"
        assert set(reply["counts"]) == {"not_found"}
        assert sum(reply["counts_by_reason"].values()) == reply["counts"]["not_found"]

    def test_dynamic_declaration_marks_the_miss(self, tmp_path, registry):
        self.build(tmp_path, registry, {
            "b.tf": (
                'resource "aws_s3_bucket" "data" {\n  bucket = "${var.env}-data"\n}\n'
            ),
        })
        got = reasons(cross_repo_requests_func(origin="awssdk", max_results=100))
        assert got["lonely-bucket"] == "declared_name_dynamic"
        assert got["data-bucket"] == "declared_name_dynamic"
        assert got["mock-bucket"] == "test_double"
        assert got["app/secret"] == "no_owner_lookup"

    def test_wrapper_module_marks_the_miss(self, tmp_path, registry):
        self.build(tmp_path, registry, {
            "m.tf": 'module "data" {\n  source = "../modules/s3_bucket"\n  name = var.n\n}\n',
        })
        got = reasons(cross_repo_requests_func(origin="awssdk", max_results=100))
        assert got["lonely-bucket"] == "declared_name_dynamic"

    def test_literal_declaration_is_not_dynamic(self, tmp_path, registry):
        self.build(tmp_path, registry, {
            "b.tf": 'resource "aws_s3_bucket" "d" {\n  bucket = "data-bucket"\n}\n',
        })
        reply = cross_repo_requests_func(origin="awssdk", max_results=100)
        got = reasons(reply)
        assert got["data-bucket"] is None
        assert got["lonely-bucket"] == "not_declared_in_registry"

    def test_status_filter_accepts_a_reason_and_old_keys_stay(self, tmp_path, registry):
        self.build(tmp_path, registry, None)
        reply = cross_repo_requests_func(
            origin="awssdk", status="no_owner_lookup", max_results=100,
        )
        assert [r["target_name"] for r in reply["results"]] == ["app/secret"]
        assert reply["counts"]["not_found"] == 5
        old = cross_repo_requests_func(origin="awssdk", status="not_found", max_results=100)
        assert old["total"] == 5
        assert all(r["status"] == "not_found" for r in old["results"])


    def test_declared_ssm_parameter_and_role_are_matched_not_missed(self, tmp_path, registry):
        make_repo(tmp_path, registry, "app", {"app.py": (
            "import boto3\n"
            'boto3.client("ssm").get_parameter(Name="/app/setting")\n'
            'boto3.client("sts").assume_role(\n'
            '    RoleArn="arn:aws:iam::123456789012:role/CopyDevAssumeRole", RoleSessionName="s")\n'
        )})
        make_repo(tmp_path, registry, "infra", {"p.tf": (
            'resource "aws_ssm_parameter" "s" {\n  name = "/app/setting"\n}\n'
            'resource "aws_iam_role" "r" {\n  name = "CopyDevAssumeRole"\n}\n'
        )})
        reply = cross_repo_requests_func(origin="awssdk", repos=["app"], max_results=100)
        status = {r["target_name"]: r["status"] for r in reply["results"]}
        assert status["/app/setting"] == "matched"
        assert status["CopyDevAssumeRole"] == "matched"
