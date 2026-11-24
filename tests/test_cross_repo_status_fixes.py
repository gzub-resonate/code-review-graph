"""Registry module sources, external actions and trimmed fuzzy rows in the cross-repo tools."""

from __future__ import annotations

import pytest

from code_review_graph.tools import registry_tools
from code_review_graph.tools.registry_tools import (
    cross_repo_requests_func,
    cross_repo_search_func,
)

from .test_cross_repo_ranking_budget import named_repo
from .test_cross_repo_ranking_budget import registry as registry  # noqa: F401
from .test_terraform_module_links import MODULE, STACK
from .test_terraform_module_links import make_repo as make_tf_repo
from .test_workflow_links import ACTION, TEMPLATE
from .test_workflow_links import make_repo as make_wf_repo

REGISTRY_STACK = '''module "sftp" {
  source  = "cloudposse/transfer-sftp/aws"
  version = "1.3.0"
}
module "runners" {
  source = "registry.terraform.io/philips-labs/github-runner/aws"
}
'''
CALLER = """name: ci
on: push
jobs:
  build:
    uses: acme/workflow-templates/.github/workflows/ci.yml@v1
  missing:
    uses: acme/not-indexed/.github/workflows/x.yml@main
  local:
    uses: ./.github/workflows/other.yml
  steps-job:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
      - uses: someorg/custom-thing@v1
      - uses: docker://alpine:3.19
      - uses: ./.github/actions/local
"""


def by_name(reply, key):
    return {r[key]: r for r in reply["results"]}


class TestTerraformRegistryModules:
    @pytest.fixture
    def repos(self, tmp_path, registry):  # noqa: F811
        make_tf_repo(tmp_path, registry, "stacks", {
            "a/terragrunt.hcl": STACK,
            "r/main.tf": REGISTRY_STACK,
        })
        make_tf_repo(tmp_path, registry, "mod-repo", {"modules/resources/lambda/main.tf": MODULE})
        # A registered repo named like the registry module name must not capture it.
        make_tf_repo(tmp_path, registry, "transfer-sftp", {"x/main.tf": MODULE})

    def test_registry_source_is_classified_and_git_source_still_resolves(self, repos):
        reply = cross_repo_requests_func(origin="terraform", repos=["stacks"])
        rows = by_name(reply, "file")
        git = rows["a/terragrunt.hcl"]
        assert git["status"] == "matched"
        assert git["module_repo"] == "mod-repo"
        registry_rows = [r for r in reply["results"] if r["status"] == "registry_module"]
        assert {r["module_repo"] for r in registry_rows} == {"transfer-sftp", "github-runner"}
        sftp = next(r for r in registry_rows if r["module_repo"] == "transfer-sftp")
        assert sftp["file"] == "r/main.tf"

    def test_status_filter_selects_registry_modules(self, repos):
        reply = cross_repo_requests_func(
            origin="terraform", repos=["stacks"], status="registry_module",
        )
        assert reply["total"] == 2
        assert {r["status"] for r in reply["results"]} == {"registry_module"}


class TestExternalActions:
    @pytest.fixture
    def repos(self, tmp_path, registry):  # noqa: F811
        make_wf_repo(tmp_path, registry, "app", {".github/workflows/ci.yml": CALLER})
        make_wf_repo(tmp_path, registry, "workflow-templates", {
            ".github/workflows/ci.yml": TEMPLATE,
            ".github/actions/setup/action.yml": ACTION,
        })

    def test_third_party_is_external_and_same_org_is_not_registered(self, repos):
        reply = cross_repo_requests_func(origin="workflow", repos=["app"])
        rows = by_name(reply, "target_repo")
        assert rows["workflow-templates"]["status"] == "matched"
        assert rows["not-indexed"]["status"] == "repo_not_registered"
        assert rows["not-indexed"]["target_owner"] == "acme"
        for name, owner in (
            ("checkout", "actions"),
            ("configure-aws-credentials", "aws-actions"),
            ("custom-thing", "someorg"),
        ):
            assert rows[name]["status"] == "external_action"
            assert rows[name]["target_owner"] == owner
        assert reply["total"] == 5  # ./ and docker:// references stay omitted

    def test_status_filter_selects_external_actions(self, repos):
        reply = cross_repo_requests_func(
            origin="workflow", repos=["app"], status="external_action",
        )
        assert reply["total"] == 3
        assert {r["status"] for r in reply["results"]} == {"external_action"}


def _rows(*names_tiers):
    return [
        (rank, tier, {"name": name, "qualified_name": f"f.py::{name}", "kind": "Class"})
        for rank, (name, tier) in enumerate(names_tiers)
    ]


class TestFuzzyTrim:
    WITH_EXACT = (
        ("AttributeFilter", "exact"), ("AttributeFilterX", "prefix"),
        ("Attribute", "fuzzy"), ("BooleanFilterProps", "fuzzy"), ("attrFilter", "tokens"),
    )
    WITHOUT_EXACT = (("AttributeFilterX", "prefix"), ("Attribute", "fuzzy"), ("Boolean", "tokens"))

    @pytest.fixture(autouse=True)
    def repo(self, tmp_path, registry):  # noqa: F811
        named_repo(tmp_path, registry, "a", ["x"])

    def search(self, monkeypatch, rows, **args):
        monkeypatch.setattr(
            registry_tools, "_search_repo", lambda *_a, **_k: iter(_rows(*rows)),
        )
        args.setdefault("max_results", 100)
        reply = cross_repo_search_func(query="AttributeFilter", **args)
        assert reply["status"] == "ok", reply
        return reply

    def test_weak_rows_are_dropped_when_an_exact_match_exists(
        self, monkeypatch,
    ):
        full = self.search(monkeypatch, self.WITH_EXACT, include_fuzzy=True)
        assert full["total"] == 5
        assert full["fuzzy_dropped"] == 0
        assert "hidden" not in full["summary"]

        trimmed = self.search(monkeypatch, self.WITH_EXACT)
        assert [r["match"] for r in trimmed["results"]] == ["exact", "prefix"]
        assert trimmed["fuzzy_dropped"] == 3
        assert trimmed["total"] == 2 == len(trimmed["results"])
        assert "3 fuzzy/tokens row(s) hidden" in trimmed["summary"]

    def test_fuzzy_rows_are_kept_when_nothing_matches_exactly(
        self, monkeypatch,
    ):
        default = self.search(monkeypatch, self.WITHOUT_EXACT)
        assert default["fuzzy_dropped"] == 0
        assert default["total"] == 3
        assert [r["match"] for r in default["results"]] == ["prefix", "tokens", "fuzzy"]
