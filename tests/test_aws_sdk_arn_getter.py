"""A Step Functions call whose ARN comes from a getter in another file (``aws_sdk_calls``).

``StartExecution({stateMachineArn: await verticalRefreshArn()})`` names no machine in its own
file. The getter does: ``const verticalRefreshArn = () => sfn.stateMachineArn('vertical-refresh')``.
The extractor records the helper's reference where the name is written out and tags the call with
the getter it used; the link step names the call, and says it did, only when the repo's getters
of that name all give the same machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_review_graph.aws_sdk_links import _resolve_arn_getters, read_edges
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser

READER = """\
import { paginateListExecutions } from '@aws-sdk/client-sfn';
import { sfn } from './aws';

export const MACHINE = 'vertical-refresh';

export const verticalRefreshArn = () => sfn.stateMachineArn(MACHINE);
"""

OPERATIONS = """\
import { StartExecutionCommand } from '@aws-sdk/client-sfn';
import { verticalRefreshArn } from './reader';
import { client } from './aws';

export async function startRefresh(input: string) {
  return client.send(
    new StartExecutionCommand({ stateMachineArn: await verticalRefreshArn(), input }),
  );
}
"""


def _refs(name: str, source: str) -> list[Any]:
    _, edges = CodeParser().parse_bytes(Path("/work/app") / name, source.encode())
    return [e for e in edges if e.kind == "REFERENCES" and e.target.startswith("external:aws-")]


def test_a_state_machine_arn_helper_with_a_written_out_name_is_a_reference() -> None:
    (edge,) = _refs("src/reader.ts", READER)
    assert edge.target == "external:aws-states:vertical-refresh"
    assert edge.source.endswith("::verticalRefreshArn")
    assert edge.extra["operation"] == "state_machine_arn"
    assert edge.extra["sdk"] == "arn-helper"
    assert "sdk_operation" not in edge.extra  # no SDK operation is invented for a helper
    assert edge.extra["access"] == "unknown"


def test_a_helper_whose_name_is_not_written_out_is_not_a_reference() -> None:
    source = (
        "import { paginateListExecutions } from '@aws-sdk/client-sfn';\n"
        "export const arnOf = (name: string) => sfn.stateMachineArn(name);\n"
    )
    assert _refs("src/arn.ts", source) == []


def test_a_call_whose_arn_is_a_getter_names_the_getter() -> None:
    (edge,) = _refs("src/operations.ts", OPERATIONS)
    assert edge.target == "external:aws-states:?"
    assert edge.extra["unresolved"] is True
    assert edge.extra["arn_getter"] == "verticalRefreshArn"
    assert edge.extra["operation"] == "start_execution"


def _row(source: str, name: str, sdk: str, unresolved: bool, **extra: Any) -> dict[str, Any]:
    return {
        "source": source, "name": name, "sdk": sdk, "unresolved": unresolved, "extra": dict(extra),
    }


def test_a_call_is_named_when_every_getter_of_that_name_agrees() -> None:
    rows = [
        _row("a/reader.ts::verticalRefreshArn", "vertical-refresh", "arn-helper", False),
        _row("a/ops.ts::start", "?", "aws-sdk-js-v3", True, arn_getter="verticalRefreshArn"),
    ]
    _resolve_arn_getters(rows)
    assert rows[1]["name"] == "vertical-refresh" and rows[1]["unresolved"] is False
    assert rows[1]["extra"]["resolved_via"] == "arn_getter:verticalRefreshArn"


def test_a_call_stays_unresolved_when_two_getters_disagree() -> None:
    rows = [
        _row("a/one.ts::arn", "machine-one", "arn-helper", False),
        _row("a/two.ts::arn", "machine-two", "arn-helper", False),
        _row("a/ops.ts::start", "?", "aws-sdk-js-v3", True, arn_getter="arn"),
    ]
    _resolve_arn_getters(rows)
    assert rows[2]["unresolved"] is True and rows[2]["name"] == "?"
    assert "resolved_via" not in rows[2]["extra"]


def test_a_call_stays_unresolved_when_no_getter_names_a_machine() -> None:
    rows = [_row("a/ops.ts::start", "?", "aws-sdk-js-v3", True, arn_getter="missing")]
    _resolve_arn_getters(rows)
    assert rows[0]["unresolved"] is True


def test_a_built_graph_joins_the_start_call_to_the_machine_the_getter_names(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "reader.ts").write_text(READER, encoding="utf-8")
    (src / "operations.ts").write_text(OPERATIONS, encoding="utf-8")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        rows = read_edges(store)
    start = next(r for r in rows if r["operation"] == "start_execution")
    assert start["name"] == "vertical-refresh"
    assert start["unresolved"] is False
    assert start["source"].endswith("::startRefresh")
    assert start["extra"]["resolved_via"] == "arn_getter:verticalRefreshArn"
    helper = next(r for r in rows if r["operation"] == "state_machine_arn")
    assert helper["name"] == "vertical-refresh" and helper["sdk"] == "arn-helper"
