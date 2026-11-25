"""Dead code skips generated files; test source sets and git hooks are not production."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, is_test_file
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.query import _is_non_production_file

GENERATED = (
    "package gen;\npublic class Tables {\n"
    "    public static int unusedAccessor() { return 1; }\n}\n"
)
HANDWRITTEN = (
    "package app;\npublic class Util {\n"
    "    public static int unusedHelper() { return 1; }\n}\n"
)


def _dead(tmp_path: Path, files: dict[str, str], **kwargs) -> set[str]:
    repo = tmp_path / "repo"
    paths = []
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    (repo / ".git").mkdir(exist_ok=True)
    parser = CodeParser(repo)
    store = GraphStore(tmp_path / "graph.db")
    try:
        for path in paths:
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges, "")
        return {d["name"] for d in find_dead_code(store, root=repo, **kwargs)}
    finally:
        store.close()


FILES = {
    "src/generated/jooq/gen/Tables.java": GENERATED,
    "src/main/java/app/Util.java": HANDWRITTEN,
}


def test_generated_symbols_are_not_reported_as_dead(tmp_path):
    dead = _dead(tmp_path, FILES)
    assert "unusedHelper" in dead
    assert "unusedAccessor" not in dead
    assert "Tables" not in dead


def test_include_generated_brings_them_back(tmp_path):
    dead = _dead(tmp_path, FILES, include_generated=True)
    assert {"unusedHelper", "unusedAccessor"} <= dead


@pytest.mark.parametrize("path", [
    "src/qaTest/java/com/acme/OwnSiteTagWithChild.java",
    "src/integrationTest/java/com/acme/GatewayIT.java",
])
def test_extra_gradle_test_source_sets_are_test_code(tmp_path, path):
    assert is_test_file(tmp_path / path, tmp_path)
    assert _is_non_production_file(str(tmp_path / path), tmp_path)


@pytest.mark.parametrize("path", [
    "src/contest/java/com/acme/Entry.java",
    "src/main/java/com/acme/Latest.java",
])
def test_similar_names_stay_production(tmp_path, path):
    assert not is_test_file(tmp_path / path, tmp_path)


@pytest.mark.parametrize("path", [
    "gradle/git-hooks/pre-push",
    ".githooks/pre-commit",
    ".husky/pre-commit",
])
def test_git_hook_scripts_are_not_production(tmp_path, path):
    assert _is_non_production_file(str(tmp_path / path), tmp_path)
