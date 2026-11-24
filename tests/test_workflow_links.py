"""A GitHub Actions ``uses`` reference resolves to the registered repo that holds it.

``cross_repo_requests_tool(origin="workflow")`` joins ``uses: org/repo/.github/workflows/x.yml@v1``
of one repo to the repo named ``repo`` and the file in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

CALLER = """name: ci
on: push
jobs:
  build:
    uses: acme/workflow-templates/.github/workflows/ci.yml@v1
  missing:
    uses: acme/workflow-templates/.github/workflows/nope.yml@v2
  outside:
    uses: acme/not-registered/.github/workflows/x.yml@main
  local:
    uses: ./.github/workflows/other.yml
  steps-job:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: acme/workflow-templates/.github/actions/setup@v3
      - uses: docker://alpine:3.19
      - uses: ./.github/actions/local
"""
TEMPLATE = (
    "name: ci\non:\n  workflow_call:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo hi\n"
)
ACTION = "name: setup\nruns:\n  using: composite\n  steps:\n    - run: echo hi\n      shell: bash\n"


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
    make_repo(tmp_path, registry, "app", {".github/workflows/ci.yml": CALLER})
    make_repo(tmp_path, registry, "workflow-templates", {
        ".github/workflows/ci.yml": TEMPLATE,
        ".github/actions/setup/action.yml": ACTION,
    })
    return tmp_path


def by_target(reply):
    return {(r["target_repo"], r["target_path"]): r for r in reply["results"]}


class TestWorkflowLinks:
    def test_each_reference_is_resolved(self, repos):
        reply = cross_repo_requests_func(origin="workflow", repos=["app"])
        assert reply["status"] == "ok"
        assert reply["origin"] == "workflow"
        rows = by_target(reply)
        wf = rows[("workflow-templates", ".github/workflows/ci.yml")]
        assert wf["status"] == "matched"
        assert wf["repo"] == "app"
        assert wf["file"] == ".github/workflows/ci.yml"
        assert wf["ref"] == "v1"
        assert wf["kind"] == "reusable_workflow"
        assert isinstance(wf["line"], int)
        assert rows[("workflow-templates", ".github/workflows/nope.yml")]["status"] == (
            "path_not_found"
        )
        assert rows[("not-registered", ".github/workflows/x.yml")]["status"] == (
            "repo_not_registered"
        )
        action = rows[("workflow-templates", ".github/actions/setup")]
        assert action["status"] == "matched"
        assert action["kind"] == "action"
        assert rows[("checkout", "")]["status"] == "external_action"

    def test_local_and_docker_references_are_ignored(self, repos):
        reply = cross_repo_requests_func(origin="workflow", repos=["app"])
        assert reply["total"] == 5
        for row in reply["results"]:
            assert not row["target_path"].startswith(".github/workflows/other")
            assert "alpine" not in row["target_repo"]
            assert "local" not in row["target_path"]

    def test_filters_show_who_uses_a_workflow(self, repos):
        reply = cross_repo_requests_func(
            origin="workflow", service="workflow-templates", route=".github/workflows",
        )
        assert {r["target_path"] for r in reply["results"]} == {
            ".github/workflows/ci.yml", ".github/workflows/nope.yml",
        }
        only = cross_repo_requests_func(origin="workflow", status="matched")
        assert only["total"] == 2
        assert only["counts"]["repo_not_registered"] == 1
        assert only["counts"]["external_action"] == 1

    def test_ambiguous_names_are_reported(self, tmp_path, repos, registry):
        make_repo(tmp_path / "other", registry, "workflow-templates", {"x.txt": "x"})
        reply = cross_repo_requests_func(origin="workflow", repos=["app"])
        statuses = {
            r["status"] for r in reply["results"] if r["target_repo"] == "workflow-templates"
        }
        assert statuses == {"ambiguous"}

    def test_an_unknown_status_is_refused(self, repos):
        with pytest.raises(ValueError):
            cross_repo_requests_func(origin="workflow", status="module_not_found")
        with pytest.raises(ValueError):
            cross_repo_requests_func(origin="nope")
