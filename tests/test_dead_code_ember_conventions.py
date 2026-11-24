"""Dead code: Ember convention directories and ANTLR-generated files."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code

EMBER = {"ember-cli-build.js": "module.exports = function () {};\n"}


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
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
        return {d["name"] for d in find_dead_code(store, root=repo)}
    finally:
        store.close()


COMPONENT = "export default class FooWidget {\n  didInsertElement() {}\n}\n"
UTIL = "export function orphanUtil() { return 1; }\n"


def test_component_in_ember_repo_not_reported(tmp_path):
    dead = _dead(tmp_path, {**EMBER, "app/components/foo-widget.js": COMPONENT})
    assert "FooWidget" not in dead


def test_component_without_ember_cli_build_still_reported(tmp_path):
    dead = _dead(tmp_path, {"app/components/foo-widget.js": COMPONENT})
    assert "FooWidget" in dead


def test_non_convention_util_still_reported(tmp_path):
    dead = _dead(tmp_path, {**EMBER, "app/utils/helper.js": UTIL})
    assert "orphanUtil" in dead


def test_server_and_config_not_reported(tmp_path):
    dead = _dead(tmp_path, {
        **EMBER,
        "server/index.js": "function serverThing() {}\n",
        "config/environment.js": "function envThing() {}\n",
        "lib/other.js": "function libThing() {}\n",
    })
    assert "serverThing" not in dead
    assert "envThing" not in dead
    assert "libThing" in dead


def test_antlr_generated_file_not_reported(tmp_path):
    header = "// Generated from Rql.g4 by ANTLR 4.9.2\n"
    dead = _dead(tmp_path, {
        "src/gen.js": header + "export function genRule() {}\n",
        "src/plain.js": "export function plainOrphan() {}\n",
    })
    assert "genRule" not in dead
    assert "plainOrphan" in dead


def test_route_absent_from_router_still_reported(tmp_path):
    router = (
        "Router.map(function () {\n"
        "  this.route('dashboard');\n"
        "  // this.route('elections-report');\n"
        "});\n"
    )
    dead = _dead(tmp_path, {
        **EMBER,
        "app/router.js": router,
        "app/routes/dashboard.js": "export default class DashboardRoute {}\n",
        "app/routes/elections-report.js": "export default class ElectionsReportRoute {}\n",
    })
    assert "DashboardRoute" not in dead
    assert "ElectionsReportRoute" in dead
