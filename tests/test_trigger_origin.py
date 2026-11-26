"""``origin="trigger"``: what invokes a Lambda, queue, state machine, topic or bucket handler."""

from __future__ import annotations

import pytest

from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import registry as registry  # noqa: F401
from .test_service_aliases_infra_join import make_repo

MAIN = '''
resource "aws_sqs_queue" "jobs" {
  name = "jobs-queue"
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
resource "aws_sns_topic_subscription" "to_worker" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.worker.arn
}
resource "aws_sns_topic_subscription" "to_hook" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "https"
  endpoint  = "https://example.com/hook"
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
  event_pattern = jsonencode({ source = ["aws.s3"] })
}
resource "aws_cloudwatch_event_target" "uploads" {
  rule = aws_cloudwatch_event_rule.uploads.name
  arn  = aws_sfn_state_machine.ingest.arn
}
resource "aws_s3_bucket" "in" {
  bucket = "inbox-bucket"
}
resource "aws_s3_bucket_notification" "in" {
  bucket = aws_s3_bucket.in.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.worker.arn
  }
}
resource "aws_lambda_permission" "from_nightly" {
  function_name = aws_lambda_function.nightly.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nightly.arn
}
resource "aws_apigatewayv2_integration" "worker" {
  integration_uri = aws_lambda_function.worker.invoke_arn
}
resource "aws_apigatewayv2_route" "post_job" {
  route_key = "POST /v1/jobs"
  target    = "integrations/${aws_apigatewayv2_integration.worker.id}"
}
resource "aws_api_gateway_integration" "legacy" {
  rest_api_id = aws_api_gateway_rest_api.public.id
  uri         = aws_lambda_function.nightly.invoke_arn
}
resource "aws_pipes_pipe" "jobs" {
  source = aws_sqs_queue.jobs.arn
  target = aws_sfn_state_machine.ingest.arn
  role_arn = aws_iam_role.pipe.arn
}
resource "aws_cloudwatch_event_target" "dyn" {
  rule = aws_cloudwatch_event_rule.elsewhere.name
  arn  = aws_lambda_function.worker.arn
}
'''


@pytest.fixture
def trig(tmp_path, registry):  # noqa: F811
    make_repo(tmp_path, registry, "infra", {"stack/main.tf": MAIN})
    make_repo(tmp_path, registry, "plain", {"a.tf": 'resource "random_id" "x" {}\n'})
    return tmp_path


def rows(**query):
    reply = cross_repo_requests_func(origin="trigger", **query)
    assert reply["status"] == "ok" and reply["origin"] == "trigger"
    return reply["results"], reply


def test_what_triggers_a_lambda(trig):
    found, reply = rows(service="jobs-worker")
    by_kind = {(r["trigger_kind"], r["source"]): r for r in found}
    assert ("event_source_mapping", "jobs-queue") in by_kind
    assert ("sns_subscription", "alerts-topic") in by_kind
    assert ("s3_notification", "inbox-bucket") in by_kind
    assert ("api_gateway", "POST /v1/jobs") in by_kind
    assert by_kind[("api_gateway", "POST /v1/jobs")]["route"] == "POST /v1/jobs"
    mapping = by_kind[("event_source_mapping", "jobs-queue")]
    assert mapping["status"] == "resolved" and mapping["repo"] == "infra"
    assert mapping["file"] == "stack/main.tf" and mapping["line"] > 0
    assert mapping["target"] == "lambda/jobs-worker"
    assert reply["trigger_kinds"]["event_source_mapping"] >= 1


def test_schedule_and_event_pattern_are_literals_only(trig):
    found, _ = rows(service="nightly-report")
    rule = next(r for r in found if r["trigger_kind"] == "eventbridge_rule")
    assert rule["source"] == "nightly-rule" and rule["schedule"] == "cron(0 6 * * ? *)"
    perm = next(r for r in found if r["trigger_kind"] == "lambda_permission")
    assert perm["source"] == "nightly-rule" and perm["principal"] == "events.amazonaws.com"
    assert perm["via"] == "reference"


def test_what_does_a_source_fire(trig):
    found, _ = rows(route="alerts-topic")
    targets = {r["target"] for r in found}
    assert targets == {"lambda/jobs-worker", "https/https"}
    hook = next(r for r in found if r["target_service"] == "https")
    assert hook["protocol"] == "https" and "example.com" not in repr(hook)


def test_unresolved_shapes_are_labelled_not_guessed(trig):
    found, reply = rows(status="source_unresolved")
    sources = {r["source"] for r in found}
    assert "aws_cloudwatch_event_rule.elsewhere" in sources or "elsewhere" in str(sources)
    assert "aws_api_gateway_rest_api.public" in sources
    assert reply["counts"]["source_unresolved"] == len(found)
    sfn = [r for r in rows(route="uploads-rule")[0]]
    assert sfn and sfn[0]["target_name"] == "aws_sfn_state_machine.ingest"
    assert sfn[0]["status"] == "target_unresolved"
    assert sfn[0]["event_pattern_sources"] == ["aws.s3"]


def test_pipe_with_one_source_and_filter_by_both(trig):
    found, _ = rows(service="aws_sfn_state_machine.ingest", route="jobs-queue")
    assert [r["trigger_kind"] for r in found] == ["eventbridge_pipe"]
    assert found[0]["via"] == "reference"


def test_paging_and_validation(trig):
    everything, reply = rows(max_results=100)
    assert reply["total"] == len(everything) >= 8
    seen: list = []
    cursor = None
    while True:
        page = cross_repo_requests_func(origin="trigger", max_results=3, cursor=cursor)
        seen.extend(page["results"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    assert seen == everything
    with pytest.raises(ValueError):
        cross_repo_requests_func(origin="trigger", status="matched")


def test_repos_filter_and_empty_repo(trig):
    found, reply = rows(repos=["plain"])
    assert found == [] and reply["total"] == 0
