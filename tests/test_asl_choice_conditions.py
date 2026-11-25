"""The test of a Step Functions Choice rule, read on demand and never stored.

The graph indexes state machines value-free (docs/schema.md: "Choice conditions, variables and
values are never stored"), so a ``choice`` edge says which branch it is (``index``) and nothing
about the test. ``next_states_of`` reads the test from the file when it is asked, with the same
caps the build applies, and only while the file is the one that was indexed.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.asl_parse import read_choice_conditions
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _machine(choices):
    return {
        "StartAt": "Choose",
        "States": {
            "Choose": {"Type": "Choice", "Choices": choices, "Default": "Resize"},
            "Scale": {"Type": "Pass", "Next": "Resize"},
            "Resize": {"Type": "Pass", "End": True},
        },
    }


def _text(definition) -> bytes:
    # Pretty-printed, as a real definition is: each rule on its own line.
    return json.dumps(definition, indent=2).encode()


def _conditions(choices) -> dict[int, str | None]:
    """The test of each rule, by its index in ``Choices``."""
    table = read_choice_conditions(_text(_machine(choices)), CodeParser()._get_parser("json"))
    return {index: text for (_line, index), text in table.items()}


def test_a_comparison_is_written_as_variable_operator_value() -> None:
    got = _conditions([{"Variable": "$.mode", "StringEquals": "scale_down", "Next": "Scale"}])
    assert got == {0: '$.mode StringEquals "scale_down"'}


def test_numbers_booleans_and_paths_are_not_quoted() -> None:
    got = _conditions([
        {"Variable": "$.n", "NumericGreaterThan": 3, "Next": "Scale"},
        {"Variable": "$.f", "NumericLessThanEquals": 2.5, "Next": "Scale"},
        {"Variable": "$.on", "BooleanEquals": True, "Next": "Scale"},
        {"Variable": "$.a", "StringEqualsPath": "$.b", "Next": "Scale"},
    ])
    assert got[0] == "$.n NumericGreaterThan 3"
    assert got[1] == "$.f NumericLessThanEquals 2.5"
    assert got[2] == "$.on BooleanEquals true"
    assert got[3] == "$.a StringEqualsPath $.b"


def test_and_or_not_combine_their_terms() -> None:
    got = _conditions([
        {
            "And": [
                {"Variable": "$.a", "BooleanEquals": True},
                {"Not": {"Variable": "$.b", "IsPresent": True}},
            ],
            "Next": "Scale",
        },
        {
            "Or": [
                {"Variable": "$.c", "StringEquals": "x"},
                {"Variable": "$.c", "StringEquals": "y"},
            ],
            "Next": "Scale",
        },
    ])
    assert got[0] == "($.a BooleanEquals true and not $.b IsPresent true)"
    assert got[1] == '($.c StringEquals "x" or $.c StringEquals "y")'


def test_a_jsonata_condition_is_kept_as_written() -> None:
    got = _conditions([{"Condition": "{% $states.input.mode = 'scale' %}", "Next": "Scale"}])
    assert got[0] == "{% $states.input.mode = 'scale' %}"


def test_a_crafted_rule_cannot_make_the_text_large() -> None:
    nested = {"Variable": "$.x", "StringEquals": "v"}
    for _ in range(10):
        nested = {"Not": nested}
    got = _conditions([
        {"Variable": "$.v", "StringEquals": "V" * 500 + "\n\x00planted", "Next": "Scale"},
        {"And": [{"Variable": "$.t", "StringEquals": "t"}] * 40, "Next": "Scale"},
        {**nested, "Next": "Scale"},
    ])
    for index in range(3):
        text = got[index]
        assert text is not None and len(text) <= 160
        assert "\n" not in text and "\x00" not in text
    assert got[1].count("$.t StringEquals") <= 6


def test_a_number_that_is_not_plain_text_shows_a_question_mark_never_zero() -> None:
    # A valid JSON number too long to be shown as plain text (25 digits).
    source = _text(_machine([{"Variable": "$.n", "NumericEquals": 12345, "Next": "Scale"}]))
    table = read_choice_conditions(
        source.replace(b"12345", b"1" * 25), CodeParser()._get_parser("json"),
    )
    (text,) = table.values()
    assert text == "$.n NumericEquals ?"


def test_a_rule_with_nothing_readable_has_no_text() -> None:
    assert _conditions([{"Next": "Scale"}]) == {0: None}


def test_two_rules_on_one_line_are_never_guessed() -> None:
    one_line = json.dumps(_machine([
        {"Variable": "$.m", "StringEquals": "a", "Next": "Scale"},
        {"Variable": "$.m", "StringEquals": "b", "Next": "Scale"},
    ])).encode()
    table = read_choice_conditions(one_line, CodeParser()._get_parser("json"))
    # Different indexes, so each is keyed by its own (line, index).
    assert {text for text in table.values()} == {
        '$.m StringEquals "a"', '$.m StringEquals "b"',
    }


def test_the_graph_stores_no_condition_text(tmp_path: Path) -> None:
    path = tmp_path / "m.asl.json"
    path.write_bytes(_text(_machine([
        {"Variable": "$.PLANTED-VAR", "StringEquals": "PLANTED-VALUE", "Next": "Scale"},
    ])))
    nodes, edges = CodeParser().parse_file(path)
    dump = repr(nodes) + repr(edges)
    assert "PLANTED" not in dump
    choice = next(e for e in edges if e.kind == "TRANSITIONS_TO" and e.extra["via"] == "choice")
    assert "condition" not in choice.extra and choice.extra["index"] == 0


def _built(tmp_path: Path, choices) -> tuple[str, Path]:
    (tmp_path / ".git").mkdir(exist_ok=True)
    folder = tmp_path / "pipelines" / "statemachine"
    folder.mkdir(parents=True)
    machine_file = folder / "m.asl.json"
    machine_file.write_bytes(_text(_machine(choices)))
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    full_build(tmp_path, store)
    store.close()
    return str(tmp_path), machine_file


def test_next_states_of_returns_each_branch_with_its_condition(tmp_path: Path) -> None:
    root, _ = _built(tmp_path, [
        {"Variable": "$.mode", "StringEquals": "scale_down", "Next": "Scale"},
    ])
    result = query_graph("next_states_of", "Choose", repo_root=root)
    rows = {e["via"]: e for e in result["edges"]}
    assert rows["choice"]["condition"] == '$.mode StringEquals "scale_down"'
    assert rows["choice"]["index"] == 0
    assert "condition" not in rows["default"]
    assert "condition_unavailable" not in rows["default"]


def test_two_rules_into_one_state_keep_both_conditions(tmp_path: Path) -> None:
    root, _ = _built(tmp_path, [
        {"Variable": "$.mode", "StringEquals": "a", "Next": "Scale"},
        {"Variable": "$.mode", "StringEquals": "b", "Next": "Scale"},
        {"Variable": "$.mode", "StringEquals": "c", "Next": "Resize"},
    ])
    result = query_graph("next_states_of", "Choose", repo_root=root)
    to_scale = next(e for e in result["edges"] if e["target"].endswith("/Scale"))
    assert to_scale["conditions"] == ['$.mode StringEquals "a"', '$.mode StringEquals "b"']
    # The Default and rule 3 both reach Resize: the row is one of them, and rule 3 is not lost.
    to_resize = next(e for e in result["edges"] if e["target"].endswith("/Resize"))
    assert '$.mode StringEquals "c"' in (
        [to_resize.get("condition")] + to_resize.get("conditions", [])
    )


def test_a_file_changed_after_indexing_gives_no_condition_and_says_why(tmp_path: Path) -> None:
    root, machine_file = _built(tmp_path, [
        {"Variable": "$.mode", "StringEquals": "scale_down", "Next": "Scale"},
    ])
    # The rule's line and index would now name a different test than the edge was built from.
    machine_file.write_bytes(_text(_machine([
        {"Variable": "$.other", "StringEquals": "changed", "Next": "Scale"},
    ])).replace(b"changed", b"changed "))
    result = query_graph("next_states_of", "Choose", repo_root=root)
    choice = next(e for e in result["edges"] if e["via"] == "choice")
    assert "condition" not in choice
    assert "changed since it was indexed" in choice["condition_unavailable"]
    # Nothing of the edited rule leaks into the answer.
    assert "$.other" not in json.dumps(choice)


def test_credential_shaped_operands_are_redacted_not_trimmed() -> None:
    got = _conditions([
        {"Variable": "$.k", "StringEquals": "AKIAABCDEFGHIJKLMNOP", "Next": "Scale"},
        {"Variable": "$.t", "StringEquals": "ghp_" + "a1" * 18, "Next": "Scale"},
        {"Variable": "$.a", "StringEquals": "123456789012", "Next": "Scale"},
        {"Variable": "$.h", "StringEquals": "Bearer abcdefghijklmnop", "Next": "Scale"},
        {"Variable": "$.u", "StringEquals": "https://user:pw@host.example.net/x", "Next": "Scale"},
        {"Variable": "$.m", "StringEquals": "scale_down", "Next": "Scale"},
        {"Condition": "{% $states.input.token = 'sk-abcdefghijklmnopqrstuvwxyz0123' %}",
         "Next": "Scale"},
        {"Variable": "$.p", "StringEqualsPath": "$.otherField", "Next": "Scale"},
    ])
    for index in (0, 1, 2, 3, 4):
        assert got[index] == f"$.{'ktahu'[index]} StringEquals \"<redacted>\""
    assert got[5] == '$.m StringEquals "scale_down"'
    assert got[6] == "<redacted>"
    assert got[7] == "$.p StringEqualsPath $.otherField"
    text = " ".join(t for t in got.values() if t)
    for leaked in ("AKIA", "ghp_", "123456789012", "Bearer", "user:pw", "sk-abc"):
        assert leaked not in text


def test_a_credential_cannot_hide_behind_characters_that_are_removed_afterwards() -> None:
    """The check runs on the text that is printed, not on a raw value with invisible characters."""
    got = _conditions([
        {"Variable": "$.a", "StringEquals": "AKIA\x00ABCDEFGHIJKLMNOP", "Next": "Scale"},
        {"Variable": "$.b", "StringEquals": "ghp_​" + "a1" * 18, "Next": "Scale"},
        {"Variable": "$.c", "StringEquals": "ｇｈｐ_" + "a1" * 18, "Next": "Scale"},
        {"Variable": "$.d", "StringEquals": "1234⁠567890‍12", "Next": "Scale"},
        {"Variable": "$.e", "StringEquals": "scale​_down", "Next": "Scale"},
    ])
    for index in range(4):
        assert got[index].endswith('"<redacted>"'), got[index]
    # An ordinary value loses only the invisible character.
    assert got[4] == '$.e StringEquals "scale_down"'
    joined = " ".join(t for t in got.values() if t)
    assert "AKIA" not in joined and "ghp_" not in joined and "123456" not in joined


def test_the_variable_is_cleaned_before_it_is_shown() -> None:
    got = _conditions([{"Variable": "$.mo\x00de​", "StringEquals": "x", "Next": "Scale"}])
    assert got[0] == '$.mode StringEquals "x"'
