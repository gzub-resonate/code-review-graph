"""A small repo that mirrors how project-disrupt starts its Step Functions machines.

``_sfn.get_client()`` is a lazy client factory, ``_launch.submit`` starts a machine named by its
``state_machine_arn`` parameter, ``_env`` holds the dev and prod ARN constants and a default
function, and the ``run_*`` modules are the callers.
"""

# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path

from code_review_graph.parser import CodeParser

DEV = "arn:aws:states:us-east-1:111111111111:stateMachine:disrupt-next-best-customer"
PROD = "arn:aws:states:us-east-1:222222222222:stateMachine:disrupt-next-best-customer"

SFN = '''\
from ._env import make_client

_client = None


def get_client():
    global _client
    if _client is None:
        _client = make_client('stepfunctions')
    return _client
'''

ENV = f'''\
import os

_DEV_NBC_STATE_MACHINE_ARN = '{DEV}'
_PROD_NBC_STATE_MACHINE_ARN = '{PROD}'
_PROD_ETL_STATE_MACHINE_ARN = 'arn:aws:states:us-east-1:222222222222:stateMachine:disrupt-etl-2'
_TOKEN_STATE_MACHINE_ARN = 'arn:aws:states:us-east-1:2:stateMachine:hidden'


def _is_dev():
    return os.environ.get('DEV') == '1'


def default_next_best_customer_state_machine_arn():
    return _DEV_NBC_STATE_MACHINE_ARN if _is_dev() else _PROD_NBC_STATE_MACHINE_ARN


def default_split_arn():
    return _DEV_NBC_STATE_MACHINE_ARN if _is_dev() else _PROD_ETL_STATE_MACHINE_ARN


def default_dynamic_arn():
    return os.environ['X']


def make_client(service_name):
    import boto3
    return boto3.client(service_name)
'''

LAUNCH = '''\
from . import _sfn


def submit(*, logger, state_machine_arn, run_name, sm_input):
    return _sfn.get_client().start_execution(
        stateMachineArn=state_machine_arn, name=run_name, input=sm_input,
    )


def describe(execution_arn):
    return _sfn.get_client().describe_execution(executionArn=execution_arn)
'''

CALLER = '''\
from . import _env, _launch


def run(input, logger):
    _launch.submit(
        logger=logger,
        state_machine_arn=input.state_machine_arn or _env.default_next_best_customer_state_machine_arn(),
        run_name='x', sm_input='{}',
    )


def run_literal(logger):
    _launch.submit(logger=logger, state_machine_arn='arn:aws:states:us-east-1:3:stateMachine:lit-sm', run_name='x', sm_input='{}')


def run_constant(logger):
    _launch.submit(logger=logger, state_machine_arn=_env._PROD_ETL_STATE_MACHINE_ARN, run_name='x', sm_input='{}')


def run_dynamic(input, logger):
    _launch.submit(logger=logger, state_machine_arn=input.arn, run_name='x', sm_input='{}')


def run_split(logger):
    _launch.submit(logger=logger, state_machine_arn=_env.default_split_arn(), run_name='x', sm_input='{}')


def run_dyn_default(logger):
    _launch.submit(logger=logger, state_machine_arn=_env.default_dynamic_arn(), run_name='x', sm_input='{}')


def run_secretish(logger):
    _launch.submit(logger=logger, state_machine_arn=_env._TOKEN_STATE_MACHINE_ARN, run_name='x', sm_input='{}')
'''

PKG = "mcp/tools/disrupt"
FILES = {
    f"{PKG}/__init__.py": "",
    f"{PKG}/_sfn.py": SFN,
    f"{PKG}/_env.py": ENV,
    f"{PKG}/_launch.py": LAUNCH,
    f"{PKG}/run_nbc.py": CALLER,
}


def write_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    for rel, text in (files or FILES).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def aws_rows(root: Path, rel: str) -> list[dict]:
    """The AWS SDK rows (extra of the ``aws-states`` edges) of one file, by line."""
    _nodes, edges = CodeParser(root).parse_file(root / rel)
    rows = []
    for edge in edges:
        if edge.target.startswith("external:aws-") and edge.extra.get("resolution") == "aws_sdk":
            rows.append({**edge.extra, "line": edge.line, "target": edge.target})
    return sorted(rows, key=lambda r: (r["line"], r["target"]))
