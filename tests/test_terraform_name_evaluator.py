"""Terraform names built from variables, locals, wrapper modules and Terragrunt inputs.

The tree imitates resonate/resonate-terraform: ``aws/global/<env>/common.hcl`` sets
``inputs.environment``, the unit ``aws/global/<env>/sns/terragrunt.hcl`` deploys
``modules/global/sns`` whose ``module`` blocks call ``modules/resources/sns`` (a topic named
``var.name``), and ``modules/global/s3/*`` builds a bucket name from a ``locals`` value that reads
``data.aws_caller_identity`` (unknown, so never resolved).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from code_review_graph import terraform_names
from code_review_graph.parser import CodeParser


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    terraform_names.clear_cache()


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def declared(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    _nodes, edges = CodeParser(repo_root=root).parse_bytes(path, path.read_bytes())
    return {
        e.target: e for e in edges
        if e.kind == "REFERENCES" and e.target.startswith("external:aws-")
        and e.extra.get("operation") == "declare"
    }


def sns_tree(root: Path, envs: tuple[str, ...] = ("dev", "prod")) -> None:
    write(root, "modules/resources/sns/sns-main.tf",
          'resource "aws_sns_topic" "this" {\n  name = var.name\n}\n')
    write(root, "modules/resources/sns/sns-variables.tf",
          'variable "name" {\n  type = string\n}\n')
    write(root, "modules/global/sns/variables.tf",
          'variable "environment" {\n  type = string\n}\n')
    write(root, "modules/global/sns/main.tf", '''
module "das_pass" {
  source = "../../resources/sns"
  name   = "das-datapipeline-pass-${var.environment}-topic"
}
module "techops" {
  source = "../../resources/sns"
  name   = "TechOps"
}
module "unknown" {
  source = "../../resources/sns"
  name   = "x-${var.never_declared}"
}
''')
    for env in envs:
        write(root, f"aws/global/{env}/common.hcl",
              f'inputs = {{\n  environment = "{env}"\n}}\n')
        write(root, f"aws/global/{env}/sns/terragrunt.hcl", '''
terraform {
  source = "${get_parent_terragrunt_dir("root")}/modules//global/sns"
}
include {
  path = find_in_parent_folders("common.hcl")
}
include "root" {
  path = find_in_parent_folders("root.hcl")
}
''')
    write(root, "root.hcl", "")


def test_wrapper_module_topic_named_from_the_deploying_units_inputs(tmp_path: Path) -> None:
    sns_tree(tmp_path)
    found = declared(tmp_path, "modules/global/sns/main.tf")
    assert "external:aws-sns:das-datapipeline-pass-dev-topic" in found
    assert "external:aws-sns:das-datapipeline-pass-prod-topic" in found
    assert "external:aws-sns:TechOps" in found
    edge = found["external:aws-sns:das-datapipeline-pass-dev-topic"]
    assert edge.extra["terraform_module"] == "das_pass"
    assert edge.extra["terraform_type"] == "aws_sns_topic"
    assert "terraform_label" not in edge.extra


def test_unknown_input_is_never_written(tmp_path: Path) -> None:
    sns_tree(tmp_path)
    names = list(declared(tmp_path, "modules/global/sns/main.tf"))
    assert not any("never_declared" in n or n.endswith(":x-") for n in names)
    assert len(names) == 3


def test_variable_without_a_value_stays_unresolved(tmp_path: Path) -> None:
    sns_tree(tmp_path, envs=())
    names = list(declared(tmp_path, "modules/global/sns/main.tf"))
    assert names == ["external:aws-sns:TechOps"]


def test_locals_defaults_lower_upper_and_ternary(tmp_path: Path) -> None:
    write(tmp_path, "svc/variables.tf", '''
variable "environment" {
  default = "Prod"
}
variable "region" {
  default = "us-east-1"
}
''')
    write(tmp_path, "svc/main.tf", '''
locals {
  suffix = var.environment == "Prod" ? "" : "-${lower(var.environment)}"
  region = var.region == "us-east-1" ? "" : "-${var.region}"
}
resource "aws_sqs_queue" "jobs" {
  name = "jobs-${lower(var.environment)}${local.suffix}${local.region}"
}
resource "aws_s3_bucket" "data" {
  bucket = upper(local.suffix == "" ? "data" : "other")
}
resource "aws_sqs_queue" "dyn" {
  name = "q-${data.aws_caller_identity.current.account_id}"
}
''')
    found = declared(tmp_path, "svc/main.tf")
    assert "external:aws-sqs:jobs-prod" in found
    assert found["external:aws-sqs:jobs-prod"].extra["terraform_label"] == "jobs"
    assert "external:aws-s3:DATA" in found
    assert len(found) == 2


def test_input_overrides_default_and_unreadable_input_blocks_default(tmp_path: Path) -> None:
    write(tmp_path, "m/main.tf", '''
variable "env" {
  default = "dev"
}
resource "aws_sqs_queue" "q" {
  name = "q-${var.env}"
}
''')
    write(tmp_path, "u/terragrunt.hcl", '''
terraform {
  source = "../m"
}
inputs = {
  env = get_env("STAGE")
}
''')
    assert declared(tmp_path, "m/main.tf") == {}
    write(tmp_path, "u/terragrunt.hcl", '''
terraform {
  source = "../m"
}
inputs = {
  env = "qa"
}
''')
    terraform_names.clear_cache()
    assert list(declared(tmp_path, "m/main.tf")) == ["external:aws-sqs:q-qa"]


def test_bucket_named_from_an_account_dependent_local_is_not_resolved(tmp_path: Path) -> None:
    write(tmp_path, "modules/resources/s3_bucket/main.tf",
          'resource "aws_s3_bucket" "this" {\n  bucket = var.name\n}\n')
    write(tmp_path, "modules/global/s3/main.tf", '''
data "aws_caller_identity" "current" {}
locals {
  region_naming = "-${data.aws_caller_identity.current.account_id}"
}
module "configuration_bucket" {
  source = "../../resources/s3_bucket"
  name   = "resonate-configuration${local.region_naming}"
}
module "fixed_bucket" {
  source = "../../resources/s3_bucket"
  name   = "resonate-${lower("CODEPIPELINE")}"
}
''')
    found = declared(tmp_path, "modules/global/s3/main.tf")
    assert list(found) == ["external:aws-s3:resonate-codepipeline"]
    assert found["external:aws-s3:resonate-codepipeline"].extra["terraform_module"] == (
        "fixed_bucket"
    )


def test_remote_module_source_is_not_followed(tmp_path: Path) -> None:
    write(tmp_path, "main.tf", '''
module "x" {
  source = "git::https://github.com/org/repo.git//modules/sns"
  name   = "never"
}
''')
    assert declared(tmp_path, "main.tf") == {}


def test_ssm_parameter_and_iam_role_names_from_locals_and_unit_inputs(tmp_path: Path) -> None:
    write(tmp_path, "aws/dev/common.hcl", 'inputs = {\n  environment = "dev"\n}\n')
    write(tmp_path, "aws/dev/iam/terragrunt.hcl", '''
terraform {
  source = "${get_parent_terragrunt_dir("root")}/modules//iam"
}
include {
  path = find_in_parent_folders("common.hcl")
}
include "root" {
  path = find_in_parent_folders("root.hcl")
}
''')
    write(tmp_path, "root.hcl", "")
    write(tmp_path, "modules/iam/variables.tf", 'variable "environment" {\n  type = string\n}\n')
    write(tmp_path, "modules/iam/main.tf", '''
locals {
  prefix = "Copy-${upper(var.environment)}"
}
resource "aws_iam_role" "copy" {
  name = "${local.prefix}AssumeRole"
}
resource "aws_ssm_parameter" "slack" {
  name = "/config/${var.environment}/slack_url"
}
resource "aws_ssm_parameter" "unknown" {
  name = "/config/${var.nobody_sets_this}/x"
}
''')
    found = declared(tmp_path, "modules/iam/main.tf")
    assert "external:aws-ssm:/config/dev/slack_url" in found
    assert found["external:aws-ssm:/config/dev/slack_url"].extra["name_evaluated"] is True
    assert found["external:aws-ssm:/config/dev/slack_url"].extra["terraform_label"] == "slack"
    assert not any("nobody_sets_this" in n for n in found)
    assert "external:aws-sts:Copy-DEVAssumeRole" in found
    assert len(found) == 2
