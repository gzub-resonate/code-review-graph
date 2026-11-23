"""``query_graph`` answers "who points at this?" for a unit outside the repository.

Parsers record a call to code that is not in the graph as an edge to
``external:<kind>:<name>``. No node has that name, so the query reads the edges that end
there instead of searching for a symbol.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query_graph

LAMBDA = "external:aws-lambda:order-validator"
ACTION = "external:github-actions:actions/checkout"


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    machine = (root / "flow.asl.json").as_posix()
    workflow = (root / "ci.yml").as_posix()
    util = (root / "util.py").as_posix()
    store.upsert_node(NodeInfo(
        kind="File", name="flow.asl.json", file_path=machine, line_start=1, line_end=9,
        language="json",
    ))
    for name, line in (("Validate", 3), ("Revalidate", 6)):
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=machine, line_start=line, line_end=line + 1,
            language="json",
        ))
        store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source=f"{machine}::{name}", target=LAMBDA,
            file_path=machine, line=line, extra={"resource_kind": "lambda"},
        ))
    store.upsert_node(NodeInfo(
        kind="File", name="ci.yml", file_path=workflow, line_start=1, line_end=9,
        language="yaml",
    ))
    store.upsert_edge(EdgeInfo(
        kind="IMPORTS_FROM", source=workflow, target=ACTION, file_path=workflow, line=4,
        extra={"resolution": "github_actions_uses"},
    ))
    store.upsert_node(NodeInfo(
        kind="Function", name="call_out", file_path=util, line_start=1, line_end=3,
        language="python",
    ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=f"{util}::call_out", target="external:aws-lambda:billing",
        file_path=util, line=2,
    ))
    store.commit()
    yield root
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def ask(root, pattern, target, **extra):
    return query_graph(pattern=pattern, target=target, repo_root=str(root), **extra)


def test_references_to_an_external_target_lists_every_referencing_state(repo):
    reply = ask(repo, "references_to", LAMBDA)
    assert reply["status"] == "ok", reply
    assert sorted(r["name"] for r in reply["results"]) == ["Revalidate", "Validate"]
    assert all(r["file"] if "file" in r else True for r in reply["results"])
    assert {e["kind"] for e in reply["edges"]} == {"REFERENCES"}
    assert {e["target"] for e in reply["edges"]} == {LAMBDA}


def test_importers_of_an_external_target_lists_the_importing_file(repo):
    reply = ask(repo, "importers_of", ACTION)
    assert reply["status"] == "ok", reply
    assert [r["importer"].rsplit("/", 1)[-1] for r in reply["results"]] == ["ci.yml"]
    assert reply["results"][0]["file"].endswith("ci.yml")


def test_callers_of_an_external_target_lists_the_calling_function(repo):
    reply = ask(repo, "callers_of", "external:aws-lambda:billing")
    assert reply["status"] == "ok", reply
    assert [r["name"] for r in reply["results"]] == ["call_out"]


def test_an_external_target_nothing_points_at_is_an_empty_answer_not_a_missing_node(repo):
    for pattern in ("references_to", "importers_of", "callers_of"):
        reply = ask(repo, pattern, "external:aws-lambda:nobody")
        assert reply["status"] == "ok", (pattern, reply)
        assert reply["results"] == []


def test_other_edge_kinds_do_not_answer_for_the_wrong_pattern(repo):
    assert ask(repo, "importers_of", LAMBDA)["results"] == []
    assert ask(repo, "references_to", ACTION)["results"] == []
    assert ask(repo, "callers_of", LAMBDA)["results"] == []


def test_only_the_edge_patterns_read_an_external_target(repo):
    for pattern in ("callees_of", "children_of", "tests_for", "imports_of"):
        assert ask(repo, pattern, LAMBDA)["status"] == "not_found", pattern


def test_a_node_name_that_merely_starts_like_a_target_is_still_a_symbol(repo):
    reply = ask(repo, "references_to", "Validate")
    assert reply["status"] in {"ok", "ambiguous", "not_found"}
    assert not any(r.get("name") == "Revalidate" for r in reply.get("results", []))


def test_the_result_cap_still_applies_to_an_external_target(repo):
    reply = ask(repo, "references_to", LAMBDA, max_results=1)
    assert reply["status"] == "ok" and len(reply["results"]) == 1
    assert reply["result_count"] == 2 and reply["results_omitted"] == 1
