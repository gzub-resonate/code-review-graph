"""A readable ``source`` on ``awssdk`` rows even for a symbol with a collision suffix.

Two same-named symbols of one file are stored with ``:S<12 hex>`` in their identity. The row's
``source`` shows the plain name; ``source_id`` keeps the identity, and ``source=`` accepts both.
"""

from __future__ import annotations

import re

from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_asl_cross_repo_links import make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401

APP = {
    "svc/dup.py": (
        "import boto3\nsqs = boto3.client('sqs')\n\n\n"
        "class A:\n    def run(self):\n        sqs.send_message(QueueUrl='https://sqs.us-east-1."
        "amazonaws.com/1/first-queue')\n\n"
        "    def run(self):\n        sqs.send_message(QueueUrl='https://sqs.us-east-1."
        "amazonaws.com/1/second-queue')\n"
    ),
}


def _rows(**kwargs):
    reply = cross_repo_requests_func(origin="awssdk", repos=["app"], **kwargs)
    assert reply["status"] == "ok", reply
    return reply["results"]


def test_source_is_readable_and_source_id_keeps_the_identity(tmp_path, registry) -> None:
    make_repo(tmp_path, registry, "app", APP)
    rows = [r for r in _rows() if r["operation"] == "send_message"]
    assert len(rows) == 2
    suffixed = [r for r in rows if "source_id" in r]
    assert suffixed, rows
    for row in suffixed:
        assert row["source"] == "A.run"
        assert re.search(r":S[0-9a-f]{12}", row["source_id"])
        assert not re.search(r":S[0-9a-f]{12}", row["source"])
        found = _rows(source=row["source_id"])
        assert [r["target_name"] for r in found] == [row["target_name"]]
    assert len(_rows(source="A.run")) == 2
