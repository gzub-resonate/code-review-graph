"""A Task state that runs a Lambda of the repository is linked to the handler function.

The Lambda Terragrunt unit records ``handler``, ``function_name`` and the source directory
``git_repo_sub_dir`` evaluates to; ``asl_resolver`` joins a state's
``external:aws-lambda:<name>`` to the unit deployed under that name and adds a ``REFERENCES``
edge to the ``Function`` node of the handler.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.asl_resolver import lambda_units, resolve_asl_local_targets
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import incremental_update
from code_review_graph.parser import CodeParser

from .asl_links_helpers import (
    FILES,
    HANDLER,
    LAMBDA_HCL,
    build_repo,
    derived_edges,
    lambda_unit,
    state,
    write_files,
)

LH = "asl_lambda_handler"
HANDLER_QN = "python-lambdas/validateOrder/validate_order.py::handler"
HELPER_QN = "python-lambdas/validateOrder/validate_order.py::helper"


def handler_edges(store: GraphStore, root: Path) -> list[tuple[str, str, str, float]]:
    return [
        (source, target, tier, confidence)
        for source, target, resolution, tier, confidence in derived_edges(store, root)
        if resolution == LH
    ]


class TestStaticHandler:
    def test_a_lambda_unit_records_handler_name_and_source_directory(self, tmp_path):
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        write_files(root, FILES)
        unit = root / "terraform/python-lambdas/validateOrder/prod/terragrunt.hcl"
        nodes, _edges = CodeParser(root).parse_file(unit)
        assert nodes[0].extra["terragrunt_inputs"] == {
            "handler": "validate_order.handler",
            "function_name": "validateOrder",
            "source_dir": (root / "python-lambdas/validateOrder").as_posix(),
        }

    def test_the_state_points_at_the_handler_function(self, tmp_path):
        root = tmp_path / "repo"
        store = build_repo(root)
        try:
            assert handler_edges(store, root) == [
                (state("Validate"), HANDLER_QN, "EXTRACTED", 1.0),
            ]
            # The handler is a node, so the edge's own columns say the same.
            row = store._conn.execute(
                "SELECT confidence_tier, confidence, json_extract(extra, '$.external_target') "
                "FROM edges WHERE json_extract(extra, '$.resolution') = ?", (LH,),
            ).fetchone()
            assert tuple(row) == ("EXTRACTED", 1.0, "external:aws-lambda:validateOrder")
        finally:
            store.close()

    def test_an_overridden_function_name_is_the_name_that_is_matched(self, tmp_path):
        files = dict(FILES)
        # The directory is ``auditFn``; the function is deployed as ``audit-trail``, the
        # name the ``Typed`` state's placeholder stands for.
        files["terraform/python-lambdas/auditFn/prod/terragrunt.hcl"] = lambda_unit(
            "audit.record", '  function_name = "audit-trail"\n',
        )
        files["python-lambdas/auditFn/audit.py"] = "def record(event, context):\n    return 1\n"
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            assert (
                state("Typed"), "python-lambdas/auditFn/audit.py::record", "EXTRACTED", 1.0,
            ) in handler_edges(store, root)
            names = {unit["name"] for unit in lambda_units(store)}
            assert names == {"validateOrder", "audit-trail"}
        finally:
            store.close()

    def test_a_java_handler_is_left_unlinked(self, tmp_path):
        files = dict(FILES)
        for env in ("prod", "nonprod"):
            files[f"terraform/python-lambdas/validateOrder/{env}/terragrunt.hcl"] = lambda_unit(
                "com.acme.Validate::handleRequest",
            )
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            assert handler_edges(store, root) == []
            assert lambda_units(store) == []
        finally:
            store.close()

    def test_units_that_disagree_on_the_handler_give_no_edge(self, tmp_path):
        files = dict(FILES)
        files["terraform/python-lambdas/validateOrder/nonprod/terragrunt.hcl"] = lambda_unit(
            "validate_order.helper",
        )
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            assert handler_edges(store, root) == []
            assert resolve_asl_local_targets(store)["handler_ambiguous"] == 1
        finally:
            store.close()


class TestDirectoryNameFallback:
    """The parent cannot be evaluated, so only the directory names say where the code is."""

    DYNAMIC_PARENT = LAMBDA_HCL.replace(
        '"${get_repo_root()}/${local.src_path}"', 'get_env("LAMBDA_SOURCE_DIR")',
    ).replace(
        'split("/", local.src_path)[length(split("/", local.src_path)) - 1]',
        'get_env("LAMBDA_NAME")',
    )

    def test_the_link_is_inferred_from_the_directory_above_the_unit(self, tmp_path):
        files = dict(FILES)
        files["terraform/lambda.hcl"] = self.DYNAMIC_PARENT
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            info = lambda_units(store)
            assert {(u["name"], u["static"]) for u in info} == {("validateOrder", False)}
            assert handler_edges(store, root) == [
                (state("Validate"), HANDLER_QN, "INFERRED", 0.6),
            ]
            row = store._conn.execute(
                "SELECT confidence_tier, confidence FROM edges "
                "WHERE json_extract(extra, '$.resolution') = ?", (LH,),
            ).fetchone()
            assert tuple(row) == ("INFERRED", 0.6)
        finally:
            store.close()

    def test_an_overridden_function_name_is_preferred_for_the_directory(self, tmp_path):
        files = dict(FILES)
        files["terraform/lambda.hcl"] = self.DYNAMIC_PARENT
        # The unit sits in ``wrapper``; the code is in a directory named after the function.
        files["terraform/python-lambdas/wrapper/prod/terragrunt.hcl"] = lambda_unit(
            "audit.record", '  function_name = "audit-trail"\n',
        )
        files["python-lambdas/audit-trail/audit.py"] = "def record(event, context):\n    return 1\n"
        files["python-lambdas/wrapper/audit.py"] = "def record(event, context):\n    return 2\n"
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            assert (
                state("Typed"), "python-lambdas/audit-trail/audit.py::record", "INFERRED", 0.6,
            ) in handler_edges(store, root)
        finally:
            store.close()

    def test_nothing_is_guessed_when_no_directory_matches(self, tmp_path):
        files = dict(FILES)
        files["terraform/lambda.hcl"] = self.DYNAMIC_PARENT
        files["src/validate_order.py"] = files.pop("python-lambdas/validateOrder/validate_order.py")
        root = tmp_path / "repo"
        store = build_repo(root, files)
        try:
            assert handler_edges(store, root) == []
        finally:
            store.close()


class TestUpdateEqualsFullBuild:
    def test_a_change_to_the_handler_file_alone_reruns_the_links(self, tmp_path):
        handler_file = "python-lambdas/validateOrder/validate_order.py"
        renamed = HANDLER.replace("def handler(", "def entry(")
        changed = dict(FILES)
        changed[handler_file] = renamed

        full_root = tmp_path / "full" / "repo"
        full = build_repo(full_root, changed)
        try:
            expected = derived_edges(full, full_root)
        finally:
            full.close()
        assert not any(edge[2] == LH for edge in expected)

        root = tmp_path / "inc" / "repo"
        store = build_repo(root)
        try:
            assert handler_edges(store, root)
            write_files(root, {handler_file: renamed})
            incremental_update(root, store, changed_files=[handler_file])
            assert derived_edges(store, root) == expected
            # And back: the link returns when the function does.
            write_files(root, {handler_file: HANDLER})
            incremental_update(root, store, changed_files=[handler_file])
            assert handler_edges(store, root) == [
                (state("Validate"), HANDLER_QN, "EXTRACTED", 1.0),
            ]
        finally:
            store.close()

    def test_a_change_to_the_unit_alone_gives_what_a_full_build_gives(self, tmp_path):
        unit = "terraform/python-lambdas/validateOrder/prod/terragrunt.hcl"
        changed = dict(FILES)
        changed[unit] = lambda_unit("validate_order.helper")
        changed["terraform/python-lambdas/validateOrder/nonprod/terragrunt.hcl"] = changed[unit]

        full_root = tmp_path / "full" / "repo"
        full = build_repo(full_root, changed)
        try:
            expected = derived_edges(full, full_root)
        finally:
            full.close()
        assert (state("Validate"), HELPER_QN, LH, "EXTRACTED", 1.0) in expected

        root = tmp_path / "inc" / "repo"
        store = build_repo(root)
        try:
            write_files(root, {rel: text for rel, text in changed.items() if FILES[rel] != text})
            incremental_update(root, store, changed_files=[
                unit, "terraform/python-lambdas/validateOrder/nonprod/terragrunt.hcl",
            ])
            assert derived_edges(store, root) == expected
        finally:
            store.close()
