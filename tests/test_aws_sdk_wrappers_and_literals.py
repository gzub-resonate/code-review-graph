"""Client wrappers and literal tables in Python AWS SDK call sites (Release B, item B3).

Fixtures imitate real files of the ``resonate`` org:

* ``step-function-workflow-orchestrator/python_lambdas/cea-behavioral-pipeline/common/s3_io.py``
  (``def _client(): return boto3.client('s3')``, called as ``_client().head_object(...)``).
* ``step-function-workflow-orchestrator/python_lambdas/avmap-mapper/access_token.py`` and
  ``infrastructure-lambdas-step-functions/python-lambdas/runTeamCityJob/runTeamCityJob.py``
  (an SSM parameter name held in a module constant).
Everything runs offline on inline source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_review_graph.parser import CodeParser

ROOT = Path("/work/svc")


def aws_edges(source: str) -> list[Any]:
    _nodes, edges = CodeParser().parse_bytes(ROOT / "app.py", source.encode())
    return [e for e in edges if e.kind == "REFERENCES" and e.target.startswith("external:aws-")]


def targets(source: str) -> set[tuple[str, str]]:
    return {(e.target, e.extra["operation"]) for e in aws_edges(source)}


# -- 1. zero-argument client wrappers -----------------------------------------------------------


def test_wrapper_call_site_resolves() -> None:
    src = (
        "import boto3\n"
        "def _client():\n"
        "    return boto3.client('s3')\n"
        "def exists():\n"
        "    _client().head_object(Bucket='fixed-bkt', Key='k')\n"
        "def put():\n"
        "    c = _client()\n"
        "    c.put_object(Bucket='other-bkt', Key='k', Body=b'')\n"
    )
    assert targets(src) == {
        ("external:aws-s3:fixed-bkt", "head_object"),
        ("external:aws-s3:other-bkt", "put_object"),
    }


def test_wrapper_edges_are_extracted_not_inferred() -> None:
    src = (
        "import boto3\n"
        "def _client():\n"
        "    return boto3.client('s3')\n"
        "def f():\n"
        "    _client().get_object(Bucket='fixed-bkt', Key='k')\n"
    )
    (edge,) = aws_edges(src)
    assert edge.extra["confidence_tier"] == "EXTRACTED"
    assert "client_inferred" not in edge.extra


def test_wrapper_with_constant_argument_and_local_variable() -> None:
    src = (
        "import boto3\n"
        "def get_dynamo(region='us-east-1'):\n"
        "    resource = boto3.resource('dynamodb', region_name=region)\n"
        "    return resource\n"
        "def f():\n"
        "    get_dynamo('eu-west-1').Table('orders').put_item(Item={})\n"
    )
    assert targets(src) == {("external:aws-dynamodb:orders", "put_item")}


def test_wrapper_method_through_self() -> None:
    src = (
        "import boto3\n"
        "class Store:\n"
        "    def _s3(self):\n"
        "        return boto3.client('s3')\n"
        "    def load(self):\n"
        "        self._s3().get_object(Bucket='store-bkt', Key='k')\n"
    )
    assert targets(src) == {("external:aws-s3:store-bkt", "get_object")}


def test_wrapper_with_variable_argument_at_call_site_stays_unbound() -> None:
    src = (
        "import boto3\n"
        "def _client(region=None):\n"
        "    return boto3.client('s3')\n"
        "def f(region):\n"
        "    _client(region).get_object(Bucket='fixed-bkt', Key='k')\n"
    )
    assert targets(src) == set()


def test_wrapper_whose_service_is_a_parameter_stays_unbound() -> None:
    src = (
        "import boto3\n"
        "def _client(name):\n"
        "    return boto3.client(name)\n"
        "def f():\n"
        "    _client().get_object(Bucket='fixed-bkt', Key='k')\n"
    )
    assert targets(src) == set()


def test_wrapper_returning_two_services_stays_unbound() -> None:
    src = (
        "import boto3\n"
        "def _client(flag):\n"
        "    if flag:\n"
        "        return boto3.client('s3')\n"
        "    return boto3.client('dynamodb')\n"
        "def f():\n"
        "    _client().get_object(Bucket='fixed-bkt', Key='k')\n"
    )
    assert targets(src) == set()


def test_wrapper_defined_twice_stays_unbound() -> None:
    src = (
        "import boto3\n"
        "def _client():\n"
        "    return boto3.client('s3')\n"
        "class Other:\n"
        "    def _client(self):\n"
        "        return boto3.client('sqs')\n"
        "def f():\n"
        "    _client().get_object(Bucket='fixed-bkt', Key='k')\n"
    )
    assert targets(src) == set()


# -- 2. ARN and name constant tables ------------------------------------------------------------

_SM = "arn:aws:states:us-east-1:123456789012:stateMachine:etl-sm"


def test_arn_table_subscript_and_get_with_literal_key() -> None:
    src = (
        "import boto3\n"
        "sfn = boto3.client('stepfunctions')\n"
        f"MACHINES = {{'etl': '{_SM}'}}\n"
        "def run():\n"
        "    sfn.start_execution(stateMachineArn=MACHINES['etl'])\n"
        "    sfn.describe_state_machine(stateMachineArn=MACHINES.get('etl'))\n"
    )
    assert targets(src) == {
        ("external:aws-states:etl-sm", "start_execution"),
        ("external:aws-states:etl-sm", "describe_state_machine"),
    }


def test_arn_table_with_variable_key_stays_unresolved() -> None:
    src = (
        "import boto3\n"
        "sfn = boto3.client('stepfunctions')\n"
        f"MACHINES = {{'etl': '{_SM}'}}\n"
        "def run(name):\n"
        "    sfn.start_execution(stateMachineArn=MACHINES[name])\n"
    )
    assert targets(src) == {("external:aws-states:?", "start_execution")}


def test_arn_table_with_missing_key_or_written_table_stays_unresolved() -> None:
    src = (
        "import boto3\n"
        "sfn = boto3.client('stepfunctions')\n"
        f"MACHINES = {{'etl': '{_SM}'}}\n"
        "MACHINES['late'] = 'arn:aws:states:us-east-1:1:stateMachine:late-sm'\n"
        "def run():\n"
        "    sfn.start_execution(stateMachineArn=MACHINES['etl'])\n"
    )
    assert targets(src) == {("external:aws-states:?", "start_execution")}


def test_arn_table_with_non_literal_value_skips_only_that_key() -> None:
    src = (
        "import boto3\n"
        "sfn = boto3.client('stepfunctions')\n"
        f"MACHINES = {{'etl': '{_SM}', 'dyn': build()}}\n"
        "def run():\n"
        "    sfn.start_execution(stateMachineArn=MACHINES['etl'])\n"
        "    sfn.start_execution(stateMachineArn=MACHINES['dyn'])\n"
    )
    assert targets(src) == {
        ("external:aws-states:etl-sm", "start_execution"),
        ("external:aws-states:?", "start_execution"),
    }


# -- 3. role and parameter names defined elsewhere in the module --------------------------------


def test_class_attribute_constants_resolve_through_self_and_class() -> None:
    src = (
        "import boto3\n"
        "sts = boto3.client('sts')\n"
        "ssm = boto3.client('ssm')\n"
        "class Auth:\n"
        "    ROLE = 'arn:aws:iam::123456789012:role/x-role'\n"
        "    param = '/svc/token'\n"
        "    def go(self):\n"
        "        sts.assume_role(RoleArn=self.ROLE, RoleSessionName='s')\n"
        "        ssm.get_parameter(Name=Auth.param)\n"
    )
    assert targets(src) == {
        ("external:aws-sts:x-role", "assume_role"),
        ("external:aws-ssm:/svc/token", "get_parameter"),
    }


def test_class_attribute_assigned_twice_stays_unresolved() -> None:
    src = (
        "import boto3\n"
        "ssm = boto3.client('ssm')\n"
        "class A:\n"
        "    param = '/one'\n"
        "class B:\n"
        "    param = '/two'\n"
        "class C:\n"
        "    def go(self):\n"
        "        ssm.get_parameter(Name=self.param)\n"
    )
    assert targets(src) == {("external:aws-ssm:?", "get_parameter")}


def test_class_attribute_same_name_in_two_classes_resolves_per_class() -> None:
    src = (
        "import boto3\n"
        "ssm = boto3.client('ssm')\n"
        "class A:\n"
        "    param = '/one'\n"
        "    def go(self):\n"
        "        ssm.get_parameter(Name=self.param)\n"
        "class B:\n"
        "    param = '/two'\n"
        "    def go(self):\n"
        "        ssm.get_parameter(Name=self.param)\n"
    )
    assert targets(src) == {
        ("external:aws-ssm:/one", "get_parameter"),
        ("external:aws-ssm:/two", "get_parameter"),
    }


def test_module_constant_ssm_name_still_resolves() -> None:
    # runTeamCityJob.py: SSM_TEAMCITY_TOKEN = 'TEAMCITY_TOKEN'
    src = (
        "import boto3\n"
        "ssm = boto3.client('ssm')\n"
        "SSM_TEAMCITY_TOKEN = 'TEAMCITY_TOKEN'\n"
        "def tok():\n"
        "    return ssm.get_parameter(Name=SSM_TEAMCITY_TOKEN, WithDecryption=True)\n"
    )
    assert targets(src) == {("external:aws-ssm:TEAMCITY_TOKEN", "get_parameter")}


def test_function_parameter_name_stays_unresolved() -> None:
    src = (
        "import boto3\n"
        "ssm = boto3.client('ssm')\n"
        "def read(ssm_key):\n"
        "    return ssm.get_parameter(Name=ssm_key)\n"
    )
    assert targets(src) == {("external:aws-ssm:?", "get_parameter")}
