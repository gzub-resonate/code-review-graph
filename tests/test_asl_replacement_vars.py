"""A ``${placeholder}`` Task target is named from the deploying Terragrunt unit's inputs.

Most Step Functions definitions do not spell what a Task calls: the target is a
``${key}`` that the Terragrunt unit fills from ``replacement_vars``. The parser reads an
allowlist of ``inputs`` keys, reduces each value to naming data, and ``asl_resolver`` adds a
``REFERENCES`` edge from the state to ``external:aws-<service>:<name>``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.asl_resolver import resolve_asl_local_targets
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import incremental_update
from code_review_graph.infra_references import (
    TERRAGRUNT_INPUT_KEYS,
    reduce_arn,
    reduce_lambda_handler,
    reduce_replacement_var,
)
from code_review_graph.parser import CodeParser

from .asl_links_helpers import (
    FILES,
    MACHINE_PATH,
    build_repo,
    derived_edges,
    machine_unit,
    state,
    write_files,
)

RV = "asl_replacement_vars"


def replacement_targets(store: GraphStore, root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for source, target, resolution, tier, confidence in derived_edges(store, root):
        if resolution == RV:
            assert (tier, confidence) == ("EXTRACTED", 1.0)
            found.setdefault(source, set()).add(target)
    return found


class TestReducers:
    def test_an_arn_keeps_only_service_kind_and_name(self):
        assert reduce_arn("arn:aws:lambda:us-east-1:111111111111:function:do-it:LIVE") == (
            "lambda", "function", "do-it",
        )
        assert reduce_arn("arn:aws:ecs:eu-west-1:1:task-definition/job:7") == (
            "ecs", "task-definition", "job",
        )
        assert reduce_arn("arn:aws:sns:us-east-1:1:alerts") == ("sns", "topic", "alerts")
        assert reduce_arn("arn:aws:states:us-east-1:1:stateMachine:child") == (
            "states", "state-machine", "child",
        )

    def test_an_arn_of_another_service_or_shape_is_not_reduced(self):
        assert reduce_arn("arn:aws:secretsmanager:us-east-1:1:secret:db-password") is None
        assert reduce_arn("arn:aws:iam::1:role/Admin") is None
        assert reduce_arn("arn:aws:lambda:us-east-1:1:layer:tools:3") is None
        assert reduce_arn("arn:evil:lambda:us-east-1:1:function:x") is None

    def test_an_unevaluated_account_is_dropped_but_an_unevaluated_name_is_refused(self):
        hole = "\x00"
        assert reduce_replacement_var(
            "replacement_vars", "Fn", f"arn:aws:lambda:{hole}:{hole}:function:do-it",
        ) == {"key": "Fn", "service": "lambda", "kind": "function", "name": "do-it"}
        assert reduce_replacement_var(
            "replacement_vars", "Fn", f"arn:aws:lambda:us-east-1:1:function:do-{hole}",
        ) is None

    @pytest.mark.parametrize("value", [
        "subnet-0abc1234", "team@example.com", "my-bucket", "PROD",
        "postgres://admin:hunter2@db.example.com/orders",
        "https://user:token@example.com/x", "hunter2",
    ])
    def test_a_plain_value_of_the_untyped_map_is_skipped(self, value):
        assert reduce_replacement_var("replacement_vars", "Key", value) is None

    def test_a_typed_map_holds_plain_names_of_its_service(self):
        assert reduce_replacement_var("lambda_replacement_vars", "Fn", "add-hour") == {
            "key": "Fn", "service": "lambda", "kind": "function", "name": "add-hour",
        }
        assert reduce_replacement_var("sns_topic_replacement_vars", "T", "alerts")["service"] == (
            "sns"
        )
        assert reduce_replacement_var("lambda_replacement_vars", "Fn", "not a name!") is None
        assert reduce_replacement_var(
            "lambda_replacement_vars", "Fn", "arn:aws:sns:us-east-1:1:alerts",
        ) is None

    @pytest.mark.parametrize("key", ["ApiSecret", "db_password", "SlackToken", "API_KEY"])
    def test_a_secret_looking_key_is_skipped_whatever_its_value(self, key):
        arn = "arn:aws:lambda:us-east-1:1:function:fine"
        assert reduce_replacement_var("replacement_vars", key, arn) is None
        assert reduce_replacement_var("lambda_replacement_vars", key, "fine") is None

    def test_a_handler_is_module_dot_function_only(self):
        assert reduce_lambda_handler("app.handler") == "app.handler"
        assert reduce_lambda_handler("pkg/app.handler") == "pkg/app.handler"
        assert reduce_lambda_handler("com.acme.Handler::handleRequest") is None
        assert reduce_lambda_handler("https://user:pw@host/x.y") is None
        assert reduce_lambda_handler("handler") is None

    def test_the_allowlist_is_exactly_these_keys(self):
        assert TERRAGRUNT_INPUT_KEYS == {
            "step_function_file_path", "name", "replacement_vars", "lambda_replacement_vars",
            "step_function_replacement_vars", "sns_topic_replacement_vars",
            "handler", "function_name", "git_repo_sub_dir",
        }


class TestParser:
    def test_a_unit_records_naming_data_only(self, tmp_path):
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        write_files(root, FILES)
        unit = root / "terraform/step-functions/orders/prod/terragrunt.hcl"
        nodes, edges = CodeParser(root).parse_file(unit)
        info = nodes[0].extra["terragrunt_inputs"]
        assert info["state_machine_file"] == (root / MACHINE_PATH).as_posix()
        assert info["replacement_vars"] == [
            {"key": "AuditFunctionArn", "service": "lambda", "kind": "function",
             "name": "audit-trail"},
            {"key": "ChildMachineArn", "service": "states", "kind": "state-machine",
             "name": "child-machine"},
            {"key": "TopicArn", "service": "sns", "kind": "topic", "name": "alerts"},
            {"key": "ValidateFunctionArn", "service": "lambda", "kind": "function",
             "name": "validateOrder"},
        ]
        assert set(info) == {"state_machine_file", "state_machine_name", "replacement_vars"}
        assert info["state_machine_name"] == "orders"

    def test_no_raw_value_account_id_or_secret_reaches_a_node_or_an_edge(self, tmp_path):
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        write_files(root, FILES)
        parser = CodeParser(root)
        blob = ""
        for rel in FILES:
            if rel.endswith(".hcl"):
                nodes, edges = parser.parse_file(root / rel)
                blob += json.dumps([[n.name, n.extra] for n in nodes])
                blob += json.dumps([[e.source, e.target, e.extra] for e in edges])
        for leaked in (
            "111111111111", "222222222222", "us-east-1", "arn:aws", "hunter2", "postgres",
            "team@example.com", "subnet-0abc1234", "correct-horse", "tok_live",
            "looksLikeASecret", "ApiSecret", "PLATFORM", "python3.13", "A function",
        ):
            assert leaked not in blob, leaked

    def test_an_unlisted_key_is_never_evaluated(self, tmp_path, monkeypatch):
        from code_review_graph import terragrunt_static

        root = tmp_path.resolve()
        (root / ".git").mkdir()
        write_files(root, FILES)
        seen: list[str] = []
        original = terragrunt_static.TerragruntEvaluator._evaluate_in

        def spy(self, expr, frame, holes=False):
            seen.append(expr.text.decode())
            return original(self, expr, frame, holes)

        monkeypatch.setattr(terragrunt_static.TerragruntEvaluator, "_evaluate_in", spy)
        parser = CodeParser(root)
        for rel in FILES:
            if rel.endswith("terragrunt.hcl"):
                parser.parse_file(root / rel)
        text = "\n".join(seen)
        assert "validateOrder" in text  # the allowlisted map was read
        for unlisted in ("correct-horse", "tok_live", "python3.13", "PLATFORM", "A function"):
            assert unlisted not in text, unlisted

    def test_an_expression_that_cannot_be_reduced_leaves_no_entry(self, tmp_path):
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        files = dict(FILES)
        files["terraform/step-functions/orders/prod/terragrunt.hcl"] = '''include "base" {
  path = find_in_parent_folders()
}

inputs = {
  replacement_vars = {
    FromEnv   = get_env("FUNCTION_ARN")
    FromCmd   = run_cmd("aws", "sts", "get-caller-identity")
    FromDep   = dependency.fn.outputs.arn
    Account   = "arn:aws:lambda:us-east-1:${get_aws_account_id()}:function:known-name"
    NamePart  = "arn:aws:lambda:us-east-1:1:function:${get_env("NAME")}"
  }
}
'''
        write_files(root, files)
        unit = root / "terraform/step-functions/orders/prod/terragrunt.hcl"
        nodes, _edges = CodeParser(root).parse_file(unit)
        assert nodes[0].extra["terragrunt_inputs"]["replacement_vars"] == [
            {"key": "Account", "service": "lambda", "kind": "function", "name": "known-name"},
        ]

    def test_more_includes_than_may_be_read_leaves_the_unit_dynamic(self, tmp_path):
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        files = dict(FILES)
        includes = "".join(
            f'include "p{i}" {{\n  path = "${{get_repo_root()}}/terraform/p{i}.hcl"\n}}\n'
            for i in range(4)
        )
        for i in range(4):
            files[f"terraform/p{i}.hcl"] = "inputs = {\n  team = \"x\"\n}\n"
        files["terraform/p3.hcl"] = MACHINE_BASE_IN_PARENT
        files["terraform/step-functions/orders/prod/terragrunt.hcl"] = includes + '''
inputs = {
  replacement_vars = {
    ValidateFunctionArn = "arn:aws:lambda:us-east-1:1:function:validateOrder"
  }
}
'''
        write_files(root, files)
        unit = root / "terraform/step-functions/orders/prod/terragrunt.hcl"
        nodes, _edges = CodeParser(root).parse_file(unit)
        info = nodes[0].extra["terragrunt_inputs"]
        assert info["dynamic_reason"] == "depth_limit"
        assert "state_machine_file" not in info


MACHINE_BASE_IN_PARENT = '''inputs = {
  step_function_file_path = "step-functions/orders/statemachine/orders.asl.json"
}
'''


class TestResolver:
    def test_each_placeholder_state_points_at_what_the_units_name(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            targets = replacement_targets(store, root)
            assert targets == {
                state("Validate"): {"external:aws-lambda:validateOrder"},
                # One name per environment unit: one edge per distinct name.
                state("Notify"): {"external:aws-sns:alerts", "external:aws-sns:alerts-dev"},
                state("Child"): {"external:aws-states:child-machine"},
                state("Typed"): {"external:aws-lambda:audit-trail"},
            }
            row = store._conn.execute(
                "SELECT extra FROM edges WHERE target_qualified = 'external:aws-sns:alerts'"
            ).fetchone()
            extra = json.loads(row[0])
            assert extra["placeholder"] == "TopicArn"
            assert extra["resource_kind"] == "sns" and extra["target_kind"] == "topic"
        finally:
            store.close()

    def test_the_pass_reports_what_it_linked_and_what_stays_open(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            stats = resolve_asl_local_targets(store)
            assert stats["replacement_links"] == 5
            assert stats["placeholder_states_linked"] == 4
            assert stats["placeholder_states_open"] == 1  # ${NobodyFillsThis}
            assert stats["include_depth_limit"] == 0
        finally:
            store.close()

    def test_running_the_pass_again_changes_nothing(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            before = derived_edges(store, root)
            resolve_asl_local_targets(store)
            resolve_asl_local_targets(store)
            assert derived_edges(store, root) == before
        finally:
            store.close()

    def test_an_update_gives_what_a_full_build_gives(self, tmp_path):
        changed = dict(FILES)
        changed["terraform/step-functions/orders/nonprod/terragrunt.hcl"] = machine_unit(
            "222222222222", "renamed-topic",
        )
        changed[MACHINE_PATH] = FILES[MACHINE_PATH].replace("${NobodyFillsThis}", "${TopicArn}")

        full_root = tmp_path / "full" / "repo"
        full = build_repo(full_root, changed)
        try:
            expected = derived_edges(full, full_root)
        finally:
            full.close()

        root = tmp_path / "inc" / "repo"
        store = build_repo(root)
        try:
            before = derived_edges(store, root)
            write_files(root, {
                rel: text for rel, text in changed.items() if FILES[rel] != text
            })
            incremental_update(root, store, changed_files=[
                "terraform/step-functions/orders/nonprod/terragrunt.hcl", MACHINE_PATH,
            ])
            after = derived_edges(store, root)
        finally:
            store.close()
        assert after == expected
        assert after != before
        assert (state("Unfilled"), "external:aws-sns:renamed-topic", RV, "EXTRACTED", 1.0) in after
        assert not any("alerts-dev" in edge[1] for edge in after)
