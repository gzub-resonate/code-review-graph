"""Reference-shaped strings in infrastructure files become external IMPORTS_FROM edges.

Covers GitHub Actions ``uses:`` references, Dockerfile images and Terragrunt sources. The
graph stays value-free: only the allowlisted reference strings are stored, never ``env``,
``with``, ``secrets``, ``ARG``/``ENV`` values or Terragrunt ``inputs``. Names are synthetic.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.infra_references import (
    parse_docker_image,
    parse_github_uses,
)
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph

SECRET = "s3cr3t-planted-value-9f2a"


def _parse(
    root: Path, rel: str, text: str,
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return CodeParser(repo_root=root).parse_bytes(path, path.read_bytes())


def _imports(edges: list[EdgeInfo]) -> list[EdgeInfo]:
    return [e for e in edges if e.kind == "IMPORTS_FROM"]


def _dump(nodes: list[NodeInfo], edges: list[EdgeInfo]) -> str:
    return json.dumps(
        [asdict(n) for n in nodes] + [asdict(e) for e in edges], sort_keys=True, default=str,
    )


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------

WORKFLOW = f"""\
name: ci
on: push
env:
  TOKEN: {SECRET}
jobs:
  build:
    uses: acme/shared-workflows/.github/workflows/build.yml@main
    with:
      api-key: {SECRET}
    secrets:
      token: {SECRET}
  test:
    runs-on: ubuntu-latest
    env:
      PASSWORD: {SECRET}
    steps:
      - uses: actions/checkout@v4
      - uses: acme/setup-tool@0123456789abcdef0123456789abcdef01234567
        with:
          secret: {SECRET}
      - uses: github/codeql-action/init@v3
      - run: echo {SECRET}
"""


def test_parse_github_uses_parts() -> None:
    parts = parse_github_uses("acme/shared-workflows/.github/workflows/b.yml@v1", "job")
    assert parts is not None
    assert parts["kind"] == "reusable_workflow"
    assert parts["owner_repo"] == "acme/shared-workflows"
    assert parts["path"] == ".github/workflows/b.yml"
    assert parts["ref"] == "v1" and parts["ref_type"] == "ref"
    step = parse_github_uses("Acme/Setup-Tool@" + "a" * 40, "step")
    assert step is not None
    assert step["kind"] == "action" and step["owner_repo"] == "acme/setup-tool"
    assert step["ref_type"] == "sha"
    assert parse_github_uses("justonepart", "step") is None
    assert parse_github_uses("acme/x\n@v1", "step") is None
    assert parse_github_uses("a" * 600, "step") is None


def test_workflow_uses_become_external_edges(tmp_path: Path) -> None:
    _nodes, edges = _parse(tmp_path, ".github/workflows/ci.yml", WORKFLOW)
    file_path = (tmp_path / ".github/workflows/ci.yml").as_posix()
    got = [(e.target, e.line, e.extra["kind"], e.extra.get("ref")) for e in _imports(edges)]
    assert got == [
        (
            "external:github-actions:acme/shared-workflows/.github/workflows/build.yml",
            7, "reusable_workflow", "main",
        ),
        ("external:github-actions:actions/checkout", 17, "action", "v4"),
        (
            "external:github-actions:acme/setup-tool", 18, "action",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        ("external:github-actions:github/codeql-action/init", 21, "action", "v3"),
    ]
    assert {e.source for e in _imports(edges)} == {file_path}
    first = _imports(edges)[0].extra
    assert first["resolution"] == "github_actions_uses"
    assert first["owner_repo"] == "acme/shared-workflows"
    assert first["job"] == "build" and first["scope"] == "job"
    sha = _imports(edges)[2].extra
    assert sha["ref_type"] == "sha"


def test_workflow_file_node_lists_references(tmp_path: Path) -> None:
    nodes, _edges = _parse(tmp_path, ".github/workflows/ci.yml", WORKFLOW)
    file_node = next(n for n in nodes if n.kind == "File")
    uses = file_node.extra["github_actions_uses"]
    assert [u["raw"] for u in uses][:2] == [
        "acme/shared-workflows/.github/workflows/build.yml@main",
        "actions/checkout@v4",
    ]
    assert uses[0]["line"] == 7


def test_no_value_from_env_with_or_secrets_is_stored(tmp_path: Path) -> None:
    nodes, edges = _parse(tmp_path, ".github/workflows/ci.yml", WORKFLOW)
    assert SECRET not in _dump(nodes, edges)


def test_local_references_resolve_to_files_in_repo(tmp_path: Path) -> None:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/other.yml").write_text("on: push\njobs: {}\n")
    (tmp_path / ".github/actions/setup").mkdir(parents=True)
    (tmp_path / ".github/actions/setup/action.yml").write_text(
        "runs:\n  using: composite\n  steps: []\n",
    )
    _nodes, edges = _parse(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n"
        "  a:\n    uses: ./.github/workflows/other.yml\n"
        "  b:\n    steps:\n"
        "      - uses: ./.github/actions/setup\n"
        "      - uses: ./.github/actions/missing\n",
    )
    targets = [e.target for e in _imports(edges)]
    assert targets == [
        (tmp_path / ".github/workflows/other.yml").as_posix(),
        (tmp_path / ".github/actions/setup/action.yml").as_posix(),
        "./.github/actions/missing",
    ]
    assert [e.extra["kind"] for e in _imports(edges)] == ["local"] * 3


def test_composite_action_steps_are_read(tmp_path: Path) -> None:
    _nodes, edges = _parse(
        tmp_path,
        ".github/actions/build/action.yml",
        "name: build\nruns:\n  using: composite\n  steps:\n"
        f"    - uses: acme/setup-tool@v2\n      with:\n        key: {SECRET}\n"
        "    - run: echo hi\n      shell: bash\n",
    )
    assert [(e.target, e.extra["kind"]) for e in _imports(edges)] == [
        ("external:github-actions:acme/setup-tool", "action"),
    ]


def test_expressions_and_docker_refs_are_recorded_not_guessed(tmp_path: Path) -> None:
    _nodes, edges = _parse(
        tmp_path,
        ".github/workflows/dyn.yml",
        "jobs:\n  a:\n    steps:\n"
        "      - uses: ${{ matrix.action }}@v1\n"
        "      - uses: acme/setup-tool@${{ inputs.version }}\n"
        "      - uses: docker://registry.example.com/base/python:3.12\n"
        "      - uses: docker://alpine@sha256:" + "ab" * 32 + "\n",
    )
    rows = _imports(edges)
    assert rows[0].target == "external:github-actions:dynamic"
    assert rows[0].extra["dynamic"] is True
    assert rows[0].extra["raw"] == "${{ matrix.action }}@v1"
    assert rows[1].target == "external:github-actions:acme/setup-tool"
    assert rows[1].extra["dynamic"] is True
    assert rows[1].extra["ref"] == "${{ inputs.version }}"
    assert rows[2].target == "external:docker-image:registry.example.com/base/python"
    assert rows[2].extra["kind"] == "docker" and rows[2].extra["tag"] == "3.12"
    assert rows[3].target == "external:docker-image:alpine"
    assert rows[3].extra["digest"] == "sha256:" + "ab" * 32


def test_workflow_reference_cap(tmp_path: Path) -> None:
    steps = "".join(f"      - uses: acme/tool-{i}@v1\n" for i in range(700))
    nodes, edges = _parse(
        tmp_path, ".github/workflows/many.yml", "jobs:\n  a:\n    steps:\n" + steps,
    )
    assert len(_imports(edges)) == 500
    file_node = next(n for n in nodes if n.kind == "File")
    assert file_node.extra["infra_references_truncated"] is True


@pytest.mark.parametrize(
    "text",
    [
        "jobs: [unclosed\n",
        "jobs:\n  a:\n    uses: [1, 2]\n    steps: oops\n",
        "jobs: 5\n",
        "- just\n- a list\n",
        "jobs:\n  a:\n    steps:\n      - uses: {a: b}\n      - 7\n",
        "\x00\x01",
    ],
)
def test_malformed_workflows_never_raise(tmp_path: Path, text: str) -> None:
    _nodes, edges = _parse(tmp_path, ".github/workflows/bad.yml", text)
    assert _imports(edges) == []


def test_other_yaml_files_get_no_references(tmp_path: Path) -> None:
    text = "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n"
    nodes, edges = _parse(tmp_path, "config/pipeline.yml", text)
    assert _imports(edges) == []
    assert "github_actions_uses" not in next(n for n in nodes if n.kind == "File").extra
    # ``workflows`` without ``.github`` is not a workflow directory.
    _nodes, edges = _parse(tmp_path, "workflows/ci.yml", text)
    assert _imports(edges) == []


def test_workflow_parse_is_deterministic(tmp_path: Path) -> None:
    first = _dump(*_parse(tmp_path, ".github/workflows/ci.yml", WORKFLOW))
    second = _dump(*_parse(tmp_path, ".github/workflows/ci.yml", WORKFLOW))
    assert first == second


def _build(tmp_path: Path) -> GraphStore:
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    full_build(tmp_path, store)
    return store


def test_external_targets_survive_a_build_and_answer_who_uses_x(tmp_path: Path) -> None:
    for repo in ("one", "two"):
        _parse(
            tmp_path,
            f"{repo}/.github/workflows/ci.yml",
            "jobs:\n  a:\n    uses: acme/shared-workflows/.github/workflows/b.yml@main\n",
        )
    store = _build(tmp_path)
    try:
        target = "external:github-actions:acme/shared-workflows/.github/workflows/b.yml"
        sources = sorted(e.source_qualified for e in store.get_edges_by_target(target))
        assert sources == [
            (tmp_path / "one/.github/workflows/ci.yml").as_posix(),
            (tmp_path / "two/.github/workflows/ci.yml").as_posix(),
        ]
        listing = query_graph(
            "imports_of", sources[0], repo_root=str(tmp_path), max_results=10,
        )
        assert target in json.dumps(listing)
    finally:
        store.close()


def test_parse_docker_image_parts() -> None:
    parts = parse_docker_image("registry.example.com:5000/base/Python:3.12@sha256:abc")
    assert parts["name"] == "registry.example.com:5000/base/python"
    assert parts["registry"] == "registry.example.com:5000"
    assert parts["tag"] == "3.12" and parts["digest"] == "sha256:abc"
    assert parse_docker_image("python")["tag"] is None
    assert parse_docker_image("${BASE}")["dynamic"] is True
