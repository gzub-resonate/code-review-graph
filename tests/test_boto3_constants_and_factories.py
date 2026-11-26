"""Resource-like module constants and ``make_client("svc")`` factories (Python, boto3).

A constant that names a bucket, queue, state machine or ARN is stored as a ``Variable`` node with
its (masked) value; an unresolved AWS call that names such a constant is resolved from it at
build time (``resolved_names``), and a client made by a factory called with a literal service is
bound at low confidence. Everything is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_review_graph import aws_sdk_links as links
from code_review_graph.aws_config_names import resolve_config_names
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

CONSTS = """\
import os
SFN_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:disrupt-segmentation"
RESULTS_BUCKET = "guardians-results-dev"
INPUT_URI = "s3://guardians-input/app/x"
WORK_QUEUE = "work-queue"
API_TOKEN_BUCKET = "bucket-with-token-name"
SECRET_BUCKET_VALUE = "AKIAIOSFODNN7EXAMPLE"
RETRY_COUNT = "3"
GREETING = "hello world"
DUP_BUCKET = "one"
DUP_BUCKET = "two"
LONG_BUCKET_ARN = "arn:aws:s3:::x"
"""

LAUNCH = """\
import boto3
import consts
from consts import RESULTS_BUCKET, SFN_STATE_MACHINE_ARN, DUP_BUCKET

s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")

def launch():
    sfn.start_execution(stateMachineArn=SFN_STATE_MACHINE_ARN)
    s3.put_object(Bucket=RESULTS_BUCKET, Key="k")
    s3.get_object(Bucket=consts.RESULTS_BUCKET, Key="k")
    s3.get_object(Bucket=DUP_BUCKET, Key="k")
    s3.get_object(Bucket=MISSING_BUCKET, Key="k")
"""


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


def _constants(store: GraphStore) -> dict[str, dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT name, extra FROM nodes WHERE kind = 'Variable'",
    ).fetchall()
    return {n: json.loads(e) for n, e in rows if "resource_value" in (e or "")}


def _aws(store: GraphStore) -> list[tuple[str, dict[str, Any]]]:
    rows = store._conn.execute(
        "SELECT target_qualified, extra FROM edges WHERE target_qualified LIKE 'external:aws-%'",
    ).fetchall()
    return [(t, json.loads(e)) for t, e in rows]


def test_resource_like_constants_are_stored_masked_and_others_are_not(tmp_path: Path) -> None:
    store = _store(tmp_path, {"app/consts.py": CONSTS})
    got = {n: e["resource_value"] for n, e in _constants(store).items()}
    assert got == {
        "SFN_STATE_MACHINE_ARN":
            "arn:aws:states:us-east-1:ACCOUNT:stateMachine:disrupt-segmentation",
        "RESULTS_BUCKET": "guardians-results-dev",
        "INPUT_URI": "s3://guardians-input/app/x",
        "WORK_QUEUE": "work-queue",
        "LONG_BUCKET_ARN": "arn:aws:s3:::x",
    }
    for dropped in ("API_TOKEN_BUCKET", "SECRET_BUCKET_VALUE", "RETRY_COUNT", "GREETING",
                    "DUP_BUCKET"):
        assert dropped not in got
    assert "123456789012" not in json.dumps(_constants(store))
    store.close()


def test_constant_in_another_module_resolves_the_unresolved_call(tmp_path: Path) -> None:
    store = _store(tmp_path, {"app/consts.py": CONSTS, "app/launch.py": LAUNCH})
    before = {(t, e["operation"]): e for t, e in _aws(store)}
    assert before[("external:aws-states:?", "start_execution")]["const_ref"] \
        == "SFN_STATE_MACHINE_ARN"
    stats = resolve_config_names(store._conn)
    assert stats["resolved"] >= 3
    after = [(t, e) for t, e in _aws(store) if e["operation"] in ("start_execution", "get_object",
                                                                    "put_object")]
    by_ref = {}
    for _t, e in after:
        by_ref.setdefault(e.get("const_ref"), []).append(e)
    assert by_ref["SFN_STATE_MACHINE_ARN"][0]["resolved_names"] == {
        "default": "disrupt-segmentation",
    }
    assert all(e["resolved_names"] == {"default": "guardians-results-dev"}
               for e in by_ref["RESULTS_BUCKET"])
    assert len(by_ref["RESULTS_BUCKET"]) == 2  # bare name and ``consts.NAME``
    # Two values for one name, or no such constant: no guess.
    assert "resolved_names" not in by_ref["DUP_BUCKET"][0]
    assert "resolved_names" not in by_ref["MISSING_BUCKET"][0]
    store.close()


def test_resolution_is_idempotent_and_drops_a_removed_constant(tmp_path: Path) -> None:
    store = _store(tmp_path, {"app/consts.py": CONSTS, "app/launch.py": LAUNCH})
    resolve_config_names(store._conn)
    first = sorted(json.dumps(e, sort_keys=True) for _t, e in _aws(store))
    resolve_config_names(store._conn)
    assert sorted(json.dumps(e, sort_keys=True) for _t, e in _aws(store)) == first
    store._conn.execute("DELETE FROM nodes WHERE kind = 'Variable'")
    resolve_config_names(store._conn)
    assert not any("resolved_names" in e for _t, e in _aws(store))
    store.close()


def test_const_ref_is_passed_through_to_the_awssdk_row() -> None:
    assert "const_ref" in links._ROW_EXTRA and "client_inferred" in links._ROW_EXTRA


def test_existing_resolved_rows_are_unchanged() -> None:
    src = (
        "import boto3\n"
        "BUCKET = 'same-file'\n"
        "s3 = boto3.client('s3')\n"
        "def f(b):\n"
        "    s3.put_object(Bucket=BUCKET, Key='k')\n"
        "    s3.get_object(Bucket=b, Key='k')\n"
    )
    _nodes, edges = CodeParser().parse_bytes(Path("/work/svc/app.py"), src.encode())
    rows = {e.target: e.extra for e in edges if e.target.startswith("external:aws-")}
    assert rows["external:aws-s3:same-file"]["confidence"] == 1.0
    assert "const_ref" not in rows["external:aws-s3:same-file"]
    assert "const_ref" not in rows["external:aws-s3:?"]  # lower case: not a constant


def test_factory_call_with_a_literal_service_binds_a_low_confidence_client() -> None:
    src = (
        "import _env\n"
        "from helpers import make_client\n"
        "sfn = _env.make_client('stepfunctions')\n"
        "def f(arn, name):\n"
        "    sfn.start_execution(stateMachineArn='arn:aws:states:us-east-1:1:stateMachine:sm')\n"
        "    make_client('s3').get_object(Bucket='bkt', Key=name)\n"
        "    _env.get_client('ssm').get_parameter(Name='/a/b')\n"
        "    _env.make_client(name).get_object(Bucket='nope')\n"
        "    _env.make_client('not-a-service').get_object(Bucket='nope')\n"
    )
    _nodes, edges = CodeParser().parse_bytes(Path("/work/svc/app.py"), src.encode())
    rows = {e.target: e.extra for e in edges if e.target.startswith("external:aws-")}
    assert set(rows) == {
        "external:aws-states:sm", "external:aws-s3:bkt", "external:aws-ssm:/a/b",
    }
    for extra in rows.values():
        assert extra["confidence"] <= 0.5
        assert extra["confidence_tier"] == "INFERRED"
        assert extra["client_inferred"] == "factory"


def test_direct_boto3_clients_are_not_marked_inferred() -> None:
    src = "import boto3\nc = boto3.client('s3')\nc.get_object(Bucket='bkt', Key='k')\n"
    _n, edges = CodeParser().parse_bytes(Path("/work/svc/app.py"), src.encode())
    extra = next(e.extra for e in edges if e.target == "external:aws-s3:bkt")
    assert extra["confidence"] == 1.0 and "client_inferred" not in extra
