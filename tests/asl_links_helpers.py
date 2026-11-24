"""A small synthetic repository shaped like the Terragrunt layout the Step Functions links read.

``terraform/step_function.hcl`` and ``terraform/lambda.hcl`` are the parents every unit
includes, as in the repositories the feature was measured on: the Lambda parent works the
source directory and the function name out of the unit's path with ``split``, ``slice``,
``join`` and ``length``.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build

MODULE_SOURCE = "git::ssh://git@github.com/acme/infra.git//modules/resources"

STEP_FUNCTION_HCL = f'''terraform {{
  source = "{MODULE_SOURCE}/step_function"
}}

locals {{
  paths    = split("/", path_relative_to_include())
  src_path = join("/", slice(local.paths, 0, length(local.paths) - 1))
}}

inputs = {{
  full_path = get_repo_root()
  team      = "PLATFORM"
}}
'''

LAMBDA_HCL = f'''terraform {{
  source = "{MODULE_SOURCE}/lambda"
}}

locals {{
  paths    = split("/", path_relative_to_include())
  src_path = join("/", slice(local.paths, 0, length(local.paths) - 1))
}}

inputs = {{
  git_repo_sub_dir = "${{get_repo_root()}}/${{local.src_path}}"
  function_name    = split("/", local.src_path)[length(split("/", local.src_path)) - 1]
  team             = "PLATFORM"
}}
'''

MACHINE = '''{
  "StartAt": "Validate",
  "States": {
    "Validate": {
      "Type": "Task",
      "Resource": "${ValidateFunctionArn}",
      "Next": "Notify"
    },
    "Notify": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sns:publish",
      "Parameters": {"TopicArn": "${TopicArn}", "Message": "done"},
      "Next": "Child"
    },
    "Child": {
      "Type": "Task",
      "Resource": "arn:aws:states:::states:startExecution.sync:2",
      "Parameters": {"StateMachineArn": "${ChildMachineArn}"},
      "Next": "Typed"
    },
    "Typed": {
      "Type": "Task",
      "Resource": "${AuditFunctionArn}",
      "Next": "Unfilled"
    },
    "Unfilled": {
      "Type": "Task",
      "Resource": "${NobodyFillsThis}",
      "End": true
    }
  }
}
'''

MACHINE_BASE = '''inputs = {
  name                    = "orders"
  step_function_file_path = "step-functions/orders/statemachine/orders.asl.json"
}
'''


def machine_unit(account: str, topic: str, extra: str = "") -> str:
    """An environment unit of the ``orders`` machine: it fills the placeholders."""
    return f'''include {{
  path = find_in_parent_folders("step_function.hcl")
}}

include "base" {{
  path = find_in_parent_folders()
}}

inputs = {{
  replacement_vars = {{
    ValidateFunctionArn = "arn:aws:lambda:us-east-1:{account}:function:validateOrder"
    TopicArn            = "arn:aws:sns:us-east-1:{account}:{topic}"
    ChildMachineArn     = "arn:aws:states:us-east-1:{account}:stateMachine:child-machine"
    SubnetA             = "subnet-0abc1234"
    Owner               = "team@example.com"
    Database            = "postgres://admin:hunter2@db.example.com/orders"
    ApiSecret           = "arn:aws:lambda:us-east-1:{account}:function:looksLikeASecret"
  }}
  lambda_replacement_vars = {{
    "AuditFunctionArn" = "audit-trail"
  }}
  password = "correct-horse-battery-staple"
{extra}}}
'''


def lambda_unit(handler: str, extra: str = "") -> str:
    return f'''include {{
  path = find_in_parent_folders("lambda.hcl")
}}

inputs = {{
  description = "A function"
  handler     = "{handler}"
  runtime     = "python3.13"
  api_token   = "tok_live_do_not_store"
{extra}}}
'''


HANDLER = '''import json


def helper(event):
    return json.dumps(event)


def handler(event, context):
    return helper(event)
'''

FILES = {
    "terraform/step_function.hcl": STEP_FUNCTION_HCL,
    "terraform/lambda.hcl": LAMBDA_HCL,
    "terraform/step-functions/orders/terragrunt.hcl": MACHINE_BASE,
    "terraform/step-functions/orders/prod/terragrunt.hcl": machine_unit("111111111111", "alerts"),
    "terraform/step-functions/orders/nonprod/terragrunt.hcl": machine_unit(
        "222222222222", "alerts-dev",
    ),
    "step-functions/orders/statemachine/orders.asl.json": MACHINE,
    "terraform/python-lambdas/validateOrder/prod/terragrunt.hcl": lambda_unit(
        "validate_order.handler",
    ),
    "terraform/python-lambdas/validateOrder/nonprod/terragrunt.hcl": lambda_unit(
        "validate_order.handler",
    ),
    "python-lambdas/validateOrder/validate_order.py": HANDLER,
}

MACHINE_PATH = "step-functions/orders/statemachine/orders.asl.json"


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def build_repo(root: Path, files: dict[str, str] | None = None) -> GraphStore:
    """Write *files* (default :data:`FILES`) under *root* and run a full build."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    write_files(root, FILES if files is None else files)
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    full_build(root, store)
    return store


def derived_edges(store: GraphStore, root: Path) -> list[tuple[str, str, str, str, float]]:
    """``(source, target, resolution, tier, confidence)`` of every derived ASL edge.

    The tier and the confidence are the ones the resolver recorded in ``extra``. The edge's
    columns follow the graph-wide rule instead: an edge to an ``external:`` target, which
    names no node, is ``UNRESOLVED`` there whoever made it.
    """
    prefix = root.resolve().as_posix() + "/"
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, json_extract(extra, '$.resolution') AS r, "
        "json_extract(extra, '$.confidence_tier'), json_extract(extra, '$.confidence') "
        "FROM edges WHERE kind = 'REFERENCES' "
        "AND json_extract(extra, '$.resolution') LIKE 'asl_%'"
    ).fetchall()
    return sorted(
        (row[0].replace(prefix, ""), row[1].replace(prefix, ""), row[2], row[3], row[4])
        for row in rows
    )


def state(name: str) -> str:
    return f"{MACHINE_PATH}::orders/{name}"
