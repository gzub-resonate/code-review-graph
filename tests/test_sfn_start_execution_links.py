"""A Step Functions StartExecution through a client factory and a parameter (project-disrupt).

``_sfn.get_client().start_execution(stateMachineArn=state_machine_arn)`` lives in a launcher
whose client comes from a factory in another file and whose ARN is a parameter. The factory is
read through the import, and each caller that passes the parameter by keyword gets its own row.
The fixture is ``tests/sfn_fixture.py``.
"""

from __future__ import annotations

from pathlib import Path

from .sfn_fixture import DEV, FILES, PKG, aws_rows, write_repo

NBC = "external:aws-states:disrupt-next-best-customer"


def _by_line(rows: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(row["line"], []).append(row)
    return out


def test_a_factory_in_another_file_gives_a_low_confidence_row(tmp_path: Path) -> None:
    root = write_repo(tmp_path)
    row = _by_line(aws_rows(root, f"{PKG}/_launch.py"))[5][0]
    assert row["service"] == "states"
    assert row["operation"] == "start_execution"
    assert row["sdk_operation"] == "StartExecution"
    assert row["sdk"] == "boto3"
    assert row["access"] == "write"
    assert row["client_inferred"] == "factory"
    assert row["client_factory"] == f"{PKG}/_sfn.py::get_client"
    assert row["confidence"] <= 0.5 < 1.0
    assert row["confidence_tier"] == "INFERRED"
    # the ARN is the function's own parameter, so this row names nothing
    assert row["unresolved"] and row["resource"] == "?"
    assert row["parameter"] == "state_machine_arn"
    assert row["parameter_function"] == "submit"
    assert row["unresolved_reason"] == "name_is_parameter"


def test_a_directly_built_client_keeps_full_confidence(tmp_path: Path) -> None:
    files = {
        **FILES,
        f"{PKG}/direct.py": (
            "import boto3\nc = boto3.client('stepfunctions')\n"
            "c.start_execution(stateMachineArn='arn:aws:states:us-east-1:1:stateMachine:m')\n"
        ),
    }
    row = aws_rows(write_repo(tmp_path, files), f"{PKG}/direct.py")[0]
    assert row["confidence"] == 1.0 and "client_factory" not in row


def test_a_factory_with_a_variable_service_makes_no_row(tmp_path: Path) -> None:
    files = {
        **FILES,
        f"{PKG}/_any.py": (
            "from ._env import make_client\n\n\ndef get_client(name):\n"
            "    return make_client(name)\n"
        ),
        f"{PKG}/use_any.py": (
            "from . import _any\n\n\ndef go():\n"
            "    _any.get_client('x').start_execution(stateMachineArn='arn:aws:states:"
            "us-east-1:1:stateMachine:m')\n"
        ),
    }
    assert aws_rows(write_repo(tmp_path, files), f"{PKG}/use_any.py") == []


def test_a_factory_that_cannot_be_found_makes_no_row(tmp_path: Path) -> None:
    files = {
        f"{PKG}/use_missing.py": (
            "from . import _gone\nfrom somewhere import get_client\n\n\ndef go():\n"
            "    _gone.get_client().start_execution(stateMachineArn='x')\n"
            "    get_client().start_execution(stateMachineArn='x')\n"
        ),
    }
    assert aws_rows(write_repo(tmp_path, files), f"{PKG}/use_missing.py") == []


def test_a_factory_returning_two_services_makes_no_row(tmp_path: Path) -> None:
    files = {
        f"{PKG}/_two.py": (
            "import boto3\n\n\ndef get_client(flag):\n    if flag:\n"
            "        return boto3.client('s3')\n    return boto3.client('stepfunctions')\n"
        ),
        f"{PKG}/use_two.py": (
            "from . import _two\n\n\ndef go():\n"
            "    _two.get_client(1).start_execution(stateMachineArn='x')\n"
        ),
    }
    assert aws_rows(write_repo(tmp_path, files), f"{PKG}/use_two.py") == []


def test_a_session_client_and_an_imported_function_name_bind(tmp_path: Path) -> None:
    files = {
        f"{PKG}/_s3.py": (
            "import boto3\n\n\ndef bucket_client():\n"
            "    return boto3.Session(region_name='us-east-1').client('s3')\n"
        ),
        f"{PKG}/use_s3.py": (
            "from ._s3 import bucket_client as bc\n\n\ndef go():\n"
            "    bc().put_object(Bucket='my-bucket', Key='k')\n"
        ),
    }
    row = aws_rows(write_repo(tmp_path, files), f"{PKG}/use_s3.py")[0]
    assert row["target"] == "external:aws-s3:my-bucket"
    assert row["client_factory"] == f"{PKG}/_s3.py::bucket_client"


def test_a_literal_keyword_argument_resolves_through_the_parameter(tmp_path: Path) -> None:
    rows = _by_line(aws_rows(write_repo(tmp_path), f"{PKG}/run_nbc.py"))
    row = rows[13][0]
    assert row["target"] == "external:aws-states:lit-sm"
    assert row["target_source"] == "parameter"
    assert row["arg_form"] == "literal"
    assert row["bound_via"] == f"{PKG}/_launch.py::submit(state_machine_arn)"
    assert row["operation"] == "start_execution" and row["access"] == "write"
    assert row["confidence"] == 0.4 and row["confidence_tier"] == "INFERRED"
    assert not row.get("unresolved") and "override_possible" not in row
    assert rows[17][0]["arg_form"] == "constant"
    assert rows[17][0]["target"] == "external:aws-states:disrupt-etl-2"


def test_dev_and_prod_constants_collapse_to_one_target(tmp_path: Path) -> None:
    assert DEV.rsplit(":", 1)[-1] == "disrupt-next-best-customer"
    row = _by_line(aws_rows(write_repo(tmp_path), f"{PKG}/run_nbc.py"))[5]
    assert len(row) == 1  # one row, one name, from the two ARNs of the default function
    row = row[0]
    assert row["target"] == NBC
    assert row["arg_form"] == "or"
    assert row["default_from"] == (
        f"{PKG}/_env.py::default_next_best_customer_state_machine_arn"
    )
    # ``x or default()``: the left side is a request field, so the call may name another machine
    assert row["override_possible"] is True
    assert row["confidence"] < 0.4
    assert "target_alternatives" not in row


def test_a_default_function_whose_branches_differ_gives_a_row_per_machine(
    tmp_path: Path,
) -> None:
    rows = _by_line(aws_rows(write_repo(tmp_path), f"{PKG}/run_nbc.py"))[25]
    assert {r["resource"] for r in rows} == {"disrupt-etl-2", "disrupt-next-best-customer"}
    assert all(r["target_alternatives"] == 2 for r in rows)


def test_an_argument_that_cannot_be_reduced_stays_unresolved(tmp_path: Path) -> None:
    rows = _by_line(aws_rows(write_repo(tmp_path), f"{PKG}/run_nbc.py"))
    for line in (21, 29, 33):  # an attribute, a default that reads the environment, a secret
        row = rows[line][0]
        assert row["unresolved"] is True and row["resource"] == "?"
        assert row["unresolved_reason"] == "argument_not_static"
        assert row["target_source"] == "parameter"
        assert row["confidence"] == 0.3
        assert row["bound_via"].endswith("::submit(state_machine_arn)")


def test_a_call_of_a_function_in_the_same_file_binds_too(tmp_path: Path) -> None:
    files = {
        f"{PKG}/same.py": (
            "import boto3\nsfn = boto3.client('stepfunctions')\n\n\n"
            "def start(state_machine_arn):\n"
            "    sfn.start_execution(stateMachineArn=state_machine_arn)\n\n\n"
            "def go():\n"
            "    start(state_machine_arn='arn:aws:states:us-east-1:1:stateMachine:inside')\n"
        ),
    }
    rows = _by_line(aws_rows(write_repo(tmp_path, files), f"{PKG}/same.py"))
    assert rows[6][0]["unresolved_reason"] == "name_is_parameter"
    assert rows[6][0]["confidence"] == 0.6 and "client_inferred" not in rows[6][0]
    assert rows[10][0]["target"] == "external:aws-states:inside"


def test_a_parameter_the_function_reassigns_is_not_bound(tmp_path: Path) -> None:
    files = {
        f"{PKG}/re.py": (
            "import boto3\nsfn = boto3.client('stepfunctions')\n\n\n"
            "def start(state_machine_arn):\n"
            "    state_machine_arn = state_machine_arn.strip()\n"
            "    sfn.start_execution(stateMachineArn=state_machine_arn)\n\n\n"
            "def go():\n    start(state_machine_arn='x')\n"
        ),
    }
    rows = aws_rows(write_repo(tmp_path, files), f"{PKG}/re.py")
    assert [r["line"] for r in rows] == [7]
    assert "parameter" not in rows[0]


def test_nothing_is_read_outside_the_repo_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "_sfn.py").write_text(
        "import boto3\n\n\ndef get_client():\n    return boto3.client('stepfunctions')\n",
    )
    files = {
        f"{PKG}/escape.py": (
            "from ..... import _sfn\n\n\ndef go():\n"
            "    _sfn.get_client().start_execution(stateMachineArn='x')\n"
        ),
    }
    root = write_repo(tmp_path / "w", files)
    (root.parent / "_sfn.py").write_text((outside / "_sfn.py").read_text())
    assert aws_rows(root, f"{PKG}/escape.py") == []
