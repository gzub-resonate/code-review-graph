"""CommonJS default exports in imported files, minimal import rows, and signatures.

Three small behaviours found while reading a Node.js service through the tools:
``module.exports`` was reported dead although another file requires its file,
``imports_of`` and ``importers_of`` came back as ``{}`` rows at minimal detail, and a
JavaScript function got a Python-style ``def name((a, b))`` signature.
"""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code
from code_review_graph.signatures import format_signature
from code_review_graph.tools.query import query_graph


def _store(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    paths = []
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    (repo / ".git").mkdir(exist_ok=True)
    parser = CodeParser(repo)
    (repo / ".code-review-graph").mkdir(exist_ok=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    for path in paths:
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(str(path), nodes, edges, "")
    return repo, store


APP = "const render = require('./routes/render');\nrender();\n"
ROUTE = "module.exports = function handler(req, res) { return 1; };\n"
LONELY = "module.exports = function orphan() { return 2; };\n"


def test_default_export_of_an_imported_file_is_not_dead(tmp_path):
    repo, store = _store(
        tmp_path, {"app.js": APP, "routes/render.js": ROUTE, "tools/lonely.js": LONELY},
    )
    try:
        dead = {
            (d["relative_path"], d["name"])
            for d in find_dead_code(store, root=repo)
        }
    finally:
        store.close()
    assert ("routes/render.js", "module.exports") not in dead
    assert ("tools/lonely.js", "module.exports") in dead


def test_minimal_import_queries_keep_their_fields(tmp_path):
    repo, store = _store(tmp_path, {"app.js": APP, "routes/render.js": ROUTE})
    store.close()
    imports = query_graph(
        pattern="imports_of", target="app.js", repo_root=str(repo), detail_level="minimal",
    )
    assert imports["results"], imports
    assert all(row for row in imports["results"])
    assert "import_target" in imports["results"][0]
    importers = query_graph(
        pattern="importers_of", target="routes/render.js", repo_root=str(repo),
        detail_level="minimal",
    )
    assert importers["results"], importers
    assert all(row for row in importers["results"])
    assert "importer" in importers["results"][0]


def test_signature_does_not_double_parentheses_or_force_def():
    assert format_signature("renderPPT", "Function", "(page, opts)", None, "javascript") == (
        "renderPPT(page, opts)"
    )
    assert format_signature("run", "Function", "self, x", "int", "python") == (
        "def run(self, x) -> int"
    )
    assert format_signature("f", "Function", None, None, None) == "def f()"
    assert format_signature("Widget", "Class", None, None, "java") == "class Widget"
    assert format_signature("KEY", "Variable", None, None, "java") == "KEY"
