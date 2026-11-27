"""Honest sub-reasons for the ``not_found`` status of the ``awssdk`` and ``stepfunction`` origins.

``status: not_found`` stays as it was (filters and old callers keep working). A row that has it
also gets ``not_found_reason``, worked out at query time from what each registered repo's
graph already holds (the facts :class:`~code_review_graph.stepfunction_links.Definitions`
reads), never from a new parse-time extraction:

``awssdk``
    ``test_double`` (name looks like a mock or placeholder, or the call is in a test file),
    ``no_owner_lookup`` (the service has no owner lookup at all: secretsmanager, cloudwatch ...;
    an SSM parameter or IAM role is looked up by its ``aws_ssm_parameter`` / ``aws_iam_role``),
    ``declared_name_dynamic`` (a registered repo declares resources of that Terraform type, or
    an S3 bucket wrapper module, whose name is not a plain literal, so the miss may be a name
    that cannot be evaluated), ``not_declared_in_registry`` otherwise.

``stepfunction``
    ``environment_cluster`` (an ECS target named like an environment: ``dev``, ``prodB``),
    ``declared_via_module`` (a ``module`` block is labelled with the name),
    ``data_source_only`` (only a ``data`` block of the matching type is labelled with it),
    ``source_without_unit`` (a Lambda: a directory of that name exists under a ``*lambda*``
    folder of a registered repo but no Terragrunt unit deploys it), ``not_declared_in_registry``
    otherwise. The graph does not hold a module call's ``name`` argument, so a topic a module
    declares under a literal name is ``declared_via_module`` only when the module's label is
    the name; any other module-declared name stays ``not_declared_in_registry``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .aws_terraform_wiring import DATA_STORES, DECLARED, IDENTITY_TYPES

AWSSDK_REASONS = (
    "no_owner_lookup", "declared_name_dynamic", "test_double", "not_declared_in_registry",
)
STEPFUNCTION_REASONS = (
    "data_source_only", "declared_via_module", "source_without_unit", "environment_cluster",
    "not_declared_in_registry",
)
#: Status values an origin accepts besides its real statuses (``status=data_source_only``).
REASONS_BY_ORIGIN = {"awssdk": AWSSDK_REASONS, "stepfunction": STEPFUNCTION_REASONS}

#: Terraform type -> ``(service, kind)`` for every type the awssdk join can find an owner by.
_TYPES: dict[str, tuple[str, str]] = {
    **{t: (v[0], v[1]) for t, v in DECLARED.items()},
    **{t: (v[0], v[1]) for t, v in DATA_STORES.items()},
    **{t: (v[0], v[1]) for t, v in IDENTITY_TYPES.items()},
    "aws_s3_bucket": ("s3", "bucket"),
}
#: Services that have some owner lookup (Terraform declaration or definition rules).
_LOOKUP_SERVICES = frozenset(v[0] for v in _TYPES.values()) | {"states", "ecs", "batch", "glue"}
_MOCK_NAME = re.compile(r"^(mock|fake|dummy|stub|placeholder)([-_.]|$)", re.IGNORECASE)
_ENV_NAME = re.compile(
    r"^(dev|develop|qa|stg|stage|staging|prod|preprod|uat|test|sandbox)[a-z]?[0-9]*$",
    re.IGNORECASE,
)
#: Terraform types whose declaration a wrapper module may hide, by module source words.
_MODULE_SOURCES = {"aws_s3_bucket": ("s3", "bucket")}

# Fact keys written into ``Definitions._read``'s dict (the first part never collides with a
# real service name).
RES = "~res"          # (RES, terraform type, label)  a resource block of a type of interest
DATA = "~data"        # (DATA, terraform type, ~folded label)
MODULE = "~module"    # (MODULE, "", ~folded label)
MODULE_SRC = "~modsrc"  # (MODULE_SRC, terraform type, "")  a wrapper module of that type
DIR = "~dir"          # (DIR, "lambda", ~folded directory name) under a *lambda* folder


def _fold(name: str) -> str:
    return re.sub(r"[-_]", "", name).lower()


def read_facts(store: Any, found: dict[tuple[str, str, str], str]) -> None:
    """Add the data, module, label and directory facts of one graph to *found*."""
    conn = store._conn
    for (name,) in conn.execute(
        "SELECT name FROM nodes WHERE language = 'hcl' AND kind = 'Class' "
        "AND (name LIKE 'resource.aws\\_%' ESCAPE '\\' OR name LIKE 'data.aws\\_%' ESCAPE '\\' "
        "OR name LIKE 'module.%')"
    ):
        parts = name.split(".", 2)
        if parts[0] == "module" and len(parts) >= 2:
            found.setdefault((MODULE, "", "~" + _fold(".".join(parts[1:]))), "module_block")
        elif len(parts) == 3 and parts[0] == "data":
            found.setdefault((DATA, parts[1], "~" + _fold(parts[2])), "data_block")
        elif len(parts) == 3 and parts[1] in _TYPES:
            found.setdefault((RES, parts[1], parts[2]), "resource_block")
    for (target,) in conn.execute(
        "SELECT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM' "
        "AND (file_path LIKE '%.tf' OR file_path LIKE '%.hcl') "
        "AND lower(target_qualified) LIKE '%s3%'"
    ):
        lowered = str(target).lower()
        for rtype, words in _MODULE_SOURCES.items():
            if all(word in lowered for word in words):
                found.setdefault((MODULE_SRC, rtype, ""), "wrapper_module")
    seen: set[str] = set()
    for (path,) in conn.execute(
        "SELECT file_path FROM nodes WHERE kind = 'File' AND lower(file_path) LIKE '%lambda%/%/%'"
    ):
        parts = Path(path).parts[:-1]
        for i, part in enumerate(parts):
            if i and "lambda" in "".join(parts[:i]).lower() and part not in seen:
                seen.add(part)
                found.setdefault((DIR, "lambda", "~" + _fold(part)), "directory")


def _facts(known: dict[str, dict[tuple[str, str, str], str]], repo: dict[str, Any]) -> dict:
    return known.get(str(Path(repo["path"]))) or {}


def _kind_of_stepfunction_row(row: dict[str, Any]) -> str:
    from .stepfunction_links import _DEFAULT_KIND

    return row.get("target_subkind") or _DEFAULT_KIND.get(row["target_kind"], "")


def annotate_stepfunction(
    rows: list[dict[str, Any]], known: dict[str, dict[tuple[str, str, str], str]],
    all_repos: list[dict[str, Any]],
) -> None:
    """Set ``not_found_reason`` on each ``not_found`` row of the ``stepfunction`` origin."""
    from .stepfunction_links import RESOURCE_TYPES

    facts = [_facts(known, repo) for repo in all_repos]

    def anywhere(key: tuple[str, str, str]) -> bool:
        return any(key in f for f in facts)

    for row in rows:
        if row.get("status") != "not_found":
            continue
        service, name = row["target_kind"], str(row["target_name"])
        kind = _kind_of_stepfunction_row(row)
        folded = "~" + _fold(name)
        rtype = RESOURCE_TYPES.get((service, kind))
        if service == "ecs" and _ENV_NAME.match(name):
            reason = "environment_cluster"
        elif anywhere((MODULE, "", folded)):
            reason = "declared_via_module"
        elif rtype and anywhere((DATA, rtype, folded)):
            reason = "data_source_only"
        elif service == "lambda" and anywhere((DIR, "lambda", folded)):
            reason = "source_without_unit"
        else:
            reason = "not_declared_in_registry"
        row["not_found_reason"] = reason


def annotate_awssdk(
    rows: list[dict[str, Any]], known: dict[str, dict[tuple[str, str, str], str]],
    all_repos: list[dict[str, Any]], edges_by_repo: dict[int, list[dict[str, Any]]],
    context: dict[int, list[dict[str, Any]]],
) -> None:
    """Set ``not_found_reason`` on each ``not_found`` row of the ``awssdk`` origin."""
    pending = [r for r in rows if r.get("status") == "not_found"]
    if not pending:
        return
    # Labels of Terraform declarations whose name was read as a literal, per repo and type.
    literal: dict[int, set[tuple[str, str]]] = {}
    for source in (edges_by_repo, context):
        for index, edges in source.items():
            for edge in edges:
                extra = edge.get("extra") or {}
                if (
                    edge.get("sdk") == "terraform" and edge.get("operation") == "declare"
                    and not edge.get("unresolved") and extra.get("terraform_type")
                ):
                    literal.setdefault(index, set()).add(
                        (extra["terraform_type"], str(extra.get("terraform_label"))),
                    )
    dynamic: dict[str, bool] = {}

    def has_dynamic(service: str) -> bool:
        if service not in dynamic:
            types = [t for t, (svc, _kind) in _TYPES.items() if svc == service]
            found = False
            for index, repo in enumerate(all_repos):
                facts = _facts(known, repo)
                for rtype in types:
                    if (MODULE_SRC, rtype, "") in facts:
                        found = True
                    if any(
                        key[0] == RES and key[1] == rtype
                        and (rtype, key[2]) not in literal.get(index, ())
                        for key in facts
                    ):
                        found = True
            dynamic[service] = found
        return dynamic[service]

    for row in pending:
        service, name = row["target_kind"], str(row["target_name"])
        if _MOCK_NAME.match(name) or row.get("is_test"):
            reason = "test_double"
        elif service not in _LOOKUP_SERVICES:
            reason = "no_owner_lookup"
        elif has_dynamic(service):
            reason = "declared_name_dynamic"
        else:
            reason = "not_declared_in_registry"
        row["not_found_reason"] = reason


def counts_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        reason = row.get("not_found_reason")
        if reason:
            out[reason] = out.get(reason, 0) + 1
    return out


def summary_clause(counts: dict[str, int], by_reason: dict[str, int]) -> str:
    """``; 79 not found (47 declared_name_dynamic, ...)`` or an empty string."""
    if not by_reason:
        return ""
    parts = ", ".join(
        f"{n} {reason}" for reason, n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return f"; {counts.get('not_found', sum(by_reason.values()))} not found ({parts})"
