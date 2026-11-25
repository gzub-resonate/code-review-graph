"""Terraform wiring: declarations, triggers and the join that names cross-file references."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_review_graph import aws_sdk_links as links
from code_review_graph.aws_sdk_links import read_edges
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

ROOT = Path("/work/infra")

WIRING = '''
resource "aws_sqs_queue" "jobs_dlq" {
  name = "jobs-dlq"
}
resource "aws_sqs_queue" "jobs" {
  name = "jobs-queue"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 5
  })
}
resource "aws_lambda_function" "worker" {
  function_name = "jobs-worker"
}
resource "aws_lambda_function" "nightly" {
  function_name = "nightly-report"
}
resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn
}
resource "aws_sns_topic" "alerts" {
  name = "alerts-topic"
}
resource "aws_sns_topic_subscription" "alerts_worker" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.worker.arn
}
resource "aws_sns_topic_subscription" "alerts_mail" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = "someone@example.com"
}
resource "aws_cloudwatch_event_rule" "nightly" {
  name                = "nightly-rule"
  schedule_expression = "cron(0 6 * * ? *)"
}
resource "aws_cloudwatch_event_target" "nightly" {
  rule = aws_cloudwatch_event_rule.nightly.name
  arn  = aws_lambda_function.nightly.arn
}
resource "aws_cloudwatch_event_rule" "uploads" {
  name          = "uploads-rule"
  event_pattern = jsonencode({ source = ["aws.s3"], "detail-type" = ["Object Created"] })
}
resource "aws_scheduler_schedule" "hourly" {
  name                = "hourly-sync"
  schedule_expression = "rate(1 hour)"
  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = "arn:aws:iam::123456789012:role/scheduler"
  }
}
resource "aws_dynamodb_table" "orders" {
  name     = "orders-table"
  hash_key = "id"
  attribute {
    name = "id"
    type = "S"
  }
}
resource "aws_apigatewayv2_integration" "worker" {
  integration_uri = aws_lambda_function.worker.invoke_arn
}
resource "aws_apigatewayv2_route" "post_job" {
  route_key = "POST /v1/jobs"
  target    = "integrations/${aws_apigatewayv2_integration.worker.id}"
}
'''


def tf_edges(name: str, source: str) -> list[Any]:
    _nodes, edges = CodeParser().parse_bytes(ROOT / name, source.encode())
    return [e for e in edges if e.kind == "REFERENCES" and e.target.startswith("external:aws-")]


def by_key(edges: list[Any]) -> dict[tuple[str, str], Any]:
    return {(e.target, e.extra["operation"]): e for e in edges}


def test_declarations_use_the_written_name_not_the_label_or_nested_attributes() -> None:
    found = by_key(tf_edges("main.tf", WIRING))
    for target in (
        "external:aws-sqs:jobs-queue", "external:aws-sqs:jobs-dlq",
        "external:aws-lambda:jobs-worker", "external:aws-lambda:nightly-report",
        "external:aws-sns:alerts-topic", "external:aws-events:nightly-rule",
        "external:aws-scheduler:hourly-sync", "external:aws-dynamodb:orders-table",
    ):
        assert (target, "declare") in found, target
    # The table's nested ``attribute { name = "id" }`` is not the table name.
    assert ("external:aws-dynamodb:id", "declare") not in found
    queue = found[("external:aws-sqs:jobs-queue", "declare")]
    assert queue.extra["dead_letter_target"] == "sqs/jobs-dlq"
    assert queue.extra["terraform_label"] == "jobs"
    assert queue.extra["terraform_type"] == "aws_sqs_queue"
    assert queue.source.endswith("::resource.aws_sqs_queue.jobs")


def test_schedule_and_event_pattern_are_kept_and_targets_are_listed() -> None:
    found = by_key(tf_edges("main.tf", WIRING))
    rule = found[("external:aws-events:nightly-rule", "declare")]
    assert rule.extra["schedule"] == "cron(0 6 * * ? *)"
    assert found[("external:aws-events:uploads-rule", "declare")].extra["event_sources"] == [
        "aws.s3"
    ]
    fired = found[("external:aws-events:nightly-rule", "event_target")]
    assert fired.extra["notify_targets"] == ["lambda/nightly-report"]
    sched = found[("external:aws-scheduler:hourly-sync", "declare")]
    assert sched.extra["schedule"] == "rate(1 hour)"
    assert sched.extra["notify_targets"] == ["lambda/jobs-worker"]


def test_event_source_mapping_subscription_and_route() -> None:
    found = by_key(tf_edges("main.tf", WIRING))
    mapping = found[("external:aws-sqs:jobs-queue", "event_source")]
    assert mapping.extra["notify_targets"] == ["lambda/jobs-worker"]
    assert mapping.extra["access"] == "notify"
    subs = [e for e in tf_edges("main.tf", WIRING) if e.extra["operation"] == "subscribe"]
    by_protocol = {e.extra["protocol"]: e for e in subs}
    assert by_protocol["lambda"].extra["notify_targets"] == ["lambda/jobs-worker"]
    # An e-mail subscription fires nothing the graph can name: the protocol is kept, the
    # address (a value) is not.
    assert "notify_targets" not in by_protocol["email"].extra
    assert "someone@example.com" not in repr([e.extra for e in subs])
    route = found[("external:aws-lambda:jobs-worker", "route")]
    assert route.extra["route"] == "POST /v1/jobs"


def test_dynamic_names_are_not_declared_and_values_are_not_stored() -> None:
    src = '''
resource "aws_sqs_queue" "q" {
  name = "${var.env}-jobs"
}
resource "aws_cloudwatch_event_rule" "r" {
  name                = "r-rule"
  schedule_expression = var.schedule
}
'''
    found = by_key(tf_edges("dyn.tf", src))
    assert set(found) == {("external:aws-events:r-rule", "declare")}
    assert "schedule" not in found[("external:aws-events:r-rule", "declare")].extra


def test_non_aws_terraform_makes_no_edges() -> None:
    assert tf_edges("plain.tf", 'resource "random_id" "x" {\n  byte_length = 4\n}\n') == []


def _store(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    parser = CodeParser(repo)
    store = GraphStore(str(tmp_path / "graph.db"))
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return store


def test_references_to_a_resource_in_another_file_are_named_by_the_join(tmp_path: Path) -> None:
    files = {
        "infra/queues.tf": 'resource "aws_sqs_queue" "jobs" {\n  name = "jobs-queue"\n}\n'
        'resource "aws_lambda_function" "worker" {\n  function_name = "jobs-worker"\n}\n',
        "infra/wiring.tf": 'resource "aws_lambda_event_source_mapping" "m" {\n'
        "  event_source_arn = aws_sqs_queue.jobs.arn\n"
        "  function_name    = aws_lambda_function.worker.arn\n}\n",
        "other/wiring.tf": 'resource "aws_lambda_event_source_mapping" "m" {\n'
        "  event_source_arn = aws_sqs_queue.jobs.arn\n"
        "  function_name    = aws_lambda_function.worker.arn\n}\n",
    }
    store = _store(tmp_path, files)
    try:
        edges = read_edges(store)
    finally:
        store.close()
    fired = [e for e in edges if e["operation"] == "event_source"]
    by_folder = {e["file_path"].rsplit("/", 2)[-2]: e for e in fired}
    infra = by_folder["infra"]
    assert infra["name"] == "jobs-queue" and not infra["unresolved"]
    assert infra["extra"]["notify_targets"] == ["lambda/jobs-worker"]
    # A folder with no declarations cannot name either side: nothing is guessed.
    other = by_folder["other"]
    assert other["unresolved"] and other["extra"]["notify_targets"] == []


def test_join_reports_what_triggers_a_declared_function() -> None:
    def edge(**kw: Any) -> dict[str, Any]:
        base = {"file_path": "/b/main.tf", "line": 1, "source": "/b/main.tf::r",
                "kind": "", "access": "notify", "sdk": "terraform", "unresolved": False,
                "placeholder": None, "extra": {}}
        base.update(kw)
        return base

    tf = [
        edge(service="lambda", kind="function", name="nightly-report", operation="declare",
             access="declare", extra={"terraform_label": "n", "terraform_type": "x"}),
        edge(service="events", kind="rule", name="nightly-rule", operation="event_target",
             extra={"notify_targets": ["lambda/nightly-report"]}),
        edge(service="lambda", kind="function", name="jobs-worker", operation="route",
             extra={"route": "POST /v1/jobs", "wiring": "api_gateway"}),
    ]
    repos = [{"path": "/b", "alias": "infra"}]
    result = links.resolve_links({0: tf}, repos, lambda i, target: False)
    declared = next(r for r in result if r["operation"] == "declare")
    assert declared["status"] == "declared"
    assert declared["triggered_by"] == ["events/nightly-rule (event_target)"]
    route = next(r for r in result if r["operation"] == "route")
    assert route["route"] == "POST /v1/jobs" and route["status"] == "declared"
