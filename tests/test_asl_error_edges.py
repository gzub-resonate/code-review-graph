"""Catch, Retry and Default detail on Step Functions transition edges."""

import json

from code_review_graph.parser import CodeParser

LONG = "E" * 65


def build(tmp_path, definition):
    path = tmp_path / "m.asl.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    return CodeParser().parse_file(path)


def machine():
    return {
        "StartAt": "Work",
        "States": {
            "Work": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Retry": [{"ErrorEquals": ["States.Timeout"], "MaxAttempts": 99}],
                "Catch": [{
                    "ErrorEquals": [
                        "States.ALL", "Lambda.ServiceException", LONG, "bad name!",
                        "x$(rm)", 7, "A", "B", "C", "D",
                    ],
                    "Next": "Recover",
                    "ResultPath": "$.PLANTED-PATH",
                    "Cause": "PLANTED-CAUSE",
                }],
                "Next": "Decide",
            },
            "Recover": {"Type": "Fail"},
            "Decide": {
                "Type": "Choice",
                "Choices": [{
                    "Variable": "$.PLANTED-VAR", "StringEquals": "PLANTED-VALUE",
                    "Next": "Done",
                }],
                "Default": "Recover",
            },
            "Done": {"Type": "Succeed"},
        },
    }


def edges_by_via(edges):
    return {
        e.extra["via"]: e for e in edges if e.kind == "TRANSITIONS_TO"
    }


def test_catch_edge_has_bounded_error_equals(tmp_path):
    _, edges = build(tmp_path, machine())
    catch = edges_by_via(edges)["catch"]
    assert catch.extra["error_equals"] == [
        "States.ALL", "Lambda.ServiceException", "A", "B", "C",
    ]


def test_long_and_odd_error_names_are_dropped(tmp_path):
    _, edges = build(tmp_path, machine())
    names = edges_by_via(edges)["catch"].extra["error_equals"]
    assert LONG not in names and "bad name!" not in names and "x$(rm)" not in names


def test_state_flags(tmp_path):
    nodes, _ = build(tmp_path, machine())
    work = next(n for n in nodes if n.name == "Work")
    assert work.extra["has_retry"] is True
    assert work.extra["has_catch"] is True
    done = next(n for n in nodes if n.name == "Done")
    assert "has_retry" not in done.extra and "has_catch" not in done.extra


def test_default_edge_flagged_and_choice_edge_not(tmp_path):
    _, edges = build(tmp_path, machine())
    by_via = edges_by_via(edges)
    assert by_via["default"].extra["default"] is True
    assert "default" not in by_via["choice"].extra
    assert "error_equals" not in by_via["next"].extra


def test_nothing_else_is_copied(tmp_path):
    nodes, edges = build(tmp_path, machine())
    blob = json.dumps(
        [e.extra for e in edges] + [n.extra for n in nodes], default=str,
    )
    for planted in ("PLANTED", "MaxAttempts", "ResultPath", "StringEquals"):
        assert planted not in blob
    catch = edges_by_via(edges)["catch"]
    assert set(catch.extra) == {"via", "index", "error_equals"}


def test_empty_retry_does_not_set_flag(tmp_path):
    nodes, _ = build(tmp_path, {
        "StartAt": "A",
        "States": {"A": {"Type": "Pass", "Retry": [], "End": True}},
    })
    assert "has_retry" not in next(n for n in nodes if n.name == "A").extra
