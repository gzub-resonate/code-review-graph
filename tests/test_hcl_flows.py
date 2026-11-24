"""Terraform root modules are flow entry points that run through the modules they call."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.flows import (
    detect_entry_points,
    get_flow_by_id,
    store_flows,
    trace_flows,
)
from code_review_graph.graph import GraphStore, node_to_dict
from code_review_graph.incremental import full_build


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(root: Path) -> None:
    _write(root / "main.tf", """\
variable "env" {}

module "a" {
  source = "./modules/a"
  env    = var.env
}

resource "example_bucket" "root" {}
""")
    _write(root / "modules" / "a" / "main.tf", """\
variable "env" {}

resource "example_queue" "q" {}
resource "example_topic" "t" {}

output "queue_id" {
  value = example_queue.q.id
}
""")


def test_root_module_flow_passes_through_called_module(tmp_path: Path) -> None:
    _fixture(tmp_path)
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    try:
        full_build(tmp_path, store)

        entries = detect_entry_points(store)
        entry_files = {e.file_path for e in entries if e.language == "hcl" and e.kind == "File"}
        # The called module is not a root; the directory that calls it is.
        assert (tmp_path / "main.tf").as_posix() in {Path(p).as_posix() for p in entry_files}
        assert all("modules" not in Path(p).parts for p in entry_files)

        flows = trace_flows(store)
        hcl = [f for f in flows if f["name"] == "terraform:."]
        assert len(hcl) == 1
        flow = hcl[0]

        store_flows(store, flows)
        detail = get_flow_by_id(store, _flow_id(store))
        assert detail is not None
        names = [s["name"] for s in detail["steps"]]
        assert "module.a" in names
        assert "resource.example_bucket.root" in names
        # Resources of the called module are reached through the module call.
        assert "resource.example_queue.q" in names
        assert "resource.example_topic.t" in names
        assert names.index("module.a") < names.index("resource.example_queue.q")
        # Inputs and outputs are not steps.
        assert not any(n.startswith(("var.", "output.")) for n in names)
        assert flow["file_count"] == 2
    finally:
        store.close()


def _flow_id(store: GraphStore) -> int:
    row = store._conn.execute("SELECT id FROM flows WHERE name = 'terraform:.'").fetchone()
    return int(row["id"])


def test_resource_and_variable_are_distinguishable(tmp_path: Path) -> None:
    _fixture(tmp_path)
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    try:
        full_build(tmp_path, store)
        by_name = {
            n.name: n for n in store.get_nodes_by_kind(["Class", "Function"])
            if n.language == "hcl"
        }
        assert node_to_dict(by_name["resource.example_queue.q"])["hcl_type"] == "resource"
        assert node_to_dict(by_name["var.env"])["hcl_type"] == "variable"
        assert node_to_dict(by_name["output.queue_id"])["hcl_type"] == "output"
        assert node_to_dict(by_name["module.a"])["hcl_type"] == "module"
    finally:
        store.close()


def test_root_creating_more_resources_ranks_higher(tmp_path: Path) -> None:
    _write(tmp_path / "small" / "main.tf", 'resource "x_a" "one" {}\nresource "x_a" "two" {}\n')
    body = "".join(f'resource "x_b" "r{i}" {{}}\n' for i in range(12))
    _write(tmp_path / "big" / "main.tf", body)
    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    try:
        full_build(tmp_path, store)
        flows = [f for f in trace_flows(store) if f["name"].startswith("terraform:")]
        assert [f["name"] for f in flows] == ["terraform:big", "terraform:small"]
    finally:
        store.close()
