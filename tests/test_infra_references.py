"""Reference-shaped strings in infrastructure files become external IMPORTS_FROM edges.

Covers GitHub Actions ``uses:`` references, Dockerfile images and Terragrunt sources. The
graph stays value-free: only the allowlisted reference strings are stored, never ``env``,
``with``, ``secrets``, ``ARG``/``ENV`` values or Terragrunt ``inputs``. Names are synthetic.
"""

from __future__ import annotations

import json
import os
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


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

DOCKERFILE = f"""\
# syntax=docker/dockerfile:1
ARG BASE=registry.example.com/base/python:3.12
ARG TOKEN={SECRET}
FROM --platform=linux/amd64 registry.example.com/base/python:3.12 AS build
ENV API_KEY={SECRET}
LABEL owner="{SECRET}"
RUN pip install \\
    --no-cache-dir \\
    wheel
COPY --from=acme/tool:1.2@sha256:{"cd" * 32} /usr/bin/tool /usr/bin/tool

FROM build AS test
RUN echo hi

FROM ${{BASE}} AS dynamic
FROM registry.example.com:5000/base/node@sha256:{"ab" * 32}
COPY --from=build /out /out
COPY --from=0 /out /out2
COPY --from=test /out /out3
FROM scratch AS empty
"""


def _docker(tmp_path: Path, name: str = "Dockerfile", text: str = DOCKERFILE):
    return _parse(tmp_path, name, text)


def test_dockerfile_stages_and_image_edges(tmp_path: Path) -> None:
    nodes, edges = _docker(tmp_path)
    stages = [n for n in nodes if n.kind == "Class"]
    assert [n.name for n in stages] == [
        "stage:build", "stage:test", "stage:dynamic", "stage:3", "stage:empty",
    ]
    assert stages[0].extra["base_image"] == "registry.example.com/base/python:3.12"
    imports = _imports(edges)
    got = [(e.target, e.line, e.extra["via"], e.extra.get("stage")) for e in imports]
    assert got == [
        ("external:docker-image:registry.example.com/base/python", 4, "from", "build"),
        ("external:docker-image:acme/tool", 10, "copy_from", "build"),
        ("external:docker-image:dynamic", 15, "from", "dynamic"),
        ("external:docker-image:registry.example.com:5000/base/node", 16, "from", None),
    ]
    assert imports[0].extra["tag"] == "3.12"
    assert imports[1].extra["tag"] == "1.2"
    assert imports[1].extra["digest"] == "sha256:" + "cd" * 32
    assert imports[2].extra["dynamic"] is True
    assert imports[2].extra["raw"] == "${BASE}"
    assert imports[3].extra["digest"] == "sha256:" + "ab" * 32
    assert imports[3].extra["registry"] == "registry.example.com:5000"


def test_dockerfile_stage_to_stage_edges(tmp_path: Path) -> None:
    _nodes, edges = _docker(tmp_path)
    file_path = (tmp_path / "Dockerfile").as_posix()
    deps = [
        (e.source.split("::")[1], e.target.split("::")[1], e.extra["via"], e.line)
        for e in edges if e.kind == "DEPENDS_ON"
    ]
    assert deps == [
        ("stage:test", "stage:build", "from", 12),
        ("stage:3", "stage:build", "copy_from", 17),
        ("stage:3", "stage:build", "copy_from", 18),
        ("stage:3", "stage:test", "copy_from", 19),
    ]
    assert all(e.source.startswith(file_path) for e in edges if e.kind == "DEPENDS_ON")


def test_dockerfile_never_stores_arg_env_or_label_values(tmp_path: Path) -> None:
    nodes, edges = _docker(tmp_path)
    assert SECRET not in _dump(nodes, edges)


@pytest.mark.parametrize(
    "name",
    ["Dockerfile", "Dockerfile.dev", "api.Dockerfile", "Containerfile", "dockerfile"],
)
def test_dockerfile_names_are_detected(tmp_path: Path, name: str) -> None:
    nodes, edges = _docker(tmp_path, name, "FROM acme/base:1\n")
    assert next(n for n in nodes if n.kind == "File").language == "dockerfile"
    assert [e.target for e in _imports(edges)] == ["external:docker-image:acme/base"]


@pytest.mark.parametrize("name", ["Dockerfile-notes.md", "dockerfiles.txt", "mydockerfile"])
def test_non_dockerfile_names_are_not_detected(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("FROM acme/base:1\n")
    assert CodeParser().detect_language(path) != "dockerfile"


def test_dockerfile_line_cap(tmp_path: Path) -> None:
    filler = "RUN true\n" * 3000
    text = "FROM acme/base:1\n" + filler + "FROM acme/late:1\n"
    nodes, edges = _docker(tmp_path, text=text)
    assert [e.target for e in _imports(edges)] == ["external:docker-image:acme/base"]
    assert next(n for n in nodes if n.kind == "File").extra["infra_references_truncated"]
    many = "FROM acme/base:1\n" * 5000
    nodes, edges = _docker(tmp_path, text=many)
    assert len(_imports(edges)) <= 500
    assert len([n for n in nodes if n.kind == "Class"]) <= 500


@pytest.mark.parametrize(
    "text",
    ["FROM\n", "FROM --platform\n", "COPY --from=\n", "\x00\xff", "FROM a AS\nCOPY --from", ""],
)
def test_malformed_dockerfiles_never_raise(tmp_path: Path, text: str) -> None:
    _docker(tmp_path, text=text)


def test_dockerfile_is_deterministic(tmp_path: Path) -> None:
    assert _dump(*_docker(tmp_path)) == _dump(*_docker(tmp_path))


def test_dockerfile_edges_survive_a_build(tmp_path: Path) -> None:
    _docker(tmp_path, "svc/Dockerfile", "FROM registry.example.com/base/python:3.12\n")
    _docker(tmp_path, "web/Dockerfile.dev", "FROM registry.example.com/base/python:3.11\n")
    store = _build(tmp_path)
    try:
        edges = store.get_edges_by_target("external:docker-image:registry.example.com/base/python")
        assert sorted((e.file_path, e.extra["tag"]) for e in edges) == [
            ((tmp_path / "svc/Dockerfile").as_posix(), "3.12"),
            ((tmp_path / "web/Dockerfile.dev").as_posix(), "3.11"),
        ]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Terragrunt
# ---------------------------------------------------------------------------

TERRAGRUNT = f"""\
locals {{
  region = "{SECRET}"
}}

terraform {{
  source = "git::https://git.example.com/acme/modules.git//network/vpc?ref=v1.4.0"
}}

include "root" {{
  path = find_in_parent_folders()
}}

include "common" {{
  path = "../common.hcl"
}}

dependency "vpc" {{
  config_path = "../vpc"
  mock_outputs = {{
    vpc_id = "{SECRET}"
  }}
}}

dependency "shared" {{
  config_path = "${{get_repo_root()}}/shared"
}}

inputs = {{
  password = "{SECRET}"
}}
"""


def test_parse_terragrunt_source_variants() -> None:
    from code_review_graph.infra_references import parse_terragrunt_source

    git = parse_terragrunt_source("git::https://git.example.com/acme/mods.git//vpc?ref=v1")
    assert (git["kind"], git["base"], git["subdir"], git["ref"]) == (
        "git", "https://git.example.com/acme/mods", "vpc", "v1",
    )
    ssh = parse_terragrunt_source("git@git.example.com:acme/mods.git//a/b?ref=main")
    assert (ssh["kind"], ssh["base"], ssh["subdir"], ssh["ref"]) == (
        "git", "git@git.example.com:acme/mods", "a/b", "main",
    )
    registry = parse_terragrunt_source("tfr:///acme/vpc/aws?version=3.1.0")
    assert registry["kind"] == "registry" and registry["ref"] == "3.1.0"
    bare = parse_terragrunt_source("acme/vpc/aws")
    assert bare["kind"] == "registry" and bare["base"] == "acme/vpc/aws"
    assert parse_terragrunt_source("../modules/vpc")["kind"] == "local"
    shorthand = parse_terragrunt_source("git.example.com/acme/mods//x?ref=v2")
    assert shorthand["kind"] == "git" and shorthand["ref"] == "v2"
    assert parse_terragrunt_source("https://example.com/m.zip")["kind"] == "archive"


def test_terragrunt_references(tmp_path: Path) -> None:
    (tmp_path / "live").mkdir()
    (tmp_path / "common.hcl").write_text('locals {\n  a = "b"\n}\n')
    (tmp_path / "vpc").mkdir()
    (tmp_path / "vpc/terragrunt.hcl").write_text("")
    nodes, edges = _parse(tmp_path, "live/terragrunt.hcl", TERRAGRUNT)
    rows = _imports(edges)
    got = [(e.target, e.extra["block"], e.extra.get("name"), e.line) for e in rows]
    assert got == [
        ("external:terraform-module:https://git.example.com/acme/modules", "terraform", None, 6),
        ("external:terragrunt:dynamic", "include", "root", 10),
        ((tmp_path / "common.hcl").as_posix(), "include", "common", 14),
        ((tmp_path / "vpc/terragrunt.hcl").as_posix(), "dependency", "vpc", 18),
        ("external:terragrunt:dynamic", "dependency", "shared", 25),
    ]
    source = rows[0].extra
    assert source["ref"] == "v1.4.0" and source["subdir"] == "network/vpc"
    assert source["source_kind"] == "git" and source["dynamic"] is False
    assert rows[1].extra["raw"] == "find_in_parent_folders()"
    assert rows[1].extra["dynamic"] is True
    assert rows[4].extra["raw"] == '"${get_repo_root()}/shared"'
    assert SECRET not in _dump(nodes, edges)


def test_terragrunt_relative_paths_resolve_inside_the_repo(tmp_path: Path) -> None:
    (tmp_path / "common.hcl").write_text('locals {\n  a = "b"\n}\n')
    (tmp_path / "vpc").mkdir()
    (tmp_path / "vpc/terragrunt.hcl").write_text('terraform {\n  source = "./m"\n}\n')
    (tmp_path / "modules/net").mkdir(parents=True)
    (tmp_path / "modules/net/main.tf").write_text('variable "a" {}\n')
    _nodes, edges = _parse(
        tmp_path, "live/terragrunt.hcl",
        'include "c" {\n  path = "../common.hcl"\n}\n'
        'dependency "v" {\n  config_path = "../vpc"\n}\n'
        'terraform {\n  source = "../modules/net"\n}\n',
    )
    assert [e.target for e in _imports(edges)] == [
        (tmp_path / "common.hcl").as_posix(),
        (tmp_path / "vpc/terragrunt.hcl").as_posix(),
        (tmp_path / "modules/net/main.tf").as_posix(),
    ]


def test_terragrunt_blocks_in_tf_files_are_unchanged(tmp_path: Path) -> None:
    text = 'terraform {\n  source = "git::https://git.example.com/a/b.git"\n}\n'
    _nodes, edges = _parse(tmp_path, "main.tf", text)
    assert _imports(edges) == []


@pytest.mark.parametrize(
    "text",
    [
        "terraform {",
        'include {\n  path =\n}\n',
        'dependency "x" {\n  config_path = [1, 2]\n}\n',
        'terraform {\n  source = 5\n}\n',
        'include "a" {\n  other = "x"\n}\n',
        "\x00\x01",
    ],
)
def test_malformed_terragrunt_never_raises(tmp_path: Path, text: str) -> None:
    _parse(tmp_path, "terragrunt.hcl", text)


def test_terragrunt_is_deterministic(tmp_path: Path) -> None:
    assert _dump(*_parse(tmp_path, "terragrunt.hcl", TERRAGRUNT)) == _dump(
        *_parse(tmp_path, "terragrunt.hcl", TERRAGRUNT),
    )


def test_terragrunt_sources_answer_who_uses_module_x(tmp_path: Path) -> None:
    for unit in ("a", "b"):
        _parse(
            tmp_path, f"{unit}/terragrunt.hcl",
            'terraform {\n  source = "git::https://git.example.com/acme/modules.git//x?ref=v1"\n}\n',
        )
    store = _build(tmp_path)
    try:
        edges = store.get_edges_by_target(
            "external:terraform-module:https://git.example.com/acme/modules",
        )
        assert sorted(e.file_path for e in edges) == [
            (tmp_path / "a/terragrunt.hcl").as_posix(),
            (tmp_path / "b/terragrunt.hcl").as_posix(),
        ]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Local references never leave the repository or reach an excluded file
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository root and a sibling directory outside it holding real files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "terragrunt.hcl").write_text('locals {\n  a = "b"\n}\n')
    (outside / "dir/terragrunt.hcl").write_text('locals {\n  a = "b"\n}\n')
    (outside / "dir/main.tf").write_text('variable "a" {}\n')
    (outside / "dir/action.yml").write_text("runs:\n  using: composite\n  steps: []\n")
    return repo, outside


def _parse_excluding(
    root: Path, rel: str, text: str, excluded: set[Path],
) -> list[EdgeInfo]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    parser = CodeParser(repo_root=root)
    parser.exclude_files({str(p) for p in excluded})
    return parser.parse_bytes(path, path.read_bytes())[1]


def _tg_targets(root: Path, body: str) -> list[str]:
    _nodes, edges = _parse(root, "live/terragrunt.hcl", body)
    return [e.target for e in _imports(edges)]


@pytest.mark.parametrize("attribute", ["include", "dependency", "terraform"])
def test_terragrunt_path_outside_repo_is_not_resolved(tmp_path: Path, attribute: str) -> None:
    repo, outside = _make_repo(tmp_path)
    key = {"include": "path", "dependency": "config_path", "terraform": "source"}[attribute]
    label = ' "x"' if attribute != "terraform" else ""
    for raw in (
        "../../outside/terragrunt.hcl",
        "../../outside/dir",
        (outside / "terragrunt.hcl").as_posix(),
        outside.as_posix() + "/dir",
    ):
        body = f'{attribute}{label} {{\n  {key} = "{raw}"\n}}\n'
        assert _tg_targets(repo, body) == [raw]


def test_terragrunt_absolute_path_inside_repo_is_not_probed(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / "common.hcl").write_text('locals {\n  a = "b"\n}\n')
    raw = (repo / "common.hcl").as_posix()
    assert _tg_targets(repo, f'include "c" {{\n  path = "{raw}"\n}}\n') == [raw]


def test_terragrunt_inside_repo_still_resolves_with_children(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / "vpc").mkdir()
    (repo / "vpc/terragrunt.hcl").write_text('locals {\n  a = "b"\n}\n')
    (repo / "net").mkdir()
    (repo / "net/main.tf").write_text('variable "a" {}\n')
    body = (
        'dependency "v" {\n  config_path = "../vpc"\n}\n'
        'terraform {\n  source = "../net"\n}\n'
    )
    assert _tg_targets(repo, body) == [
        (repo / "vpc/terragrunt.hcl").as_posix(),
        (repo / "net/main.tf").as_posix(),
    ]


def test_terragrunt_excluded_files_are_not_resolved(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / "hidden").mkdir()
    (repo / "hidden/terragrunt.hcl").write_text('locals {\n  a = "b"\n}\n')
    (repo / "secret.hcl").write_text('locals {\n  a = "b"\n}\n')
    edges = _parse_excluding(
        repo, "live/terragrunt.hcl",
        'include "c" {\n  path = "../secret.hcl"\n}\n'
        'dependency "h" {\n  config_path = "../hidden"\n}\n',
        {repo / "secret.hcl", repo / "hidden/terragrunt.hcl"},
    )
    assert [e.target for e in _imports(edges)] == ["../secret.hcl", "../hidden"]


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")


def test_terragrunt_symlink_leaving_repo_is_not_followed(tmp_path: Path) -> None:
    repo, outside = _make_repo(tmp_path)
    _symlink(repo / "link.hcl", outside / "terragrunt.hcl")
    _symlink(repo / "linkdir", outside / "dir")
    body = (
        'include "c" {\n  path = "../link.hcl"\n}\n'
        'dependency "d" {\n  config_path = "../linkdir"\n}\n'
    )
    assert _tg_targets(repo, body) == ["../link.hcl", "../linkdir"]


def test_terragrunt_symlink_inside_repo_resolves(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / "real.hcl").write_text('locals {\n  a = "b"\n}\n')
    _symlink(repo / "alias.hcl", repo / "real.hcl")
    body = 'include "c" {\n  path = "../alias.hcl"\n}\n'
    assert _tg_targets(repo, body) == [(repo / "alias.hcl").as_posix()]


def _gha_targets(root: Path, uses: list[str], excluded: set[Path] | None = None) -> list[str]:
    text = "jobs:\n  a:\n    steps:\n" + "".join(f"      - uses: {u}\n" for u in uses)
    edges = _parse_excluding(root, ".github/workflows/ci.yml", text, excluded or set())
    return [e.target for e in _imports(edges)]


def test_github_actions_local_reference_outside_repo_is_not_resolved(tmp_path: Path) -> None:
    repo, outside = _make_repo(tmp_path)
    uses = [
        "./../../../outside/dir",
        "./../../../outside/dir/action.yml",
        "./../../../outside/terragrunt.hcl",
    ]
    assert _gha_targets(repo, uses) == uses


def test_github_actions_local_reference_inside_repo_still_resolves(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / ".github/actions/setup").mkdir(parents=True)
    (repo / ".github/actions/setup/action.yml").write_text(
        "runs:\n  using: composite\n  steps: []\n",
    )
    assert _gha_targets(repo, ["./.github/actions/setup"]) == [
        (repo / ".github/actions/setup/action.yml").as_posix(),
    ]


def test_github_actions_excluded_local_reference_is_not_resolved(tmp_path: Path) -> None:
    repo, _outside = _make_repo(tmp_path)
    (repo / ".github/actions/setup").mkdir(parents=True)
    action = repo / ".github/actions/setup/action.yml"
    action.write_text("runs:\n  using: composite\n  steps: []\n")
    assert _gha_targets(repo, ["./.github/actions/setup"], {action}) == [
        "./.github/actions/setup",
    ]


def test_github_actions_symlink_leaving_repo_is_not_followed(tmp_path: Path) -> None:
    repo, outside = _make_repo(tmp_path)
    (repo / ".github/actions").mkdir(parents=True)
    _symlink(repo / ".github/actions/ext", outside / "dir")
    assert _gha_targets(repo, ["./.github/actions/ext"]) == ["./.github/actions/ext"]
