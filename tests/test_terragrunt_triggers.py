"""Triggers a Terragrunt Lambda unit declares in ``inputs`` (``via == "terragrunt_inputs"``).

A real-shaped unit (``verticalRefreshNotification``): the rule name and its schedule are kept,
the rule's ``input`` document, its description and a secret-looking name are never stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_review_graph import trigger_links
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

from .asl_links_helpers import LAMBDA_HCL, write_files

HOOK = "https://hooks." + "slack.com/" + "services/T000/B000/abcDEF123456"
SECRET_INPUT = (
    '{\\"slack_url\\": \\"' + HOOK + '\\", '
    '\\"api_token\\": \\"tok_live_do_not_store\\"}'
)

VERTICAL = f'''include {{
  path = find_in_parent_folders("lambda.hcl")
}}

inputs = {{
  description = "Sends a vertical refresh notification"
  handler     = "verticalRefreshNotification.handler"
  runtime     = "python3.13"
  aws_cloudwatch_event_rules = [
    {{
      name                = "vertical-refresh-notification"
      description         = "Run vertical refresh notification lambda"
      event_pattern       = null
      input               = "{SECRET_INPUT}"
      is_enabled          = true
      schedule_expression = "rate(5 minutes)"
    }}
  ]
}}
'''

IGNORE_CHANGES = '''include {
  path = find_in_parent_folders("lambda.hcl")
}

inputs = {
  handler = "checkHealth.handler"
  aws_cloudwatch_event_rules_ignore_changes = [
    {
      name                = "check-health-dev2"
      event_pattern       = null
      is_enabled          = false
      schedule_expression = "cron(30 13 ? * * *)"
    },
    {
      name                = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
      schedule_expression = "rate(1 minute)"
    },
    {
      name                = "orders-created"
      event_pattern       = "{\\"source\\": [\\"app.orders\\", \\"bad source!\\"]}"
    }
  ]
}
'''

TOPICS = '''include {
  path = find_in_parent_folders("lambda.hcl")
}

inputs = {
  handler             = "datapipelineMetrics.handler"
  existing_sns_topics = ["pipeline-fail-prod", "pipeline-late-prod"]
}
'''

QUEUED = '''include {
  path = find_in_parent_folders("lambda.hcl")
}

inputs = {
  handler             = "drain.handler"
  include_sqs         = true
  sqs_name            = "ec2GradualDrainingSqs"
  existing_sns_topics = ["scale-in"]
}
'''

PLAIN = '''include {
  path = find_in_parent_folders("lambda.hcl")
}

inputs = {
  handler = "plain.handler"
}
'''

FILES = {
    "terraform/lambda.hcl": LAMBDA_HCL,
    "terraform/python-lambdas/verticalRefreshNotification/prod/terragrunt.hcl": VERTICAL,
    "terraform/python-lambdas/checkHealth/nonprod/terragrunt.hcl": IGNORE_CHANGES,
    "terraform/python-lambdas/datapipelineMetrics/prod/terragrunt.hcl": TOPICS,
    "terraform/python-lambdas/drain/prod/terragrunt.hcl": QUEUED,
    "terraform/python-lambdas/plain/prod/terragrunt.hcl": PLAIN,
}


def _store(tmp_path: Path) -> GraphStore:
    root = tmp_path.resolve()
    (root / ".git").mkdir(exist_ok=True)
    write_files(root, FILES)
    parser = CodeParser(root)
    store = GraphStore(root / "graph.db")
    for rel in FILES:
        nodes, edges = parser.parse_file(root / rel)
        store.store_file_nodes_edges(str(root / rel), nodes, edges)
    return store


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    store = _store(tmp_path)
    try:
        return [r for r in trigger_links.read_triggers(store)
                if r["via"] == trigger_links.VIA_TERRAGRUNT]
    finally:
        store.close()


def test_the_vertical_refresh_rule_fires_its_function(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    row = next(r for r in rows if r["source"] == "vertical-refresh-notification")
    assert row["trigger_kind"] == "eventbridge_rule"
    assert row["source_service"] == "events" and row["source_kind"] == "rule"
    assert row["target"] == "lambda/verticalRefreshNotification"
    assert row["schedule"] == "rate(5 minutes)"
    assert row["status"] == "resolved"
    assert row["file"].endswith("verticalRefreshNotification/prod/terragrunt.hcl")


def test_the_unit_records_only_names_and_schedules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        stored = json.dumps([
            r[0] for r in store._conn.execute(
                "SELECT extra FROM nodes WHERE kind = 'File' AND language = 'hcl'",
            ).fetchall()
        ])
        rows = json.dumps(trigger_links.read_triggers(store), default=str)
    finally:
        store.close()
    for text in (stored, rows):
        for forbidden in (
            "hooks.slack.com", "tok_live_do_not_store", "api_token", "Run vertical refresh",
            "teamcity", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        ):
            assert forbidden not in text, forbidden


def test_ignore_changes_rules_schedules_patterns_and_a_secret_looking_name(
    tmp_path: Path,
) -> None:
    rows = {r["source"]: r for r in _rows(tmp_path) if r["target"] == "lambda/checkHealth"}
    assert set(rows) == {"check-health-dev2", "orders-created"}  # the token-named rule is gone
    assert rows["check-health-dev2"]["disabled"] is True
    assert rows["check-health-dev2"]["schedule"] == "cron(30 13 ? * * *)"
    # An escaped string is not evaluated (see TerragruntEvaluator._literal_text): no sources.
    assert "event_pattern_sources" not in rows["orders-created"]


def test_event_pattern_sources_are_plain_names_only() -> None:
    from code_review_graph.infra_references import reduce_event_rule

    rule = reduce_event_rule({
        "name": "orders-created", "event_pattern": '{"source": ["app.orders", "bad source!", 3]}',
        "schedule_expression": "rate(1 minute)", "is_enabled": "false",
    })
    assert rule == {
        "name": "orders-created", "schedule_expression": "rate(1 minute)",
        "event_pattern_sources": ["app.orders"], "enabled": False,
    }
    assert reduce_event_rule({"name": "ok", "schedule_expression": "do anything"}) \
        == {"name": "ok"}
    assert reduce_event_rule({"name": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}) is None
    assert reduce_event_rule({"name": "has space"}) is None


def test_sns_topics_and_sqs_queue_are_linked_as_the_module_wires_them(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    direct = {(r["source"], r["target"], r["protocol"]) for r in rows
              if r["trigger_kind"] == "sns_subscription" and r["target"].startswith("lambda/data")}
    assert direct == {
        ("pipeline-fail-prod", "lambda/datapipelineMetrics", "lambda"),
        ("pipeline-late-prod", "lambda/datapipelineMetrics", "lambda"),
    }
    queued = {(r["trigger_kind"], r["source"], r["target"]) for r in rows
              if r["target"] in ("lambda/drain", "sqs/ec2GradualDrainingSqs")}
    assert queued == {
        ("sns_subscription", "scale-in", "sqs/ec2GradualDrainingSqs"),
        ("event_source_mapping", "ec2GradualDrainingSqs", "lambda/drain"),
    }


def test_a_unit_without_triggers_records_nothing(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    store = GraphStore(tmp_path.resolve() / "graph.db")
    try:
        info = store._conn.execute(
            "SELECT json_extract(extra, '$.terragrunt_inputs') FROM nodes "
            "WHERE kind = 'File' AND file_path LIKE '%/plain/%'",
        ).fetchone()[0]
    finally:
        store.close()
    assert "event_rules" not in json.loads(info) and "sns_topics" not in json.loads(info)
    assert not any(r["target"] == "lambda/plain" for r in rows)


def test_module_rows_are_labelled_templates(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    write_files(root, {
        "modules/backup.tf": (
            'resource "aws_lambda_function" "backup" {\n'
            "  function_name = local.backup_function_name\n}\n"
            'resource "aws_cloudwatch_event_rule" "backup" {\n'
            "  name                = local.backup_function_name\n"
            '  schedule_expression = "rate(1 day)"\n}\n'
            'resource "aws_cloudwatch_event_target" "backup" {\n'
            "  rule = aws_cloudwatch_event_rule.backup.name\n"
            "  arn  = aws_lambda_function.backup.arn\n}\n"
        ),
    })
    store = GraphStore(root / "graph.db")
    try:
        path = root / "modules/backup.tf"
        nodes, edges = CodeParser(root).parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges)
        found = trigger_links.read_triggers(store)
    finally:
        store.close()
    assert found and all(r["via"] == "terraform" for r in found)
    for row in found:
        if row["status"] == "source_unresolved":
            assert row["template"] is True
        else:
            assert "template" not in row
