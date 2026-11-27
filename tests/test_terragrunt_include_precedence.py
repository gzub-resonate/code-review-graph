"""Which ``inputs`` value a Terragrunt unit gets when an ``include`` and the unit both set it.

Documented semantics (https://docs.terragrunt.com/reference/hcl/blocks/include/): the unit's
own value overrides the included one; when two included files set the same value the later
``include`` wins; ``shallow`` (the default) replaces, ``deep`` also combines maps key by key,
``no_merge`` merges nothing; ``expose`` only makes the file readable as ``include.<name>``.
The fixtures imitate resonate/application-lambdas ``terraform/lambda.hcl`` (parent that sets
``function_name``) and ``terraform/tagStopDetector/prod/terragrunt.hcl`` (unit that overrides it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from code_review_graph.parser import CodeParser

UNIT = "terraform/tagStopDetector/prod/terragrunt.hcl"
PARENT = 'inputs = {\n  function_name = "parent-name"\n  handler = "app.handler"\n}\n'


def _info(tmp_path: Path, unit: str, **files: str) -> dict[str, Any]:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    for name, text in {"terraform/lambda.hcl": PARENT, **{
        k.replace("__", "/").replace("_hcl", ".hcl"): v for k, v in files.items()
    }}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    unit_path = root / UNIT
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")
    nodes, _edges = CodeParser(root).parse_file(unit_path)
    return nodes[0].extra.get("terragrunt_inputs", {})


def _include(extra: str = "", label: str = "", path: str = "terraform/lambda.hcl") -> str:
    head = f'include "{label}"' if label else "include"
    return f'{head} {{\n  path = "${{get_repo_root()}}/{path}"\n{extra}}}\n'


def _name(info: dict[str, Any]) -> Optional[str]:
    return info.get("function_name")


def test_the_unit_value_wins_over_the_included_one(tmp_path: Path) -> None:
    unit = _include() + 'inputs = {\n  function_name = "tag-stop-detector"\n}\n'
    assert _name(_info(tmp_path, unit)) == "tag-stop-detector"


def test_an_included_value_is_used_when_the_unit_does_not_set_it(tmp_path: Path) -> None:
    assert _name(_info(tmp_path, _include() + "inputs = {\n}\n")) == "parent-name"


@pytest.mark.parametrize("strategy", ["shallow", "deep"])
def test_the_unit_wins_under_shallow_and_deep(tmp_path: Path, strategy: str) -> None:
    unit = _include(f'  merge_strategy = "{strategy}"\n') + (
        'inputs = {\n  function_name = "child-name"\n}\n'
    )
    assert _name(_info(tmp_path, unit)) == "child-name"


def test_expose_does_not_change_which_value_wins(tmp_path: Path) -> None:
    unit = _include("  expose = true\n", "root") + 'inputs = {\n  function_name = "child-name"\n}\n'
    assert _name(_info(tmp_path, unit)) == "child-name"


def test_no_merge_ignores_the_included_inputs(tmp_path: Path) -> None:
    unit = _include('  merge_strategy = "no_merge"\n') + (
        'inputs = {\n  handler = "app.handler"\n}\n'
    )
    assert _name(_info(tmp_path, unit)) is None


def test_the_later_include_wins_over_an_earlier_one(tmp_path: Path) -> None:
    second = 'inputs = {\n  function_name = "second-name"\n}\n'
    unit = (
        _include(label="a")
        + _include(label="b", path="terraform/second.hcl")
        + 'inputs = {\n  handler = "app.handler"\n}\n'
    )
    info = _info(tmp_path, unit, terraform__second_hcl=second)
    assert _name(info) == "second-name"


def test_a_no_merge_include_is_skipped_but_the_other_still_merges(tmp_path: Path) -> None:
    second = 'inputs = {\n  function_name = "second-name"\n}\n'
    unit = (
        _include(label="a")
        + _include('  merge_strategy = "no_merge"\n', "b", "terraform/second.hcl")
        + 'inputs = {\n  handler = "app.handler"\n}\n'
    )
    info = _info(tmp_path, unit, terraform__second_hcl=second)
    assert _name(info) == "parent-name"


# -- map inputs (replacement_vars): shallow replaces, deep combines key by key -----------------


def _arn(name: str) -> str:
    return f"arn:aws:lambda:us-east-1:1:function:{name}"


def _vars(info: dict[str, Any]) -> dict[str, str]:
    return {r["key"]: r["name"] for r in info.get("replacement_vars", [])}


def _map(**entries: str) -> str:
    body = "".join(f'    {k} = "{_arn(v)}"\n' for k, v in entries.items())
    return f"inputs = {{\n  replacement_vars = {{\n{body}  }}\n}}\n"


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("shallow", {"A": "child-a"}),
        ("deep", {"A": "child-a", "B": "parent-b"}),
        ("no_merge", {"A": "child-a"}),
    ],
)
def test_map_entries_follow_the_merge_strategy(
    tmp_path: Path, strategy: str, expected: dict[str, str],
) -> None:
    unit = _include(f'  merge_strategy = "{strategy}"\n') + _map(A="child-a")
    info = _info(tmp_path, unit, terraform__lambda_hcl=_map(A="parent-a", B="parent-b"))
    assert _vars(info) == expected


def test_a_later_include_wins_for_a_map_entry_under_deep(tmp_path: Path) -> None:
    unit = (
        _include('  merge_strategy = "deep"\n', "a")
        + _include('  merge_strategy = "deep"\n', "b", "terraform/second.hcl")
        + "inputs = {\n}\n"
    )
    info = _info(
        tmp_path, unit,
        terraform__lambda_hcl=_map(A="first-a", B="first-b"),
        terraform__second_hcl=_map(A="second-a"),
    )
    assert _vars(info) == {"A": "second-a", "B": "first-b"}
