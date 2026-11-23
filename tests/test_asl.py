"""Edge cases and safety of the Amazon States Language (Step Functions) parser."""

import json
from pathlib import Path

import pytest

from code_review_graph import asl_parse
from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_ACCOUNT = "987654321098"
FAKE_TOKEN = "tok-PLANTED-0123456789"
FAKE_COMMENT = "PLANTED-COMMENT-do-not-index"
FAKE_VALUE = "planted-choice-value"
FAKE_PARAM = "planted-parameter-value"
PLANTED = (FAKE_ACCOUNT, FAKE_TOKEN, FAKE_COMMENT, FAKE_VALUE, FAKE_PARAM)


def parse(tmp_path, definition, name="machine.asl.json", raw=None):
    path = tmp_path / name
    path.write_text(raw if raw is not None else json.dumps(definition), encoding="utf-8")
    parser = CodeParser()
    nodes, edges = parser.parse_file(path)
    return nodes, edges, path


def states(nodes):
    return {n.identity_name: n for n in nodes if n.kind == "Function"}


def transitions(edges):
    return {
        (e.source.split("::", 1)[1], e.target.split("::", 1)[1], e.extra["via"])
        for e in edges
        if e.kind == "TRANSITIONS_TO"
    }


def test_invalid_json_keeps_only_the_file_node(tmp_path):
    nodes, edges, _ = parse(tmp_path, None, raw='{"StartAt": "A", "States": {')
    assert [n.kind for n in nodes] == ["File"]
    assert nodes[0].extra["asl_problem"] == "invalid_json"
    assert edges == []


@pytest.mark.parametrize("raw", ["", "[]", '"text"', "42", "{}", '{"States": []}'])
def test_non_state_machines_emit_no_states(tmp_path, raw):
    nodes, edges, _ = parse(tmp_path, None, raw=raw)
    assert [n.kind for n in nodes] == ["File"]
    assert edges == []


def test_empty_states_gives_a_machine_with_no_states(tmp_path):
    nodes, edges, _ = parse(tmp_path, {"StartAt": "A", "States": {}})
    assert [n.kind for n in nodes] == ["File", "Class"]
    assert nodes[1].extra["state_count"] == 0
    assert not [e for e in edges if e.kind == "TRANSITIONS_TO"]


def test_missing_start_at_flags_no_start_state(tmp_path):
    definition = {"States": {
        "A": {"Type": "Pass", "Next": "B"},
        "B": {"Type": "Succeed"},
    }}
    nodes, edges, _ = parse(tmp_path, definition)
    assert not any(n.extra["is_start"] for n in states(nodes).values())
    assert transitions(edges) == {("machine/A", "machine/B", "next")}


def test_transition_to_a_missing_state_is_counted_not_linked(tmp_path):
    definition = {"StartAt": "A", "States": {"A": {"Type": "Pass", "Next": "Nowhere"}}}
    nodes, edges, _ = parse(tmp_path, definition)
    assert transitions(edges) == set()
    assert next(n for n in nodes if n.kind == "Class").extra["unresolved_transitions"] == 1


def test_same_state_name_in_different_branches_is_unique(tmp_path):
    branch = {"StartAt": "Work", "States": {"Work": {"Type": "Succeed"}}}
    definition = {"StartAt": "Work", "States": {
        "Work": {"Type": "Parallel", "Branches": [branch, branch], "Next": "After"},
        "After": {"Type": "Map", "ItemProcessor": branch, "End": True},
    }}
    nodes, edges, _ = parse(tmp_path, definition)
    found = states(nodes)
    assert {"machine/Work", "machine/Work[0]/Work", "machine/Work[1]/Work",
            "machine/After", "machine/After[map]/Work"} == set(found)
    qualified = [n for n in (e.target for e in edges if e.kind == "CONTAINS")]
    assert len(qualified) == len(set(qualified))


def test_legacy_iterator_is_read_like_item_processor(tmp_path):
    inner = {"StartAt": "I", "States": {"I": {"Type": "Succeed"}}}
    definition = {"StartAt": "M", "States": {"M": {"Type": "Map", "Iterator": inner, "End": True}}}
    nodes, edges, _ = parse(tmp_path, definition)
    assert ("machine/M", "machine/M[map]/I", "map") in transitions(edges)


def test_state_names_with_separators_stay_distinct(tmp_path):
    definition = {"StartAt": "a/b", "States": {
        "a/b": {"Type": "Pass", "Next": "a[0]"},
        "a[0]": {"Type": "Succeed"},
    }}
    nodes, edges, _ = parse(tmp_path, definition)
    assert set(states(nodes)) == {"machine/a%2Fb", "machine/a%5B0%5D"}
    assert transitions(edges) == {("machine/a%2Fb", "machine/a%5B0%5D", "next")}


def test_nesting_beyond_the_cap_is_truncated(tmp_path):
    depth = asl_parse._MAX_ASL_NESTING + 4
    inner = {"StartAt": "Leaf", "States": {"Leaf": {"Type": "Succeed"}}}
    for level in range(depth):
        inner = {"StartAt": f"P{level}", "States": {
            f"P{level}": {"Type": "Parallel", "Branches": [inner], "End": True},
        }}
    nodes, _, _ = parse(tmp_path, inner)
    assert len(states(nodes)) <= asl_parse._MAX_ASL_NESTING + 1
    assert nodes[0].extra["asl_problem"] == "truncated"


def test_json_deeper_than_the_cap_does_not_crash(tmp_path):
    raw = '{"StartAt":"A","States":{"A":{"Type":"Pass","X":' + "[" * 5000 + "]" * 5000 + "}}}"
    nodes, _, _ = parse(tmp_path, None, raw=raw)
    assert nodes[0].kind == "File"


def test_state_count_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(asl_parse, "_MAX_ASL_STATES", 5)
    definition = {"StartAt": "S0", "States": {
        f"S{i}": {"Type": "Pass", "Next": f"S{i + 1}"} for i in range(20)
    }}
    nodes, _, _ = parse(tmp_path, definition)
    assert len(states(nodes)) == 5
    assert nodes[0].extra["asl_problem"] == "truncated"


def test_oversized_file_keeps_only_the_file_node(tmp_path, monkeypatch):
    monkeypatch.setattr(asl_parse, "_MAX_ASL_BYTES", 100)
    definition = {"StartAt": "A", "States": {"A": {"Type": "Succeed", "Comment": "x" * 200}}}
    nodes, edges, _ = parse(tmp_path, definition)
    assert [n.kind for n in nodes] == ["File"]
    assert nodes[0].extra["asl_problem"] == "too_large"
    assert edges == []


def test_placeholders_are_quoted_outside_strings_only():
    quote = asl_parse._asl_quote_placeholders
    source = b'{"a": ${x}, "b": "keep ${y} and \\"${z}\\"", "c": ${w.v}, "d": ${bad}}'
    assert quote(source) == (
        b'{"a": "${x}", "b": "keep ${y} and \\"${z}\\"", "c": "${w.v}", "d": "${bad}"}'
    )
    assert quote(b'{"a": 1}') == b'{"a": 1}'


@pytest.mark.parametrize(
    "hostile",
    [
        # Each escaped quote used to start another scan to the end of the line.
        b'${x}"' + b'\\"' * 400_000,
        b'${x}' + b'"a' * 400_000 + b'"',
        b'${x}"' * 200_000,
        b'${' * 400_000,
    ],
    ids=["escaped-quotes", "odd-quotes", "placeholder-quote-run", "dollar-brace-run"],
)
def test_placeholder_quoting_is_linear_on_hostile_lines(hostile):
    import time

    start = time.perf_counter()
    asl_parse._asl_quote_placeholders(hostile)
    assert time.perf_counter() - start < 3.0


def test_unterminated_strings_with_placeholders_stay_invalid_json(tmp_path):
    raw = '{"StartAt": "A", "States": {"A": ${x}"' + '\\"' * 100_000
    nodes, edges, _ = parse(tmp_path, None, raw=raw)
    assert [n.kind for n in nodes] == ["File"]
    assert nodes[0].extra["asl_problem"] == "invalid_json"
    assert edges == []


def test_huge_choice_and_catch_lists_are_bounded(tmp_path):
    cap = asl_parse._MAX_ASL_LIST_ITEMS
    definition = {"StartAt": "C", "States": {
        "C": {"Type": "Choice", "Default": "End", "Choices": [{"Next": "End"}] * (cap + 50)},
        "End": {"Type": "Succeed"},
    }}
    _, edges, _ = parse(tmp_path, definition)
    assert len([e for e in edges if e.extra.get("via") == "choice"]) <= cap


def test_unquoted_template_variables_are_read(tmp_path):
    raw = (
        '{"StartAt": "A", "States": {"A": {"Type": "Task", "Resource": ${fn_arn},\n'
        '"Next": "B"}, "B": {"Type": "Succeed"}}}'
    )
    nodes, edges, _ = parse(tmp_path, None, raw=raw)
    task = states(nodes)["machine/A"]
    assert task.extra["target_placeholders"] == ["fn_arn"]
    assert task.line_start == 1
    assert transitions(edges) == {("machine/A", "machine/B", "next")}


def test_comments_in_the_file_are_accepted(tmp_path):
    raw = '// header\n{"StartAt": "A", /* c */ "States": {"A": {"Type": "Succeed"}}}\n'
    nodes, _, _ = parse(tmp_path, None, raw=raw)
    assert set(states(nodes)) == {"machine/A"}


def test_arn_and_name_forms(tmp_path):
    def task(resource, **parameters):
        state = {"Type": "Task", "Resource": resource, "End": True}
        if parameters:
            state["Parameters"] = parameters
        return state

    lambda_arn = "arn:aws:lambda:eu-west-1:{}:function:%s"
    definition = {"StartAt": "T1", "States": {
        "T1": task(lambda_arn.format(FAKE_ACCOUNT) % "worker:7"),
        "T2": task("arn:aws:states:::lambda:invoke", FunctionName="plain-name:live"),
        "T3": task("arn:aws:states:::lambda:invoke", FunctionName="${fn_${env}}"),
        "T4": task("arn:aws:states:::lambda:invoke", **{"FunctionName.$": "$.name"}),
        "T5": task("arn:aws:states:::sqs:sendMessage",
                   QueueUrl=f"https://sqs.us-east-1.amazonaws.com/{FAKE_ACCOUNT}/jobs"),
        "T6": task("arn:aws:states:::ecs:runTask", TaskDefinition=(
            f"arn:aws:ecs:us-east-1:{FAKE_ACCOUNT}:task-definition/runner:12"),
            Cluster="main"),
        "T7": task("arn:aws:states:::dynamodb:putItem",
                   TableName=f"arn:aws:dynamodb:us-east-1:{FAKE_ACCOUNT}:table/events"),
        "T8": task("arn:aws:states:::aws-sdk:rds:describeDBInstances"),
        "T9": task("arn:aws:states:::elasticmapreduce:addStep.sync", **{"ClusterId.$": "$.id"}),
        "T10": task("arn:aws:lambda:${region}:${account}:function:fixed-name"),
        "T11": task("arn:aws:lambda:us-east-1:1:function:app-${stage}"),
    }}
    nodes, edges, _ = parse(tmp_path, definition)
    refs = {e.source.split("/")[-1]: e.target for e in edges if e.kind == "REFERENCES"}
    assert refs == {
        "T1": "external:aws-lambda:worker",
        "T2": "external:aws-lambda:plain-name",
        "T5": "external:aws-sqs:jobs",
        "T6": "external:aws-ecs:runner",
        "T7": "external:aws-dynamodb:events",
        "T10": "external:aws-lambda:fixed-name",
    }
    found = states(nodes)
    assert found["machine/T3"].extra["target_dynamic"] is True
    assert "target_placeholders" in found["machine/T3"].extra
    assert found["machine/T4"].extra["target_dynamic"] is True
    assert "target_placeholders" not in found["machine/T4"].extra
    assert found["machine/T8"].extra["resource_kind"] == "aws-sdk:rds"
    assert found["machine/T9"].extra["resource_kind"] == "elasticmapreduce"
    assert "target_dynamic" not in found["machine/T9"].extra
    assert found["machine/T11"].extra["target_placeholders"] == ["stage"]
    assert not any("app-" in repr(n.extra) for n in nodes)


def test_unknown_state_type_is_not_stored(tmp_path):
    definition = {"StartAt": "A", "States": {"A": {"Type": FAKE_TOKEN, "End": True}}}
    nodes, _, _ = parse(tmp_path, definition)
    assert states(nodes)["machine/A"].extra["state_type"] == "Unknown"


def test_jsonata_machine_records_the_query_language_only(tmp_path):
    definition = {"QueryLanguage": "JSONata", "StartAt": "A", "States": {
        "A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
              "Arguments": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:calc"},
              "End": True},
    }}
    nodes, edges, _ = parse(tmp_path, definition)
    assert next(n for n in nodes if n.kind == "Class").extra["query_language"] == "JSONata"
    assert [e.target for e in edges if e.kind == "REFERENCES"] == ["external:aws-lambda:calc"]


def _planted_definition():
    return {
        "Comment": FAKE_COMMENT,
        "StartAt": "Call",
        "States": {
            "Call": {
                "Type": "Task",
                "Comment": FAKE_COMMENT,
                "Resource": f"arn:aws:lambda:us-east-1:{FAKE_ACCOUNT}:function:worker",
                "Parameters": {"apiToken": FAKE_TOKEN, "Note": FAKE_PARAM},
                "InputPath": f"$.{FAKE_PARAM}",
                "ResultPath": f"$.{FAKE_PARAM}",
                "OutputPath": f"$.{FAKE_PARAM}",
                "Catch": [{"ErrorEquals": [FAKE_VALUE], "Next": "Route"}],
                "Next": "Route",
            },
            "Route": {
                "Type": "Choice",
                "Choices": [{
                    "Variable": f"$.{FAKE_PARAM}", "StringEquals": FAKE_VALUE, "Next": "Notify",
                }],
                "Default": "Notify",
            },
            "Notify": {
                "Type": "Task",
                "Resource": "arn:aws:states:::sns:publish",
                "Parameters": {
                    "TopicArn": f"arn:aws:sns:us-east-1:{FAKE_ACCOUNT}:alerts",
                    "Message": FAKE_TOKEN,
                },
                "Next": "Dyn",
            },
            "Dyn": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {
                    "FunctionName": f"States.Format('{FAKE_ACCOUNT}-{FAKE_TOKEN}')",
                    "Payload": {"x": FAKE_PARAM},
                },
                "Next": "Wait",
            },
            "Wait": {"Type": "Wait", "SecondsPath": f"$.{FAKE_PARAM}", "Next": "Fail"},
            "Fail": {"Type": "Fail", "Error": FAKE_VALUE, "Cause": FAKE_COMMENT},
        },
    }


def test_no_planted_value_reaches_any_node_or_edge(tmp_path):
    nodes, edges, _ = parse(tmp_path, _planted_definition())
    dump = repr(nodes) + repr(edges)
    for secret in PLANTED:
        assert secret not in dump, secret
    # The names the file does declare are still there.
    assert "external:aws-lambda:worker" in dump
    assert "external:aws-sns:alerts" in dump


def test_planted_values_in_a_placeholder_resource_are_dropped(tmp_path):
    raw = _planted_definition()
    raw["States"]["Call"]["Resource"] = "${call_arn}"
    nodes, edges, _ = parse(tmp_path, raw)
    dump = repr(nodes) + repr(edges)
    assert "call_arn" in dump
    for secret in PLANTED:
        assert secret not in dump, secret


def test_fixture_contains_no_value_text_in_the_graph():
    parser = CodeParser()
    path = FIXTURES / "sample.asl.json"
    nodes, edges = parser.parse_file(path)
    dump = repr(nodes) + repr(edges)
    for leaked in ("123456789012", "tok-FAKE-1234", "Synthetic order pipeline",
                   "secret cause text", "OrderRejected", "express", "$.total"):
        assert leaked not in dump, leaked


def test_output_is_deterministic(tmp_path):
    first = parse(tmp_path, _planted_definition())[:2]
    second = parse(tmp_path, _planted_definition())[:2]
    assert repr(first) == repr(second)
    path = FIXTURES / "sample.asl.json"
    a = CodeParser().parse_file(path)
    b = CodeParser().parse_file(path)
    assert repr(a) == repr(b)


def test_asl_file_never_gets_generic_json_nodes(tmp_path):
    definition = {"StartAt": "A", "States": {"A": {"Type": "Succeed"}}}
    nodes, _, _ = parse(tmp_path, definition, name="flow.asl.json")
    plain, _, _ = parse(tmp_path, definition, name="flow.json")
    assert not [n for n in nodes if n.kind == "JsonPath"]
    assert [n for n in plain if n.kind == "JsonPath"]


def test_dot_asl_extension_is_parsed_like_dot_asl_json(tmp_path):
    definition = {"StartAt": "A", "States": {"A": {"Type": "Succeed"}}}
    nodes, _, _ = parse(tmp_path, definition, name="flow.asl")
    assert next(n for n in nodes if n.kind == "Class").name == "flow"
    assert set(states(nodes)) == {"flow/A"}


def _build(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / ".git").mkdir(exist_ok=True)
    machine_dir = tmp_path / "pipelines" / "statemachine"
    machine_dir.mkdir(parents=True)
    (machine_dir / "orders.asl.json").write_text(
        (FIXTURES / "sample.asl.json").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text('{"name": "x"}', encoding="utf-8")
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    full_build(tmp_path, store)
    return store


def test_full_build_indexes_states_and_skips_generic_json(tmp_path):
    store = _build(tmp_path)
    try:
        path = (tmp_path / "pipelines" / "statemachine" / "orders.asl.json").as_posix()
        nodes = store.get_nodes_by_file(path)
        assert {n.kind for n in nodes} == {"File", "Class", "Function"}
        assert all(n.language == "asl" for n in nodes)
        edges = [e for e in store.get_all_edges() if e.file_path == path]
        assert {e.kind for e in edges} == {"CONTAINS", "REFERENCES", "TRANSITIONS_TO"}
        # A neighbouring plain JSON file still gets the generic treatment.
        plain = store.get_nodes_by_file((tmp_path / "settings.json").as_posix())
        assert any(n.kind == "JsonPath" for n in plain)
    finally:
        store.close()


def test_dead_code_ignores_states(tmp_path):
    from code_review_graph.refactor import find_dead_code

    store = _build(tmp_path)
    try:
        dead = find_dead_code(store)
        assert not [d for d in dead if d["file_path"].endswith(".asl.json")]
    finally:
        store.close()


def test_impact_tables_know_transitions_and_radius_runs_on_a_state_machine(tmp_path):
    from code_review_graph.constants import IMPACT_EDGE_DIRECTIONS, IMPACT_EDGE_WEIGHTS

    assert "TRANSITIONS_TO" in IMPACT_EDGE_WEIGHTS
    assert IMPACT_EDGE_DIRECTIONS["TRANSITIONS_TO"] == "incoming"
    store = _build(tmp_path)
    try:
        path = (tmp_path / "pipelines" / "statemachine" / "orders.asl.json").as_posix()
        result = store.get_impact_radius([path], max_depth=1)
        assert result["changed_nodes"]
    finally:
        store.close()
