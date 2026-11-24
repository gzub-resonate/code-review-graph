"""Flows and impact analysis cross from a Step Functions state machine into Lambda code.

``asl_resolver`` links a Task state to the handler function it runs
(``asl_lambda_handler``). A flow that starts at a machine follows that edge into the
handler's calls, and impact analysis walks it backwards, so a changed handler reaches the
state and the machine that run it.
"""

from __future__ import annotations

from code_review_graph.changes import analyze_changes
from code_review_graph.flows import store_flows, trace_flows

from .asl_links_helpers import FILES, MACHINE_PATH, build_repo, state

HANDLER_QN = "python-lambdas/validateOrder/validate_order.py::handler"
HELPER_QN = "python-lambdas/validateOrder/validate_order.py::helper"


class TestFlowsAndImpact:
    def test_a_flow_runs_from_the_machine_into_the_handlers_calls(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            prefix = root.resolve().as_posix() + "/"
            flows = [f for f in trace_flows(store) if f["name"] == "orders"]
            assert len(flows) == 1
            by_id = {
                row[0]: row[1].replace(prefix, "")
                for row in store._conn.execute("SELECT id, qualified_name FROM nodes")
            }
            path = [by_id[node_id] for node_id in flows[0]["path"]]
            assert path[0] == state("Validate")
            assert HANDLER_QN in path and HELPER_QN in path
            assert state("Notify") in path and state("Unfilled") in path
        finally:
            store.close()

    def test_a_changed_handler_reaches_the_state_and_its_machine_file(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            resolved = root.resolve()
            handler_file = (resolved / "python-lambdas/validateOrder/validate_order.py").as_posix()
            machine_file = (resolved / MACHINE_PATH).as_posix()
            impact = store.get_impact_radius([handler_file])
            impacted = {node.qualified_name for node in impact["impacted_nodes"]}
            assert f"{machine_file}::orders/Validate" in impacted
            assert machine_file in impact["impacted_files"]

            # detect_changes reports the state machine's flow as affected by the handler.
            store_flows(store, trace_flows(store))
            analysis = analyze_changes(store, [handler_file], repo_root=str(resolved))
            assert "orders" in {flow["name"] for flow in analysis["affected_flows"]}
        finally:
            store.close()

    def test_without_the_link_the_machine_is_not_reached(self, tmp_path):
        files = dict(FILES)
        for env in ("prod", "nonprod"):
            del files[f"terraform/python-lambdas/validateOrder/{env}/terragrunt.hcl"]
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            resolved = root.resolve()
            handler_file = (resolved / "python-lambdas/validateOrder/validate_order.py").as_posix()
            impact = store.get_impact_radius([handler_file])
            assert (resolved / MACHINE_PATH).as_posix() not in impact["impacted_files"]
            store_flows(store, trace_flows(store))
            analysis = analyze_changes(store, [handler_file], repo_root=str(resolved))
            assert "orders" not in {flow["name"] for flow in analysis["affected_flows"]}
        finally:
            store.close()
