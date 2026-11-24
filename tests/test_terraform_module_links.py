"""A remote Terraform module source resolves to the registered repo that holds it.

``cross_repo_requests_tool(origin="terraform")`` joins a Terragrunt ``terraform { source }``
of one repo, written as ``git::ssh://.../org/repo.git//modules/x``, to the repo named
``repo`` and the folder ``modules/x`` in it. Dead-code also leaves runtime-wired names
(hook callbacks, ``exports.handler``, ``local`` values) out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

STACK = '''terraform {
  source = "git::ssh://git@github.com/acme/mod-repo.git//modules/resources/lambda?ref=v1"
}
'''
STACK_TWO = '''terraform {
  source = "git::ssh://git@github.com/acme/mod-repo.git//modules/missing"
}
'''
STACK_THREE = '''terraform {
  source = "git::ssh://git@github.com/acme/not-registered.git//modules/x"
}
'''
MODULE = 'resource "aws_lambda_function" "this" {\n  function_name = "x"\n}\n'


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
    registry.register(str(root), alias=name)
    return root


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


@pytest.fixture
def repos(tmp_path, registry):
    make_repo(tmp_path, registry, "stacks", {
        "a/terragrunt.hcl": STACK,
        "b/terragrunt.hcl": STACK_TWO,
        "c/terragrunt.hcl": STACK_THREE,
    })
    make_repo(tmp_path, registry, "mod-repo", {"modules/resources/lambda/main.tf": MODULE})
    return tmp_path


def rows_by_file(reply):
    return {r["file"]: r for r in reply["results"]}


class TestTerraformLinks:
    def test_each_source_is_resolved_to_a_registered_repo_and_folder(self, repos):
        reply = cross_repo_requests_func(origin="terraform")
        assert reply["status"] == "ok"
        rows = rows_by_file(reply)
        assert rows["a/terragrunt.hcl"]["status"] == "matched"
        assert rows["a/terragrunt.hcl"]["module_repo"] == "mod-repo"
        assert rows["a/terragrunt.hcl"]["module_path"] == "modules/resources/lambda"
        assert rows["a/terragrunt.hcl"]["ref"] == "v1"
        assert rows["b/terragrunt.hcl"]["status"] == "module_not_found"
        assert rows["c/terragrunt.hcl"]["status"] == "repo_not_registered"
        assert reply["counts"] == {
            "matched": 1, "module_not_found": 1, "repo_not_registered": 1,
        }

    def test_filtering_by_module_repo_and_folder_shows_who_uses_a_module(self, repos):
        reply = cross_repo_requests_func(
            origin="terraform", service="mod-repo", route="modules/resources",
        )
        assert [r["file"] for r in reply["results"]] == ["a/terragrunt.hcl"]

    def test_an_unknown_status_is_refused(self, repos):
        with pytest.raises(ValueError):
            cross_repo_requests_func(origin="terraform", status="nope")


class TestDeadCodeLeavesRuntimeWiredNamesOut:
    def test_names_the_runtime_calls_are_not_reported(self):
        from code_review_graph.refactor import _is_runtime_wired

        class N:
            def __init__(self, name, language="python", extra=None):
                self.name, self.language, self.extra = name, language, extra or {}

        assert _is_runtime_wired(N("register_hooks"))
        assert _is_runtime_wired(N("_on_after_tool_call"))
        assert _is_runtime_wired(N("exports.handler", "javascript"))
        assert _is_runtime_wired(N("local.src_path", "hcl", {"hcl_type": "local"}))
        assert not _is_runtime_wired(N("compute_total"))
        assert not _is_runtime_wired(N("local.x", "python"))
