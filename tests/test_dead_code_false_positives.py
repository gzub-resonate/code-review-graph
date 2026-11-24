"""Dead-code false positives: uses that no call names.

Each test parses real source text into a graph and asks ``find_dead_code``,
one rule per test class: values passed as arguments, type-position uses,
re-exports, Scala entry points, inline handler strings, applied HCL blocks and
``Class.MEMBER`` reads. Each class also pins a genuinely dead sibling, so a
rule cannot pass by suppressing everything.
"""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
    """Names reported dead after parsing *files* (relative path -> source)."""
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


class TestFunctionPassedAsValue:
    def test_python_nested_function_passed_to_map(self, tmp_path):
        src = (
            "def run(items):\n"
            "    def worker(item):\n"
            "        return item\n"
            "    def unused_helper(item):\n"
            "        return item\n"
            "    return list(map(worker, items))\n"
        )
        dead = _dead(tmp_path, {"jobs.py": src})
        assert "worker" not in dead
        assert "unused_helper" in dead

    def test_html_script_function_in_wrapper_passed_to_map(self, tmp_path):
        page = (
            "<html><body><script>\n"
            "function init(pending) {\n"
            "  function savePanel(p) { return p; }\n"
            "  function neverUsedPanel(p) { return p; }\n"
            "  return Promise.all(pending.map(savePanel));\n"
            "}\n"
            "init([]);\n"
            "</script></body></html>\n"
        )
        dead = _dead(tmp_path, {"page.html": page})
        assert "savePanel" not in dead
        assert "neverUsedPanel" in dead

    def test_scala_method_passed_to_map(self, tmp_path):
        src = (
            "object Parsers {\n"
            "  def parse(line: String): Int = 1\n"
            "  def orphan(line: String): Int = 2\n"
            "  def all(xs: Seq[String]) = xs.map(parse)\n"
            "}\n"
        )
        dead = _dead(tmp_path, {"Parsers.scala": src})
        assert "parse" not in dead
        assert "orphan" in dead


class TestTypePositionUse:
    def test_tsx_props_interface_in_generic_annotation(self, tmp_path):
        src = (
            "import React from 'react';\n"
            "interface AnalysisContentProps { title: string }\n"
            "interface UnusedProps { id: number }\n"
            "export const AnalysisContent: React.FC<Readonly<AnalysisContentProps>> = "
            "() => null;\n"
        )
        dead = _dead(tmp_path, {"app/AnalysisContent.tsx": src})
        assert "AnalysisContentProps" not in dead
        assert "UnusedProps" in dead

    def test_python_variable_annotation(self, tmp_path):
        src = (
            "from typing import List\n"
            "class Item: pass\n"
            "class Unused: pass\n"
            "items: List[Item] = []\n"
        )
        dead = _dead(tmp_path, {"models.py": src})
        assert "Item" not in dead
        assert "Unused" in dead

    def test_scala_generic_type_argument(self, tmp_path):
        src = (
            "case class Row(a: Int)\n"
            "case class Spare(a: Int)\n"
            "object Store { val rows: Seq[Row] = Nil }\n"
        )
        dead = _dead(tmp_path, {"Store.scala": src})
        assert "Row" not in dead
        assert "Spare" in dead

    def test_java_generic_field(self, tmp_path):
        src = (
            "import java.util.List;\n"
            "class Item {}\n"
            "class Spare {}\n"
            "class Holder { private List<Item> items; }\n"
        )
        dead = _dead(tmp_path, {"Holder.java": src})
        assert "Item" not in dead
        assert "Spare" in dead


class TestCrossFileTypeUse:
    def test_scala_type_argument_in_another_file(self, tmp_path):
        files = {
            "schema/AvDistribution.scala": "final case class AvDistribution(a: String)\n"
            "final case class SpareSchema(a: String)\n",
            "job/Metrics.scala": "object Metrics {\n"
            "  val enc = Encoders.product[AvDistribution]\n}\n",
        }
        dead = _dead(tmp_path, files)
        assert "AvDistribution" not in dead
        assert "SpareSchema" in dead


class TestReexports:
    def test_python_init_reexport(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .core import Translator\n",
            "pkg/core.py": "class Translator: pass\nclass Hidden: pass\n",
        }
        dead = _dead(tmp_path, files)
        assert "Translator" not in dead
        assert "Hidden" in dead

    def test_python_init_star_reexport(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .core import *\n",
            "pkg/core.py": "class Translator: pass\nclass _Private: pass\n",
        }
        dead = _dead(tmp_path, files)
        assert "Translator" not in dead
        assert "_Private" in dead

    def test_ts_named_reexport(self, tmp_path):
        files = {
            "src/index.ts": "export { Widget } from './widget';\n",
            "src/widget.ts": "export class Widget {}\nexport class Spare {}\n",
        }
        dead = _dead(tmp_path, files)
        assert "Widget" not in dead
        assert "Spare" in dead

    def test_ts_star_reexport(self, tmp_path):
        files = {
            "src/index.ts": "export * from './widget';\n",
            "src/widget.ts": "export function makeWidget() { return 1; }\n",
        }
        assert "makeWidget" not in _dead(tmp_path, files)


class TestScalaEntryPoints:
    def test_object_with_main_and_main_itself(self, tmp_path):
        src = (
            "object IngestApp {\n"
            "  def main(args: Array[String]): Unit = {}\n"
            "}\n"
            "object Helpers {\n"
            "  def unused(): Int = 1\n"
            "}\n"
        )
        dead = _dead(tmp_path, {"IngestApp.scala": src})
        assert "IngestApp" not in dead
        assert "main" not in dead
        assert "Helpers" in dead

    def test_object_extending_app(self, tmp_path):
        src = "object QuickApp extends App {\n  println(1)\n}\nobject Plain {}\n"
        dead = _dead(tmp_path, {"QuickApp.scala": src})
        assert "QuickApp" not in dead
        assert "Plain" in dead


class TestInlineHandlers:
    def test_handler_string_built_in_python(self, tmp_path):
        files = {
            "admin/render.py": (
                "def row(exc_id):\n"
                "    return f'<button onclick=\"grDeleteException(\\'{exc_id}\\')\">x</button>'\n"
            ),
            "admin/static/app.js": (
                "function grDeleteException(id) { return id; }\n"
                "function grNeverBound(id) { return id; }\n"
            ),
        }
        dead = _dead(tmp_path, files)
        assert "grDeleteException" not in dead
        assert "grNeverBound" in dead

    def test_html_attribute_names_function_in_another_file(self, tmp_path):
        files = {
            "t/page.html": '<button onclick="saveAll()">x</button>\n',
            "t/app.js": "function saveAll() { return 1; }\nfunction idle() { return 2; }\n",
        }
        dead = _dead(tmp_path, files)
        assert "saveAll" not in dead
        assert "idle" in dead

    def test_htmx_on_attribute(self, tmp_path):
        files = {
            "t/page.html": '<div hx-on:click="toggleRow(this)">x</div>\n',
            "t/app.js": "function toggleRow(el) { return el; }\n",
        }
        assert "toggleRow" not in _dead(tmp_path, files)


class TestHclAppliedBlocks:
    def test_resource_module_output_provider_never_dead(self, tmp_path):
        src = (
            'resource "aws_s3_bucket" "logs" {}\n'
            'module "net" { source = "./net" }\n'
            'output "bucket" { value = 1 }\n'
            'provider "aws" {}\n'
            'variable "unused_var" {}\n'
        )
        dead = _dead(tmp_path, {"main.tf": src})
        assert dead == {"var.unused_var"}


class TestEnumMemberAccess:
    def test_python_enum_member_read_in_same_file(self, tmp_path):
        src = (
            "from enum import Enum\n"
            "class FetchStatus(Enum):\n"
            "    SUCCESS = 1\n"
            "class OtherStatus(Enum):\n"
            "    NEVER = 2\n"
            "def ok(status):\n"
            "    return status == FetchStatus.SUCCESS\n"
        )
        dead = _dead(tmp_path, {"sync.py": src})
        assert "FetchStatus" not in dead
        assert "OtherStatus" in dead

    def test_ts_enum_member_read(self, tmp_path):
        src = (
            "enum Mode { A, B }\n"
            "enum Spare { X }\n"
            "export function pick() { return Mode.A; }\n"
        )
        dead = _dead(tmp_path, {"mode.ts": src})
        assert "Mode" not in dead
        assert "Spare" in dead


class TestArrowCallbackParameterIsNotAFunction:
    def test_single_parameter_arrow_creates_no_node(self, tmp_path):
        src = "export function ids(xs: {id: number}[]) { return xs.map(entry => entry.id); }\n"
        path = tmp_path / "ids.ts"
        path.write_text(src, encoding="utf-8")
        nodes, _ = CodeParser().parse_file(path)
        assert [n.name for n in nodes if n.kind == "Function"] == ["ids"]
