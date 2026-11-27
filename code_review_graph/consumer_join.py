"""Reverse lookup at query time: who in the registry uses a named AWS resource.

``cross_repo_requests`` with ``origin="consumers"`` takes a name (a queue, bucket, SSM
parameter, table, topic, secret ...) and lists the Terraform declarations of it and every repo
that references it. It joins by name equality over what the graphs already hold:

* declarations: the Terraform ``declare`` edges (``aws_sqs_queue``, ``aws_sns_topic``,
  ``aws_s3_bucket``, ``aws_dynamodb_table``, ``aws_lambda_function`` ...), plus
  ``aws_ssm_parameter`` and ``aws_secretsmanager_secret`` blocks, whose ``name`` is read from
  the block's own lines in the repo (the graph keeps the block, not its attributes). A name
  with ``${...}`` interpolation is kept as a template and matches a name that fits it;
* consumers: the AWS SDK edges of every repo (``aws_sdk_links.read_edges``): a name written in
  code (``exact``), a name a Spring property resolves to in a profile (``exact``, ``via``
  ``resolved_name``), an SSM ``GetParametersByPath`` read whose path covers the name
  (``prefix``), a read that names the resource through a property or environment variable
  called as asked (``config_key``), and Terraform wiring of another repo (event source
  mappings, subscriptions, bucket notifications).

Not joined, because the graphs hold no value for them: environment variable and task
definition values, and string literals other than the AWS call targets above. A repo that only
passes a name through such a value shows up only when its code names the resource, or its
Spring configuration resolves to it. Names are matched case-sensitively; an SSM name matches
with or without its leading ``/``.

False positives: a short or generic name (``logs``, ``test``, ``events``) equals unrelated
resources of several repos, so a name shorter than :data:`MIN_NAME_LENGTH` characters or one of
:data:`GENERIC_NAMES` is refused, and a ``GetParametersByPath`` path that short or generic is not
used for prefix matching. A qualified name (``orders-prod-queue``, ``/prod/app/db``) is safe.
Equal names in different AWS accounts or regions are not told apart.

The no-name mode lists the SSM reads that fit no declared parameter of the registry, so a
parameter created by hand, by CloudFormation or by a Terraform module with a computed name
shows up there: "undeclared in registered Terraform" is not "missing in AWS".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from . import aws_sdk_links as al
from .config_keys import normalize_spring_config_key
from .graph import GraphStore

MIN_NAME_LENGTH = 4
#: Single-word names that name something in many unrelated repos.
GENERIC_NAMES = frozenset({
    "test", "tests", "prod", "production", "dev", "development", "stage", "staging", "qa",
    "data", "queue", "queues", "bucket", "buckets", "table", "tables", "topic", "topics",
    "secret", "secrets", "config", "configs", "default", "main", "app", "api", "logs", "log",
    "events", "event", "jobs", "job", "input", "inputs", "output", "outputs", "temp", "tmp",
    "backup", "backups", "archive", "files", "file", "results", "messages", "param", "params",
    "parameter", "parameters", "password", "token", "key", "keys", "env", "global", "common",
})
MATCH_EXACT = "exact"
MATCH_PREFIX = "prefix"
MATCH_CONFIG_KEY = "config_key"
MATCHES = (MATCH_EXACT, MATCH_PREFIX, MATCH_CONFIG_KEY)
ROLE_DECLARATION = "declaration"
ROLE_CONSUMER = "consumer"
ROLE_UNDECLARED = "undeclared_read"
ROLES = (ROLE_DECLARATION, ROLE_CONSUMER)

_PATH_READ = "get_parameters_by_path"
#: Access modes (``aws_sdk_calls.classify_access``) that change or remove what they name.
_NOT_READS = frozenset({"write", "delete", "admin"})
#: ``terraform type -> (service, kind)`` of the blocks whose ``name`` is read from the file.
FILE_DECLARED = {
    "aws_ssm_parameter": ("ssm", "parameter"),
    "aws_secretsmanager_secret": ("secretsmanager", "secret"),
}
_MAX_SPAN_LINES = 200
_MAX_FILE_BYTES = 1_000_000
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_NAME_ATTR = re.compile(r'^\s*name\s*=\s*"((?:[^"\\\n]|\\.)*)"\s*(?:#.*|//.*)?$')
_INTERP = re.compile(r"\$\{[^}]*\}")


def refusal(name: str) -> Optional[str]:
    """Why *name* is too short or too generic to join on, or ``None`` when it is fine."""
    bare = name.strip().strip("/")
    if len(bare) < MIN_NAME_LENGTH:
        return "name_too_short"
    if bare.lower() in GENERIC_NAMES:
        return "name_generic"
    return None


def normal(service: str, name: str) -> str:
    """The comparison form of a name: an SSM parameter or secret loses its leading ``/``."""
    return name.lstrip("/") if service in ("ssm", "secretsmanager") else name


def _relative(file_path: str, repo_path: Path) -> str:
    try:
        return Path(file_path).relative_to(repo_path).as_posix()
    except ValueError:
        return file_path


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _block_name(repo_path: Path, file: str, start: int, end: int) -> Optional[str]:
    """The ``name`` attribute of the Terraform block on lines *start*-*end*, or ``None``.

    Only an attribute at the top level of the block counts, so ``name`` inside ``tags`` does
    not. The text is read from a file inside the repo, at most 200 lines of it.
    """
    try:
        root = repo_path.resolve()
        path = (root / file).resolve()
        if root not in path.parents or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None
    depth = 0
    for line in lines[max(start - 1, 0):min(max(end, start), start + _MAX_SPAN_LINES)]:
        if depth == 1:
            found = _NAME_ATTR.match(line)
            if found:
                return found.group(1)
        stripped = _STRING.sub('""', line)
        stripped = re.sub(r"(?:#|//).*$", "", stripped)
        depth += stripped.count("{") + stripped.count("[") - stripped.count("}") \
            - stripped.count("]")
    return None


def template_pattern(name: str) -> Optional[re.Pattern[str]]:
    """A pattern for a declared name with ``${...}`` parts, or ``None`` when it has none.

    Each interpolation stands for one path segment. A template with fewer than
    :data:`MIN_NAME_LENGTH` fixed characters would fit nearly any name and is not used.
    """
    if "${" not in name:
        return None
    fixed = _INTERP.sub("", name).strip("/")
    if len(fixed) < MIN_NAME_LENGTH:
        return None
    parts = _INTERP.split(name.lstrip("/"))
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


def read_declarations(store: GraphStore, repo_path: Path) -> dict[str, Any]:
    """The declarations of one graph: ``{"items": [...], "unnamed": n}``.

    Each item has ``service``, ``kind``, ``name``, ``file``, ``line``, ``terraform_type`` and
    ``template`` (true when the name holds an interpolation). ``unnamed`` counts SSM and
    secret blocks whose name is not a string literal (a variable or local).
    """
    items: list[dict[str, Any]] = []
    for edge in al.read_declare_edges(store):
        items.append({
            "service": edge["service"], "kind": edge["kind"], "name": edge["name"],
            "file": _relative(edge["file_path"], repo_path), "line": edge["line"],
            "terraform_type": edge["extra"].get("terraform_type", ""), "template": False,
        })
    unnamed = 0
    # An SSM parameter or secret block can already be a ``declare`` edge (the Terraform name
    # evaluator writes one for it, including a name built from locals or inputs). Read the block
    # from its file only when no edge covers it, so one resource is never listed twice.
    covered = {(item["file"], item["line"], item["terraform_type"]) for item in items}
    rows = store._conn.execute(
        "SELECT name, file_path, line_start, line_end FROM nodes "
        "WHERE kind = 'Class' AND language = 'hcl' AND (name LIKE ? OR name LIKE ?) "
        "ORDER BY file_path, line_start",
        tuple(f"resource.{rtype}.%" for rtype in FILE_DECLARED),
    ).fetchall()
    for node_name, file_path, start, end in rows:
        rtype = node_name.split(".", 2)[1]
        service, kind = FILE_DECLARED[rtype]
        file = _relative(file_path, repo_path)
        if (file, start, rtype) in covered:
            continue
        name = _block_name(repo_path, file, start or 1, end or start or 1)
        if not name:
            unnamed += 1
            continue
        template = "${" in name
        if template and template_pattern(name) is None:
            unnamed += 1
            continue
        items.append({
            "service": service, "kind": kind, "name": name, "file": file, "line": start,
            "terraform_type": rtype, "template": template,
        })
    return {"items": items, "unnamed": unnamed}


def read_uses(store: GraphStore, repo_path: Path) -> list[dict[str, Any]]:
    """The AWS SDK and Terraform wiring edges of one graph that can name a resource."""
    out: list[dict[str, Any]] = []
    for edge in al.read_edges(store):
        if edge["sdk"] == "terraform" and edge["operation"] == "declare":
            continue
        extra = edge["extra"]
        resolved = extra.get("resolved_names")
        names: dict[str, str] = {}
        if isinstance(resolved, dict):
            names = {str(p)[:64]: v for p, v in resolved.items() if isinstance(v, str)}
        out.append({
            "service": edge["service"], "kind": edge["kind"], "name": edge["name"],
            "operation": edge["operation"], "access": edge["access"], "sdk": edge["sdk"],
            "unresolved": bool(edge["unresolved"]) or edge["name"] == "?",
            "placeholder": edge["placeholder"], "resolved": names,
            "file": _relative(edge["file_path"], repo_path), "line": edge["line"],
            "source": str(edge["source"]).split("::", 1)[-1] if edge["source"] else "",
        })
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def declaration_matches(decl: dict[str, Any], name: str) -> bool:
    """Whether the declared name (or template) equals *name*."""
    want = normal(decl["service"], name)
    have = normal(decl["service"], decl["name"])
    if not decl["template"]:
        return have == want
    pattern = template_pattern(decl["name"])
    return bool(pattern and pattern.match(want))


def covers(path: str, name: str) -> bool:
    """Whether a ``GetParametersByPath`` *path* is above the parameter *name* (any depth)."""
    base = path.strip("/")
    return bool(base) and normal("ssm", name).startswith(base + "/")


def usable_path(path: str) -> bool:
    """A path prefix long and specific enough to join on (see the module docstring)."""
    last = path.strip("/").rsplit("/", 1)[-1]
    return len(path.strip("/")) >= MIN_NAME_LENGTH and last.lower() not in GENERIC_NAMES


def use_matches(use: dict[str, Any], name: str) -> Optional[tuple[str, dict[str, Any]]]:
    """``(match, extra)`` when the edge *use* refers to *name*, else ``None``."""
    service = use["service"]
    want = normal(service, name)
    if not use["unresolved"] and use["name"] != "?":
        have = normal(service, use["name"])
        if service == "ssm" and use["operation"] == _PATH_READ:
            if have == want:
                return MATCH_EXACT, {}
            if usable_path(use["name"]) and covers(use["name"], name):
                return MATCH_PREFIX, {"path": use["name"]}
        elif have == want:
            return MATCH_EXACT, {}
    profiles = sorted(p for p, v in use["resolved"].items() if normal(service, v) == want)
    if profiles:
        return MATCH_EXACT, {"via": "resolved_name", "profiles": profiles[:5]}
    if service == "ssm" and use["operation"] == _PATH_READ:
        for profile, value in use["resolved"].items():
            if usable_path(value) and covers(value, name):
                return MATCH_PREFIX, {"via": "resolved_name", "profiles": [profile],
                                      "path": value}
    key = use["placeholder"]
    if isinstance(key, str) and key and (
        key == name or normalize_spring_config_key(key) == normalize_spring_config_key(name)
    ):
        return MATCH_CONFIG_KEY, {"placeholder": key}
    return None


def undeclared_reads(
    uses: list[dict[str, Any]], declared: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """The SSM reads of one repo that fit no declared parameter, and how many had no name.

    A ``GetParametersByPath`` read is declared when some declared parameter is under its path.
    """
    ssm = [d for d in declared if d["service"] == "ssm"]
    out: list[dict[str, Any]] = []
    unnamed = 0
    for use in uses:
        if use["service"] != "ssm" or use["sdk"] == "terraform":
            continue
        # A put or a delete of a parameter is not a read of one; the rows are labelled reads.
        if use["access"] in _NOT_READS:
            continue
        names: list[tuple[str, dict[str, Any]]] = (
            [(use["name"], {})] if not use["unresolved"] and use["name"] != "?" else []
        )
        names += [(v, {"via": "resolved_name", "profile": p})
                  for p, v in sorted(use["resolved"].items())]
        if not names:
            unnamed += 1
            continue
        for name, extra in names:
            is_path = use["operation"] == _PATH_READ
            if is_path:
                hit = any(covers(name, d["name"]) for d in ssm)
            else:
                hit = any(declaration_matches(d, name) for d in ssm)
            if not hit:
                out.append({**use, "name": name, **extra, "is_path": is_path})
    return out, unnamed
