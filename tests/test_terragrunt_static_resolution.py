"""Terragrunt references written as expressions are resolved from the files, never by running.

``find_in_parent_folders()``, ``get_parent_terragrunt_dir("root")``, ``get_repo_root()``,
``locals`` and exposed ``include`` locals are reduced statically. What cannot be (``get_env``,
``run_cmd``, ``sops_*``, ``var.*``) stays ``dynamic`` with one coarse ``dynamic_reason`` and
no text. The fixture tree is ``tests/fixtures/terragrunt_static``; every name is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.terragrunt_static import (
    DYNAMIC_REASONS,
    MAX_EVAL_DEPTH,
    MAX_FILE_BYTES,
)

FIXTURE = Path(__file__).parent / "fixtures" / "terragrunt_static"
DYNAMIC = "external:terragrunt:dynamic"
MODS = "external:terraform-module:https://git.example.com/acme/mods"


def _repo(tmp_path: Path) -> Path:
    """A copy of the fixture tree, so the repository root is a directory of its own."""
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    return root.resolve()


def _parse(
    root: Path, rel: str, text: Optional[str] = None,
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = root / rel
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return CodeParser(repo_root=root).parse_bytes(path, path.read_bytes())


def _rows(
    root: Path, rel: str, text: Optional[str] = None,
) -> list[tuple[Optional[str], int, str, dict[str, Any]]]:
    """``(block, line, target relative to the repository, extra)`` of every reference."""
    _nodes, edges = _parse(root, rel, text)
    prefix = root.as_posix() + "/"
    out = []
    for edge in edges:
        if edge.kind != "IMPORTS_FROM":
            continue
        target = edge.target[len(prefix):] if edge.target.startswith(prefix) else edge.target
        out.append((edge.extra.get("block"), edge.line, target, edge.extra))
    return out


def _targets(root: Path, rel: str, text: Optional[str] = None) -> list[str]:
    return [row[2] for row in _rows(root, rel, text)]


def _dump(nodes: list[NodeInfo], edges: list[EdgeInfo]) -> str:
    return json.dumps(
        [asdict(n) for n in nodes] + [asdict(e) for e in edges], sort_keys=True, default=str,
    ) + repr(nodes) + repr(edges)


# ---------------------------------------------------------------------------
# What is resolved
# ---------------------------------------------------------------------------


def test_expressions_resolve_to_the_files_they_name(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    got = [(b, line, t) for b, line, t, _ in _rows(root, "live/prod/app/terragrunt.hcl")]
    assert got == [
        ("include", 2, "root.hcl"),
        ("include", 7, "_envcommon/app.hcl"),
        ("include", 12, "live/terragrunt.hcl"),
        ("terraform", 16, "modules/app/main.tf"),
        ("dependency", 20, "live/prod/db/terragrunt.hcl"),
        ("dependencies", 24, "live/prod/db/terragrunt.hcl"),
        ("dependencies", 24, "live/prod/lib/terragrunt.hcl"),
        ("dependencies", 24, "../missing"),
    ]
    rows = _rows(root, "live/prod/app/terragrunt.hcl")
    assert all(extra["dynamic"] is False for *_, extra in rows)
    flags = [extra.get("resolved_statically", False) for *_, extra in rows]
    # The written-out paths ("../db", "../missing") are not evaluated; the rest are.
    assert flags == [True, True, True, True, True, False, True, False]
    assert all("dynamic_reason" not in extra for *_, extra in rows)


def test_unit_directory_and_include_functions(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert [(b, t) for b, _l, t, _ in _rows(root, "live/prod/lib/terragrunt.hcl")] == [
        ("include", "root.hcl"),
        # No main.tf in the module: the first .tf file by name stands for it.
        ("terraform", "modules/lib/a.tf"),
        ("dependency", "live/prod/app/terragrunt.hcl"),
        ("dependency", "live/prod/db/terragrunt.hcl"),
        ("dependencies", "live/prod/db/terragrunt.hcl"),
    ]


def test_locals_in_the_same_file_and_exposed_include_locals(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(root, "live/prod/locals/terragrunt.hcl")
    assert [(b, t) for b, _l, t, _ in rows] == [
        ("include", "_envcommon/app.hcl"),
        ("include", "common.hcl"),
        ("terraform", "modules/app/main.tf"),
        ("dependency", DYNAMIC),  # a remote address used as a path names no file
    ]
    assert rows[3][3]["dynamic_reason"] == "not_found"

    exposed = _rows(root, "live/prod/exposed/terragrunt.hcl")
    assert exposed[1][2] == MODS
    extra = exposed[1][3]
    assert (extra["ref"], extra["subdir"], extra["source_kind"]) == ("v9.0.0", "app", "git")
    assert extra["resolved_statically"] is True and extra["dynamic"] is False


def test_included_file_that_is_not_a_unit_resolves_its_own_locals(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(root, "_envcommon/app.hcl")
    assert [(b, t) for b, _l, t, _ in rows] == [("terraform", MODS)]
    assert rows[0][3]["ref"] == "v1.2.0"


def test_context_functions_in_a_file_that_is_not_a_unit_stay_dynamic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    text = (
        'dependency "a" {\n  config_path = "${get_terragrunt_dir()}/../db"\n}\n'
        'dependency "b" {\n  config_path = "${path_relative_to_include()}"\n}\n'
        'dependency "c" {\n  config_path = "${get_repo_root()}/live/prod/db"\n}\n'
    )
    rows = _rows(root, "_envcommon/context.hcl", text)
    assert [(r[2], r[3].get("dynamic_reason")) for r in rows] == [
        (DYNAMIC, "unsupported_function"),
        (DYNAMIC, "unsupported_function"),
        ("live/prod/db/terragrunt.hcl", None),
    ]


def test_find_in_parent_folders_starts_at_the_parent_and_stops_at_the_repo_root(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    (root / "live/prod/app/terragrunt.hcl").write_text(
        'include {\n  path = find_in_parent_folders("root.hcl")\n}\n'
        'include "own" {\n  path = find_in_parent_folders("terragrunt.hcl")\n}\n'
        'include "gone" {\n  path = find_in_parent_folders("nothing.hcl")\n}\n'
    )
    rows = _rows(root, "live/prod/app/terragrunt.hcl")
    # A terragrunt.hcl in the unit's own directory is not a parent file.
    assert [r[2] for r in rows] == ["root.hcl", "live/terragrunt.hcl", DYNAMIC]
    assert rows[2][3]["dynamic_reason"] == "not_found"


def test_find_in_parent_folders_does_not_search_above_the_repo_root(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    (outer / "repo/unit").mkdir(parents=True)
    (outer / "above.hcl").write_text('locals {\n  a = "b"\n}\n')
    rows = _rows(
        (outer / "repo").resolve(), "unit/terragrunt.hcl",
        'include {\n  path = find_in_parent_folders("above.hcl")\n}\n',
    )
    assert [r[2] for r in rows] == [DYNAMIC]
    assert rows[0][3]["dynamic_reason"] == "not_found"


def test_directory_target_without_main_tf_is_chosen_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    forward = _targets(root, "live/prod/lib/terragrunt.hcl")
    real_scandir = os.scandir

    class _Reversed:
        def __init__(self, path: Any) -> None:
            self._entries = list(real_scandir(path))[::-1]

        def __enter__(self) -> Any:
            return iter(self._entries)

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(os, "scandir", _Reversed)
    assert _targets(root, "live/prod/lib/terragrunt.hcl") == forward
    assert forward[1] == "modules/lib/a.tf"
    # A directory with no Terraform file is not a target.
    rows = _rows(
        root, "live/terragrunt.hcl",
        'terraform {\n  source = "${get_repo_root()}/modules/empty"\n}\n',
    )
    assert rows[0][2] == DYNAMIC and rows[0][3]["dynamic_reason"] == "not_found"


def test_literal_local_source_without_main_tf_uses_the_first_terraform_file(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    assert _targets(
        root, "live/terragrunt.hcl", 'terraform {\n  source = "../modules//lib"\n}\n',
    ) == ["modules/lib/a.tf"]


def test_dependencies_paths_become_references(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(
        root, "live/prod/app/terragrunt.hcl",
        'dependencies {\n  paths = [\n    "../db",\n    "../lib",\n'
        '    "${get_repo_root()}/live/prod/db"\n  ]\n}\n',
    )
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("dependencies", 3, "live/prod/db/terragrunt.hcl"),
        ("dependencies", 4, "live/prod/lib/terragrunt.hcl"),
        ("dependencies", 5, "live/prod/db/terragrunt.hcl"),
    ]
    many = ", ".join(f'"../p{i}"' for i in range(150))
    capped = _rows(root, "live/terragrunt.hcl", f"dependencies {{\n  paths = [{many}]\n}}\n")
    assert len(capped) == 100


def test_edge_order_and_content_are_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(FIXTURE, other)
    for rel in ("live/prod/app/terragrunt.hcl", "live/prod/locals/terragrunt.hcl"):
        first = _dump(*_parse(root, rel))
        assert first == _dump(*_parse(root, rel))
        again = _dump(*_parse(other.resolve(), rel)).replace(
            other.resolve().as_posix(), root.as_posix(),
        )
        assert first == again


def test_full_build_links_resolved_references_to_file_nodes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(root, store)
        unit = (root / "live/prod/app/terragrunt.hcl").as_posix()
        targets = {
            e.target_qualified for e in store.get_edges_by_source(unit)
            if e.kind == "IMPORTS_FROM"
        }
        assert (root / "root.hcl").as_posix() in targets
        assert (root / "modules/app/main.tf").as_posix() in targets
        files = set(store.get_all_files())
        # A written-out path to a file that does not exist keeps its text; the rest are files.
        assert all(t in files for t in targets if t != "../missing")
        assert "../missing" in targets
    finally:
        store.close()


# ---------------------------------------------------------------------------
# What stays dynamic, and why
# ---------------------------------------------------------------------------


def test_unresolvable_expressions_stay_dynamic_with_a_coarse_reason(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(root, "live/prod/dynamic/terragrunt.hcl")
    got = [(r[0], r[1], r[2], r[3].get("name"), r[3].get("dynamic_reason")) for r in rows]
    assert got == [
        ("include", 6, DYNAMIC, "from_env", "env"),
        ("include", 10, DYNAMIC, "from_command", "command"),
        ("terraform", 14, DYNAMIC, None, "variable"),
        ("dependency", 18, DYNAMIC, "a", "unsupported_function"),
        ("dependency", 22, DYNAMIC, "b", "unsupported_function"),
        ("dependency", 26, DYNAMIC, "c", "variable"),
        ("dependency", 30, DYNAMIC, "d", "unsupported_function"),
        ("dependency", 34, DYNAMIC, "e", "not_found"),
        ("include", 38, "common.hcl", "hidden", None),
        ("dependency", 42, DYNAMIC, "f", "not_found"),
        ("dependencies", 46, DYNAMIC, None, "unsupported_function"),
    ]
    for row in rows:
        extra = row[3]
        if row[2] == DYNAMIC:
            assert extra["dynamic"] is True
            assert extra["dynamic_reason"] in DYNAMIC_REASONS
            assert "resolved_statically" not in extra
        assert set(extra) <= {
            "resolution", "block", "name", "dynamic", "dynamic_reason", "source_kind",
            "ref", "subdir", "resolved_statically",
        }


def test_every_reason_is_in_the_fixed_vocabulary() -> None:
    assert DYNAMIC_REASONS == (
        "env", "command", "variable", "unsupported_function", "depth_limit", "not_found",
    )


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ('get_env("HOME")', "env"),
        ('run_cmd("echo", "x")', "command"),
        ('sops_decrypt_file("x.json")', "unsupported_function"),
        ('read_terragrunt_config("x.hcl").locals.a', "unsupported_function"),
        ('format("%s", "x")', "unsupported_function"),
        ('try(local.missing, "x")', "unsupported_function"),
        ("var.name", "variable"),
        ("each.value", "variable"),
        ("dependency.vpc.outputs.path", "variable"),
        ('true ? "./a" : "./b"', "variable"),
        ('["./a"]', "unsupported_function"),
        ('"${var.dir}/a"', "variable"),
        ('"${get_repo_root()}/$${escaped}"', "unsupported_function"),
        ("local.nothing", "not_found"),
        ('"%{ if true }./a%{ endif }"', "unsupported_function"),
    ],
)
def test_expression_shapes_that_cannot_be_reduced(
    tmp_path: Path, expression: str, reason: str,
) -> None:
    root = _repo(tmp_path)
    rows = _rows(
        root, "live/terragrunt.hcl", f'dependency "x" {{\n  config_path = {expression}\n}}\n',
    )
    assert len(rows) == 1
    assert rows[0][2] == DYNAMIC and rows[0][3]["dynamic_reason"] == reason


def test_cyclic_expressions_end_quickly_as_depth_limit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(root, "live/prod/cycle/terragrunt.hcl")
    assert [r[2] for r in rows] == [DYNAMIC] * 3
    assert {r[3]["dynamic_reason"] for r in rows} == {"depth_limit"}


def test_an_include_that_includes_itself_is_a_cycle(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    text = (
        'include "a" {\n  path = get_parent_terragrunt_dir("b")\n  expose = true\n}\n'
        'include "b" {\n  path = get_parent_terragrunt_dir("a")\n  expose = true\n}\n'
    )
    rows = _rows(root, "live/prod/cycle/terragrunt.hcl", text)
    assert [r[2] for r in rows] == [DYNAMIC, DYNAMIC]
    assert {r[3]["dynamic_reason"] for r in rows} == {"depth_limit"}


def test_a_chain_of_locals_resolves_until_the_depth_limit(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    def chain(length: int) -> str:
        lines = ['  l0 = "${get_repo_root()}/live/prod/db"']
        lines += [f"  l{i} = local.l{i - 1}" for i in range(1, length)]
        last = f"l{length - 1}"
        return (
            "locals {\n" + "\n".join(lines) + "\n}\n"
            f'dependency "x" {{\n  config_path = local.{last}\n}}\n'
        )

    short = _rows(root, "live/prod/cycle/terragrunt.hcl", chain(3))
    assert short[0][2] == "live/prod/db/terragrunt.hcl"
    long = _rows(root, "live/prod/cycle/terragrunt.hcl", chain(MAX_EVAL_DEPTH * 2))
    assert long[0][2] == DYNAMIC and long[0][3]["dynamic_reason"] == "depth_limit"


def test_reading_too_many_included_files_is_a_depth_limit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for name in "abcd":
        (root / f"{name}.hcl").write_text(f'locals {{\n  v = "{name}"\n}}\n')
    includes = "".join(
        f'include "{n}" {{\n  path = "../../../{n}.hcl"\n  expose = true\n}}\n' for n in "abcd"
    )

    def source(names: str) -> str:
        parts = "".join("${include.%s.locals.v}" % n for n in names)
        return f'dependency "x" {{\n  config_path = "{parts}"\n}}\n'

    ok = _rows(root, "live/prod/cycle/terragrunt.hcl", includes + source("abc"))
    # "abc" is a relative path that names nothing: not_found, so all three files were read.
    assert ok[-1][3]["dynamic_reason"] == "not_found"
    over = _rows(root, "live/prod/cycle/terragrunt.hcl", includes + source("abcd"))
    assert over[-1][3]["dynamic_reason"] == "depth_limit"


def test_huge_expression_and_huge_included_file_are_bounded(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    big = "a" * 6000
    rows = _rows(
        root, "live/terragrunt.hcl",
        f'dependency "x" {{\n  config_path = "${{get_repo_root()}}/{big}"\n}}\n',
    )
    # An over-long string is not reference-shaped, so it adds no edge at all.
    assert rows == []
    # A reference that is short but reduces to a long value is cut off.
    rows = _rows(
        root, "live/terragrunt.hcl",
        f'locals {{\n  big = "{big}"\n}}\n'
        'dependency "x" {\n  config_path = "${local.big}"\n}\n',
    )
    assert rows[0][2] == DYNAMIC and rows[0][3]["dynamic_reason"] == "depth_limit"
    (root / "huge.hcl").write_text('locals {\n  v = "x"\n}\n' + "# pad\n" * (MAX_FILE_BYTES // 6))
    assert (root / "huge.hcl").stat().st_size > MAX_FILE_BYTES
    rows = _rows(
        root, "live/prod/cycle/terragrunt.hcl",
        'include "h" {\n  path = "../../../huge.hcl"\n  expose = true\n}\n'
        'dependency "x" {\n  config_path = include.h.locals.v\n}\n',
    )
    assert rows[1][2] == DYNAMIC and rows[1][3]["dynamic_reason"] == "depth_limit"


def test_references_outside_the_repository_are_never_resolved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rows = _rows(root, "live/prod/deep/terragrunt.hcl")
    assert [(r[0], r[2], r[3]["dynamic_reason"]) for r in rows] == [
        ("include", DYNAMIC, "not_found"),
        ("terraform", DYNAMIC, "not_found"),
        ("dependency", DYNAMIC, "not_found"),
        ("dependency", DYNAMIC, "not_found"),
    ]
    assert str(tmp_path.parent) not in repr(rows)


def test_a_symbolic_link_out_of_the_repository_is_not_followed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outer.hcl").write_text('locals {\n  v = "x"\n}\n')
    (outside / "terragrunt.hcl").write_text('locals {\n  v = "x"\n}\n')
    try:
        os.symlink(outside / "outer.hcl", root / "link.hcl")
        os.symlink(outside, root / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    rows = _rows(
        root, "live/prod/cycle/terragrunt.hcl",
        'include "a" {\n  path = find_in_parent_folders("link.hcl")\n}\n'
        'include "b" {\n  path = "${get_repo_root()}/linkdir/terragrunt.hcl"\n}\n'
        'dependency "c" {\n  config_path = "${get_repo_root()}/linkdir"\n}\n',
    )
    assert [r[2] for r in rows] == [DYNAMIC] * 3
    assert str(outside) not in repr(rows)


def test_a_symbolically_linked_repository_root_still_resolves(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    link = tmp_path / "link"
    try:
        os.symlink(root, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    parser = CodeParser(repo_root=link)
    path = link / "live/prod/app/terragrunt.hcl"
    _nodes, edges = parser.parse_bytes(path, path.read_bytes())
    targets = [e.target for e in edges if e.kind == "IMPORTS_FROM"]
    assert (link / "root.hcl").as_posix() in targets
    assert (link / "modules/app/main.tf").as_posix() in targets


def test_excluded_files_are_not_resolved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    parser = CodeParser(repo_root=root)
    parser.exclude_files({str(root / "root.hcl"), str(root / "modules/app/main.tf")})
    path = root / "live/prod/app/terragrunt.hcl"
    _nodes, edges = parser.parse_bytes(path, path.read_bytes())
    rows = [e for e in edges if e.kind == "IMPORTS_FROM"]
    assert rows[0].target == DYNAMIC and rows[0].extra["dynamic_reason"] == "not_found"
    assert rows[3].target == DYNAMIC


def test_without_a_repository_root_expressions_stay_dynamic(tmp_path: Path) -> None:
    path = tmp_path / "terragrunt.hcl"
    path.write_text('include {\n  path = find_in_parent_folders()\n}\n')
    _nodes, edges = CodeParser().parse_bytes(path, path.read_bytes())
    row = [e for e in edges if e.kind == "IMPORTS_FROM"][0]
    assert row.target == DYNAMIC and row.extra["dynamic_reason"] == "not_found"


@pytest.mark.parametrize(
    "text",
    [
        'include {\n  path = find_in_parent_folders(',
        'include {\n  path = find_in_parent_folders("a", "b", "c")\n}\n',
        'include {\n  path = find_in_parent_folders("../root.hcl")\n}\n',
        'include {\n  path = find_in_parent_folders("/etc/passwd")\n}\n',
        'include {\n  path = find_in_parent_folders(1)\n}\n',
        'include "a" {\n  path = include.a.locals.x\n  expose = true\n}\n',
        'include {\n  path = path_relative_to_include()\n}\n',
        'dependencies {\n  paths = local.nothing\n}\n',
        'dependencies {\n  paths = [1, [2], {a = 3}]\n}\n',
        "locals {\n  a = local.a\n}\ndependency \"x\" {\n  config_path = local.a\n}\n",
    ],
)
def test_odd_expressions_never_raise(tmp_path: Path, text: str) -> None:
    root = _repo(tmp_path)
    _rows(root, "live/prod/cycle/terragrunt.hcl", text)


# ---------------------------------------------------------------------------
# Nothing is executed, read from the environment or stored
# ---------------------------------------------------------------------------

PLANTED = (
    "planted-local-token-3b7c", "planted-env-default-token-5d1e",
    "planted-command-token-8a40", "planted-user", "planted-pass-7f2b",
    "planted-file-token-91cd", "planted-include-secret-44ab", "planted-query-token-2e6f",
    "planted-environment-value-17aa", "planted-input-secret-62de",
)


def _assert_none_planted(nodes: list[NodeInfo], edges: list[EdgeInfo]) -> None:
    dumped = _dump(nodes, edges)
    for planted in PLANTED:
        assert planted not in dumped, planted


def test_the_fixture_with_planted_secrets_stores_none(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for rel in ("live/prod/dynamic/terragrunt.hcl", "live/prod/app/terragrunt.hcl"):
        _assert_none_planted(*_parse(root, rel))


def test_values_used_to_find_a_target_are_never_stored(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "secrets").mkdir()
    (root / "secrets/creds.hcl").write_text(
        'locals {\n  token = "planted-file-token-91cd"\n'
        '  source = "git::https://planted-user:planted-pass-7f2b@git.example.com/acme/mods.git'
        '//vpc?token=planted-query-token-2e6f&ref=v7"\n}\n',
    )
    (root / "inc.hcl").write_text(
        'locals {\n  secret = "planted-include-secret-44ab"\n'
        '  module = "git::https://planted-user:planted-pass-7f2b@git.example.com/acme/mods.git'
        '//vpc?ref=v3"\n}\n',
    )
    text = (
        "locals {\n"
        '  token  = "planted-local-token-3b7c"\n'
        '  source = "git::https://planted-user:planted-pass-7f2b@git.example.com/acme/mods.git'
        '//net?token=planted-query-token-2e6f&ref=v1"\n'
        '  pathy  = "planted-local-token-3b7c.hcl"\n'
        "}\n"
        'include "inc" {\n  path = "${get_repo_root()}/inc.hcl"\n  expose = true\n}\n'
        'include "tok" {\n  path = local.pathy\n}\n'
        "terraform {\n  source = local.source\n}\n"
        'dependency "a" {\n'
        '  config_path = read_terragrunt_config("secrets/creds.hcl").locals.token\n'
        '  mock_outputs = {\n    x = "planted-input-secret-62de"\n  }\n}\n'
        'dependency "b" {\n'
        '  config_path = get_env("PLANTED_ENV", "planted-environment-value-17aa")\n}\n'
        'dependency "c" {\n  config_path = include.inc.locals.secret\n}\n'
        'dependency "d" {\n  config_path = include.inc.locals.module\n}\n'
        'inputs = {\n  password = "planted-input-secret-62de"\n  token = local.token\n}\n'
    )
    nodes, edges = _parse(root, "live/prod/cycle/terragrunt.hcl", text)
    _assert_none_planted(nodes, edges)
    rows = [e for e in edges if e.kind == "IMPORTS_FROM"]
    by_block = [(e.extra.get("block"), e.extra.get("name"), e.target) for e in rows]
    assert by_block[0][2] == (root / "inc.hcl").as_posix()
    assert by_block[1] == ("include", "tok", DYNAMIC)
    assert by_block[2] == ("terraform", None, MODS)
    assert rows[2].extra["ref"] == "v1" and rows[2].extra["subdir"] == "net"
    assert [b[2] for b in by_block[3:]] == [DYNAMIC] * 4
    for row in rows:
        assert "raw" not in row.extra


def test_planted_commands_and_environment_are_never_run_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    sentinel = tmp_path / "sentinel-must-not-exist"
    monkeypatch.setenv("PLANTED_ENV", "planted-environment-value-17aa")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(os, "popen", refuse)
    text = (
        f'include "a" {{\n  path = run_cmd("touch", "{sentinel.as_posix()}")\n}}\n'
        'include "b" {\n  path = run_cmd("--terragrunt-quiet", "sh", "-c", '
        f'"touch {sentinel.as_posix()}")\n}}\n'
        'dependency "c" {\n'
        '  config_path = get_env("PLANTED_ENV", "planted-env-default-token-5d1e")\n}\n'
        'dependency "d" {\n  config_path = "${get_env("PLANTED_ENV")}/x"\n}\n'
        'dependency "e" {\n  config_path = sops_decrypt_file("x")\n}\n'
    )
    nodes, edges = _parse(root, "live/prod/cycle/terragrunt.hcl", text)
    assert not sentinel.exists()
    _assert_none_planted(nodes, edges)
    reasons = [e.extra["dynamic_reason"] for e in edges if e.kind == "IMPORTS_FROM"]
    assert reasons == ["command", "command", "env", "env", "unsupported_function"]



# ---------------------------------------------------------------------------
# Terraform module sources in .tf files
# ---------------------------------------------------------------------------


def _module_edges(tmp_path: Path, source: str) -> list[EdgeInfo]:
    text = f'module "m" {{\n  source = "{source}"\n}}\n'
    _nodes, edges = _parse(tmp_path.resolve(), "main.tf", text)
    return [e for e in edges if e.kind == "IMPORTS_FROM"]


@pytest.mark.parametrize(
    ("source", "target", "kind", "ref"),
    [
        ("terraform-aws-modules/vpc/aws", "external:terraform-module:terraform-aws-modules/vpc/aws",
         "registry", None),
        ("git::https://git.example.com/acme/mods.git//net?ref=v2",
         "external:terraform-module:https://git.example.com/acme/mods", "git", "v2"),
        ("github.com/acme/mods//net", "external:terraform-module:github.com/acme/mods", "git",
         None),
        ("https://example.com/m.zip", "external:terraform-module:https://example.com/m.zip",
         "archive", None),
    ],
)
def test_remote_terraform_module_sources_are_external_targets(
    tmp_path: Path, source: str, target: str, kind: str, ref: Optional[str],
) -> None:
    (edge,) = _module_edges(tmp_path, source)
    assert edge.target == target
    assert edge.extra["source_kind"] == kind and edge.extra["ref"] == ref
    assert edge.extra["dynamic"] is False


def test_terraform_module_credentials_and_queries_are_never_stored(tmp_path: Path) -> None:
    for source in (
        "git::https://planted-user:planted-pass-7f2b@git.example.com/acme/mods.git"
        "?token=planted-query-token-2e6f&ref=v1",
        "git@git.example.com:acme/mods.git//x?access=planted-query-token-2e6f",
        "https://planted-user:planted-pass-7f2b@git.example.com/m.zip#planted-file-token-91cd",
    ):
        edges = _module_edges(tmp_path, source)
        assert edges[0].target.startswith("external:terraform-module:")
        assert not any(p in repr(edges) for p in PLANTED)
    (edge,) = _module_edges(tmp_path, "https://planted-user:pa?ss-planted-pass-7f2b@host/x")
    assert edge.target == "external:terraform-module:dynamic"
    assert edge.extra["dynamic"] is True
    assert "planted" not in repr(edge)


def test_local_terraform_module_sources_are_unchanged(tmp_path: Path) -> None:
    (edge,) = _module_edges(tmp_path, "../modules/net")
    assert edge.target == "../modules/net"
    assert edge.extra == {}
