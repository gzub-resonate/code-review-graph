"""``exclude_data_types`` on get_knowledge_gaps: enums and data carriers leave the untested
hotspots when asked; the default keeps them, like get_hub_nodes."""

from __future__ import annotations

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.paging import CursorError
from code_review_graph.parser import EdgeInfo, NodeInfo, normalize_file_path
from code_review_graph.tools import get_knowledge_gaps_func


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        s.root = tmp_path
        yield s


def _type(repo, name, callers, extra, methods=()):
    file = normalize_file_path(repo.root / f"src/main/{name}.java")
    repo.upsert_node(NodeInfo("Class", name, file, 1, 50, "java", extra=extra))
    for m, params in methods:
        repo.upsert_node(NodeInfo(
            "Function", m, file, 2, 4, "java", parent_name=name, params=params,
        ))
    qn = f"{file}::{name}"
    for i in range(callers):
        caller = normalize_file_path(repo.root / f"src/main/{name}Caller{i}.java")
        repo.upsert_node(NodeInfo("Function", f"use{name}{i}", caller, 1, 3, "java"))
        repo.upsert_edge(EdgeInfo("CALLS", f"{caller}::use{name}{i}", qn, caller, 3))
    return qn


@pytest.fixture
def built(repo):
    q = {
        "enum": _type(repo, "RunStatus", 9, {"type_form": "enum"}),
        "lombok": _type(
            repo, "UserDto", 8,
            {"java_kind": "concrete", "decorators": ["Data", "Builder"]},
        ),
        "real": _type(
            repo, "PriceCalc", 7, {"java_kind": "concrete", "decorators": []},
            methods=[("compute", "(int x)")],
        ),
    }
    repo.commit()
    return q


def _hotspots(reply):
    return [row["qualified_name"] for row in reply["gaps"]["untested_hotspots"]]


def test_default_keeps_data_types_in_the_hotspots(repo, built):
    reply = get_knowledge_gaps_func(repo_root=str(repo.root), max_per_category=50)
    assert built["enum"] in _hotspots(reply) and built["lombok"] in _hotspots(reply)
    assert reply["data_types_excluded"] == 0


def test_exclude_data_types_drops_enums_and_keeps_behavior_classes(repo, built):
    reply = get_knowledge_gaps_func(
        repo_root=str(repo.root), max_per_category=50, exclude_data_types=True,
    )
    hotspots = _hotspots(reply)
    assert built["enum"] not in hotspots and built["lombok"] not in hotspots
    assert built["real"] in hotspots
    assert reply["data_types_excluded"] == 2
    assert reply["summary"]["untested_hotspots"] == len(hotspots)


def test_a_cursor_is_tied_to_the_data_type_choice(repo, built):
    first = get_knowledge_gaps_func(repo_root=str(repo.root), max_per_category=1)
    assert first["next_cursor"]
    with pytest.raises(CursorError, match="different query"):
        get_knowledge_gaps_func(
            repo_root=str(repo.root), max_per_category=1, exclude_data_types=True,
            cursor=first["next_cursor"],
        )
