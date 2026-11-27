"""A Task target the Terraform name evaluator can name is joined to the repo that declares it.

Imitates resonate/infrastructure-lambdas-step-functions (``encryptDecryptPGPS3Files``: an SNS
publish to ``TechOps``) and resonate/guardians-step-functions (``flash_panelist``: publishes to
``guardians-pipeline-qa-fail`` and ``datapipeline-pass-qa-topic``), whose topics
resonate/resonate-terraform ``modules/global/sns/main.tf`` declares through
``module "x" { source = "../../resources/sns"  name = ... }``. The module call's label is not the
topic's name, so the label rule could not join them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import terraform_names
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import make_repo
from .test_not_found_reasons import machine
from .test_terraform_name_evaluator import sns_tree


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    terraform_names.clear_cache()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


@pytest.fixture
def world(tmp_path: Path, registry):
    tree = tmp_path / "platform-src"
    sns_tree(tree)
    files = {
        str(p.relative_to(tree)): p.read_text() for p in tree.rglob("*") if p.is_file()
    }
    make_repo(tmp_path, registry, "platform", files)
    make_repo(tmp_path, registry, "machines", {
        "flow.asl.json": machine(
            ("sns", "TechOps"), ("sns", "das-datapipeline-pass-dev-topic"),
            ("sns", "techops-ssm"), ("sns", "techops"),
        ),
    })
    return tmp_path


def rows(reply):
    return {r["target_name"]: r for r in reply["results"]}


def test_a_topic_named_by_a_module_call_is_matched(world) -> None:
    got = rows(cross_repo_requests_func(origin="stepfunction", repos=["machines"]))
    assert got["TechOps"]["status"] == "matched"
    assert got["TechOps"]["target_repo"] == "platform"
    assert got["das-datapipeline-pass-dev-topic"]["status"] == "matched"
    assert got["das-datapipeline-pass-dev-topic"]["target_repo"] == "platform"


def test_an_undeclared_topic_stays_not_found_with_its_reason(world) -> None:
    got = rows(cross_repo_requests_func(origin="stepfunction", repos=["machines"]))
    assert got["techops-ssm"]["status"] == "not_found"
    assert got["techops-ssm"]["not_found_reason"] == "not_declared_in_registry"


def test_the_name_is_matched_exactly_not_by_spelling(world) -> None:
    # ``TechOps`` is declared; ``techops`` differs in case and is a different AWS topic.
    got = rows(cross_repo_requests_func(origin="stepfunction", repos=["machines"]))
    assert got["techops"]["status"] == "not_found"
