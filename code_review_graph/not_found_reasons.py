"""Honest sub-reasons for the ``not_found`` status of the ``awssdk`` and ``stepfunction`` origins.

``status: not_found`` stays as it was (filters and old callers keep working). A row that has it
also gets ``not_found_reason``, worked out at query time from what each registered repo's
graph already holds (the facts :class:`~code_review_graph.stepfunction_links.Definitions`
reads), never from a new parse-time extraction:

``awssdk``
    ``test_double`` (name looks like a mock or placeholder, or the call is in a test file),
    ``no_owner_lookup`` (the service has no owner lookup at all: ssm, sts, secretsmanager),
    ``declared_name_dynamic`` (a statement about the registry, NOT about this row's name, which
    may well be a plain literal: some registered repo declares a resource of that service's
    Terraform type, or an S3 bucket wrapper module, whose own name could not be read as a literal,
    so that unreadable declaration might be this very name; ``dynamic_declared_in`` lists the
    repos), ``not_declared_in_registry`` otherwise.
    A call in a mock, fixture or stub folder (``msw/``, ``__mocks__/``, ``mocks/``...) is a
    ``test_double`` as well.

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

from .aws_terraform_wiring import DATA_STORES, DECLARED

AWSSDK_REASONS = (
    "no_owner_lookup", "declared_name_dynamic", "test_double", "not_declared_in_registry",
)
#: ``unresolved_reason`` of an ``awssdk`` row whose name is unknown (status ``unresolved``).
#: The first group is set at build or by the config join; the second group is what
#: :func:`annotate_unresolved` adds to a row that came out with none.
UNRESOLVED_REASONS_BUILD = (
    "not_defined", "no_value", "env_placeholder", "profiles_differ", "ambiguous",
    "non_aws_host", "unrecognised_endpoint", "custom_endpoint", "proxy_endpoint",
    "serverless_cache", "internal_prefix", "no_resource_param",
)
UNRESOLVED_REASONS_QUERY = (
    "name_not_literal", "constant_not_resolved", "config_key_not_resolved", "host_not_literal",
)
UNRESOLVED_REASONS = UNRESOLVED_REASONS_BUILD + UNRESOLVED_REASONS_QUERY
#: ``reason`` of a JavaScript/TypeScript call that is ``no_route_match``.
#: ``reason`` of a Feign ``no_route_match`` row: ``served_by_caller_repo`` (the caller repo itself
#: serves a matching inbound route, so the client's ``name`` probably points at the wrong repo).
FEIGN_NO_ROUTE_REASONS = ("served_by_caller_repo",)
#: ``reason`` of a JavaScript/TypeScript ``unmatchable`` row. Feign ``unmatchable`` rows keep
#: their reason on the row but are not tallied in ``counts_by_reason`` (older callers pin that).
UNMATCHABLE_REASONS = (
    "non_literal_url", "absolute_url", "method_dynamic", "placeholder_in_route",
)
JS_NO_ROUTE_REASONS = (
    "no_servers_indexed", "no_endpoint_under_base_path", "variable_segments_unmatched",
    "route_missing_under_served_base",
)
STEPFUNCTION_REASONS = (
    "data_source_only", "declared_via_module", "source_without_unit", "environment_cluster",
    "not_declared_in_registry",
)
#: Status values an origin accepts besides its real statuses (``status=data_source_only``).
REASONS_BY_ORIGIN = {
    # ``ambiguous`` is also a status, so it is left to the status check.
    "awssdk": AWSSDK_REASONS + tuple(r for r in UNRESOLVED_REASONS if r != "ambiguous"),
    "stepfunction": STEPFUNCTION_REASONS,
}

#: Terraform type -> ``(service, kind)`` for every type the awssdk join can find an owner by.
_TYPES: dict[str, tuple[str, str]] = {
    **{t: (v[0], v[1]) for t, v in DECLARED.items()},
    **{t: (v[0], v[1]) for t, v in DATA_STORES.items()},
    "aws_s3_bucket": ("s3", "bucket"),
}
#: Services that have some owner lookup (Terraform declaration or definition rules).
_LOOKUP_SERVICES = frozenset(v[0] for v in _TYPES.values()) | {"states", "ecs", "batch", "glue"}
#: A folder that holds mock servers, fixtures or stubs: a call there is a test double.
_MOCK_PATH = re.compile(
    r"(^|/)(__mocks__|mocks?|msw|fixtures?|stubs?|fakes?)(/|$)", re.IGNORECASE,
)
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


#: Every ``reason`` a Feign/JS row can carry that ``status`` also accepts as a filter.
CROSS_REPO_ROW_REASONS = tuple(dict.fromkeys(
    FEIGN_NO_ROUTE_REASONS + JS_NO_ROUTE_REASONS + UNMATCHABLE_REASONS
))


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
    """Set ``not_found_reason`` on each ``not_found`` row and ``unresolved_reason`` on each
    ``unresolved`` row of the ``awssdk`` origin that has none."""
    annotate_unresolved(rows)
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
    dynamic: dict[str, list[str]] = {}

    def dynamic_repos(service: str) -> list[str]:
        """Repos that declare a resource of *service* whose name was not read as a literal."""
        if service not in dynamic:
            types = [t for t, (svc, _kind) in _TYPES.items() if svc == service]
            names: list[str] = []
            for index, repo in enumerate(all_repos):
                facts = _facts(known, repo)
                hit = False
                for rtype in types:
                    if (MODULE_SRC, rtype, "") in facts:
                        hit = True
                    if any(
                        key[0] == RES and key[1] == rtype
                        and (rtype, key[2]) not in literal.get(index, ())
                        for key in facts
                    ):
                        hit = True
                if hit:
                    names.append(str(repo.get("alias") or Path(str(repo["path"])).name))
            dynamic[service] = names
        return dynamic[service]

    for row in pending:
        service, name = row["target_kind"], str(row["target_name"])
        if (
            _MOCK_NAME.match(name) or row.get("is_test")
            or _MOCK_PATH.search(str(row.get("file") or ""))
        ):
            reason = "test_double"
        elif service not in _LOOKUP_SERVICES:
            reason = "no_owner_lookup"
        elif dynamic_repos(service):
            reason = "declared_name_dynamic"
            row["dynamic_declared_in"] = dynamic_repos(service)[:_MAX_DYNAMIC_REPOS]
        else:
            reason = "not_declared_in_registry"
        row["not_found_reason"] = reason


_MAX_DYNAMIC_REPOS = 5


def annotate_unresolved(rows: list[dict[str, Any]]) -> None:
    """Give each ``unresolved`` row that has no ``unresolved_reason`` one, from the row alone.

    Nothing here is a new finding: the name is unknown and the row says which kind of
    expression stands where the name should be. ``config_key_not_resolved`` (a configuration
    key or environment variable named the target but no value could be read, and the config
    join gave no finer reason), ``constant_not_resolved`` (a constant or field names it),
    ``host_not_literal`` (a connection row whose host is not spelled out) and
    ``name_not_literal`` (anything else: a variable, parameter, call result or concatenation).
    """
    for row in rows:
        if row.get("status") != "unresolved" or row.get("unresolved_reason"):
            continue
        if row.get("env_placeholder"):
            reason = "env_placeholder"
        elif row.get("placeholder"):
            reason = "config_key_not_resolved"
        elif row.get("const_ref"):
            reason = "constant_not_resolved"
        elif row.get("client") or row.get("evidence") or row.get("sdk") == "spring-config":
            reason = "host_not_literal"
        else:
            reason = "name_not_literal"
        row["unresolved_reason"] = reason


def _first_segments(index: Any) -> frozenset[str]:
    """Fixed first path segments of every server route of a ``ServerIndex`` (cached on it)."""
    cached = getattr(index, "_first_segments", None)
    if cached is None:
        found: set[str] = set()
        for shape in index.exact:
            parts = [p for p in shape.split("/") if p]
            if parts and parts[0] != "{}":
                found.add(parts[0])
        for prefix, _server in index.catch_all:
            parts = [p for p in prefix.split("/") if p]
            if parts:
                found.add(parts[0])
        cached = frozenset(found)
        try:
            index._first_segments = cached
        except AttributeError:
            pass
    return cached


def feign_caller_serves(call: Any, own_servers: Any) -> bool:
    """True when the caller's own repo has an inbound endpoint the join would match the call to.

    Uses the join's own rule (:func:`feign_links.link_call`: same method or an any-method
    endpoint, same normalised route), so a Feign client whose ``name`` points at another repo
    but whose route lives in the caller can be told apart from a route nobody serves.
    """
    from .feign_links import AMBIGUOUS, MATCHED, link_call

    return link_call(call, own_servers).status in (MATCHED, AMBIGUOUS)


def cross_repo_row_reason(
    call: Any, result: Any, own: Any, others: Any, own_servers: Any,
) -> str | None:
    """The ``reason`` for an ``unmatchable`` or ``no_route_match`` row of ``cross_repo_requests``.

    A JavaScript ``unmatchable`` row is tallied under the reason it already has; a JavaScript
    ``no_route_match`` gets one of :data:`JS_NO_ROUTE_REASONS`; a Feign ``no_route_match`` gets ``served_by_caller_repo`` when
    the caller repo itself serves the route, else no reason. Never changes the status.
    """
    from .feign_links import NO_ROUTE_MATCH, UNMATCHABLE

    if result.status == UNMATCHABLE:
        return result.reason if call.origin == "js" else None
    if result.status != NO_ROUTE_MATCH or result.reason:
        return None
    if call.origin == "js":
        return js_no_route_reason(call, [p for p in (own, others) if p is not None])
    if call.route_dynamic or not own_servers:
        return None
    return "served_by_caller_repo" if feign_caller_serves(call, own_servers) else None


def js_no_route_reason(call: Any, pools: list[Any]) -> str:
    """Why a JavaScript/TypeScript call that fit no endpoint fit none, from what the join read.

    *pools* are the server indexes searched (the caller's own repo, then every other repo).
    The order is: ``no_servers_indexed`` (no registered repo has an endpoint at all),
    ``no_endpoint_under_base_path`` (no endpoint of any repo starts with the call's first path
    segment, so the service that serves it is probably not registered or not indexed),
    ``variable_segments_unmatched`` (the route has ``{}`` pieces and no endpoint shape fits),
    ``route_missing_under_served_base`` (that first segment is served, this route is not).
    """
    from .feign_links import normalize_route

    if not any(pool.exact or pool.catch_all for pool in pools):
        return "no_servers_indexed"
    shape = normalize_route(call.route)
    parts = [p for p in shape.split("/") if p]
    first = parts[0] if parts and parts[0] != "{}" else None
    if first is not None and not any(first in _first_segments(pool) for pool in pools):
        return "no_endpoint_under_base_path"
    if "{}" in parts:
        return "variable_segments_unmatched"
    return "route_missing_under_served_base"


def counts_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Rows per ``not_found_reason`` and per ``unresolved_reason`` (the two sets of values
    never overlap)."""
    out: dict[str, int] = {}
    for row in rows:
        reason = row.get("not_found_reason") or (
            row.get("unresolved_reason") if row.get("status") == "unresolved" else None
        )
        if reason:
            out[reason] = out.get(reason, 0) + 1
    return out


def summary_clause(counts: dict[str, int], by_reason: dict[str, int]) -> str:
    """``; 79 not found (47 declared_name_dynamic, ...)`` and the same for ``unresolved``, or an
    empty string."""
    out = ""
    for label, status, wanted in (
        ("not found", "not_found", None), ("unresolved", "unresolved", set(UNRESOLVED_REASONS)),
    ):
        picked = {
            r: n for r, n in by_reason.items()
            if (r in wanted if wanted is not None else r not in set(UNRESOLVED_REASONS))
        }
        if not picked:
            continue
        parts = ", ".join(
            f"{n} {reason}"
            for reason, n in sorted(picked.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        out += f"; {counts.get(status, sum(picked.values()))} {label} ({parts})"
    return out
