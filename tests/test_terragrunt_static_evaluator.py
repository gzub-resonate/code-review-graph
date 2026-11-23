"""The static Terragrunt evaluator on its own: expression in, string or coarse reason out.

The evaluator reads files and runs nothing. These tests drive ``TerragruntEvaluator.evaluate``
directly on a parsed HCL expression, so a regression in the evaluator shows here and not only
through the parser that calls it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import pytest

from code_review_graph import terragrunt_static
from code_review_graph.terragrunt_static import (
    DYNAMIC_REASONS,
    MAX_EXPRESSION_BYTES,
    TerragruntEvaluator,
    _attribute_expression,
    _top_level_blocks,
)

tslp = pytest.importorskip("tree_sitter_language_pack")


def _parse(data: bytes) -> Any:
    return tslp.get_parser("hcl").parse(data).root_node


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with a root file, one unit and one module."""
    root = tmp_path.resolve()
    (root / "root.hcl").write_text('locals {\n  base = "modules"\n}\n')
    (root / "live" / "app").mkdir(parents=True)
    (root / "modules" / "app").mkdir(parents=True)
    (root / "modules" / "app" / "main.tf").write_text("")
    return root, root / "live" / "app" / "terragrunt.hcl"


def _evaluate(
    root: Path,
    unit: Path,
    expression: str,
    block: str = "terraform",
    attribute: str = "source",
) -> tuple[Optional[str], Optional[str]]:
    data = f"{block} {{\n  {attribute} = {expression}\n}}\n".encode()
    unit.write_bytes(data)
    tree = _parse(data)
    body = _top_level_blocks(tree, block)[0][1]
    expr = _attribute_expression(body, attribute)
    evaluator = TerragruntEvaluator(root, unit, tree, _parse, lambda path: False)
    return evaluator.evaluate(expr)


def test_a_plain_string_is_returned_as_written(tmp_path: Path) -> None:
    root, unit = _repo(tmp_path)
    assert _evaluate(root, unit, '"modules/app"') == ("modules/app", None)


def test_find_in_parent_folders_returns_the_nearest_file_of_that_name(tmp_path: Path) -> None:
    root, unit = _repo(tmp_path)
    value, reason = _evaluate(root, unit, 'find_in_parent_folders("root.hcl")')
    assert reason is None
    assert value == str(root / "root.hcl")


def test_directory_functions_join_into_one_string(tmp_path: Path) -> None:
    root, unit = _repo(tmp_path)
    value, reason = _evaluate(root, unit, '"${get_repo_root()}/modules/app"')
    assert (value, reason) == (f"{root}/modules/app", None)
    value, reason = _evaluate(root, unit, '"${get_terragrunt_dir()}/../../modules/app"')
    assert (value, reason) == (f"{root}/live/app/../../modules/app", None)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ('get_env("HOME")', "env"),
        ('run_cmd("echo", "x")', "command"),
        ("var.name", "variable"),
        ("unknown_function()", "unsupported_function"),
    ],
)
def test_an_expression_that_cannot_be_reduced_has_one_coarse_reason(
    tmp_path: Path, expression: str, reason: str,
) -> None:
    root, unit = _repo(tmp_path)
    assert _evaluate(root, unit, expression) == (None, reason)
    assert reason in DYNAMIC_REASONS


def test_a_file_outside_the_repository_is_not_found(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    root, _ = _repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = outside / "terragrunt.hcl"
    data = b'terraform {\n  source = "modules/app"\n}\n'
    stray.write_bytes(data)
    tree = _parse(data)
    expr = _attribute_expression(_top_level_blocks(tree, "terraform")[0][1], "source")
    evaluator = TerragruntEvaluator(root, stray, tree, _parse, lambda path: False)
    assert evaluator.evaluate(expr) == (None, "not_found")


def test_an_expression_past_the_size_bound_is_a_depth_limit(tmp_path: Path) -> None:
    root, unit = _repo(tmp_path)
    huge = '"' + "a" * (MAX_EXPRESSION_BYTES + 10) + '"'
    assert _evaluate(root, unit, huge) == (None, "depth_limit")


def test_evaluating_twice_gives_the_same_answer(tmp_path: Path) -> None:
    root, unit = _repo(tmp_path)
    data = b'terraform {\n  source = "${get_repo_root()}/modules/app"\n}\n'
    unit.write_bytes(data)
    tree = _parse(data)
    expr = _attribute_expression(_top_level_blocks(tree, "terraform")[0][1], "source")
    evaluator = TerragruntEvaluator(root, unit, tree, _parse, lambda path: False)
    assert evaluator.evaluate(expr) == evaluator.evaluate(expr)


def test_the_evaluator_cannot_run_a_process_read_the_environment_or_use_the_network() -> None:
    source = Path(terragrunt_static.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "subprocess", "os.environ", "getenv", "socket", "urllib", "http", "requests",
        "os.system", "os.popen", "shell=True", "pickle",
    ):
        assert forbidden not in source, forbidden
    # ``_eval`` is the evaluator's own method; only the builtins are forbidden.
    assert not re.search(r"(?<![\w.])(?:eval|exec)\(", source)
