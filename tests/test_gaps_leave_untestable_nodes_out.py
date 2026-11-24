"""Knowledge gaps leave out what no test can cover and what the interpreter calls.

A config document key and a Step Functions state are not untested hotspots, and a class's
``__init__`` or ``__repr__`` is not an isolated node; ``include_non_code`` brings them back.
"""

from __future__ import annotations

import pytest

from code_review_graph.analysis import find_knowledge_gaps
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo


def _node(store, kind, name, path, lang, parent=None):
    store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=path, line_start=1, line_end=2, language=lang,
        parent_name=parent,
    ))
    return f"{path}::{parent + '.' if parent else ''}{name}"


@pytest.fixture
def store(tmp_path):
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(get_db_path(tmp_path)) as s:
        s.set_metadata("repo_root", str(tmp_path))
        cls = _node(s, "Class", "Widget", "a.py", "python")
        init = _node(s, "Function", "__init__", "a.py", "python", "Widget")
        s.upsert_edge(EdgeInfo(kind="CONTAINS", source=cls, target=init, file_path="a.py", line=1))
        yaml_root = _node(s, "YamlPath", "$.functions", "cfg.yaml", "yaml")
        machine = _node(s, "Function", "machine", "m.asl.json", "asl")
        code = _node(s, "Function", "hot", "a.py", "python")
        for i in range(6):
            caller = _node(s, "Function", f"caller{i}", "b.py", "python")
            for target in (yaml_root, machine, code):
                s.upsert_edge(EdgeInfo(
                    kind="CALLS", source=caller, target=target, file_path="b.py", line=i + 1,
                ))
        s.commit()
        yield s


def _names(gaps, key):
    return {row["name"] for row in gaps[key]}


def test_config_and_step_function_nodes_are_not_untested_hotspots(store):
    hot = _names(find_knowledge_gaps(store), "untested_hotspots")
    assert "hot" in hot
    assert "$.functions" not in hot and "machine" not in hot


def test_dunder_methods_are_not_isolated_nodes(store):
    assert "__init__" not in _names(find_knowledge_gaps(store), "isolated_nodes")


def test_include_non_code_brings_them_back(store):
    gaps = find_knowledge_gaps(store, include_non_code=True)
    assert {"$.functions", "machine"} <= _names(gaps, "untested_hotspots")
    assert "__init__" in _names(gaps, "isolated_nodes")
