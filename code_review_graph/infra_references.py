"""Reference-shaped strings in infrastructure files: workflow ``uses``, Docker images, sources.

The graph keeps configuration VALUES out (see ``config_keys.py`` and the value-free YAML, JSON
and Spring parsers). The one exception made here is an allowlist of strings that NAME another
unit of code: a GitHub Actions ``uses:`` reference, a Dockerfile base image, a Terragrunt
module source. Each is parsed into its parts and stored as an ``IMPORTS_FROM`` edge to a
stable external target, so a question such as "which repositories call this shared workflow"
has an answer in the graph.

Nothing else is read. ``env``, ``with``, ``secrets``, ``args``, ``ENV``, ``ARG`` values and
``LABEL`` are never looked at, so no value in them can reach a node or an edge. Nor does a URL
credential (``user:password@``, a token), a query string other than a ``ref`` or ``version``,
or a fragment: they are dropped from every reference and the raw remote string is never
stored. The helpers here are pure: they take a string and return its parts, and the emitters
in ``parser.py`` decide where the edges go.

Terragrunt ``inputs`` is read for the keys in :data:`TERRAGRUNT_INPUT_KEYS` and for no other:
an unlisted key is skipped without its value being visited. They are the keys that say which
Step Functions definition a unit deploys, what its ``${placeholder}`` names stand for, and
which handler a Lambda unit runs. No raw value is stored. Each is reduced to naming data
first, by the reducers at the end of this module: the name part of an ARN (never the account
id or the region), the name in a typed ``*_replacement_vars`` map, a handler string
(``module.function``), a function name, a path inside the repository. A value that is not of
that shape (an e-mail address, a subnet id, a URL, a password), an entry whose key looks like
a secret, and an expression that cannot be reduced statically all leave nothing behind.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

#: Hard cap on the references one file may add. A generated workflow or a Dockerfile with
#: thousands of stages stops here, and the file's ``infra_references_truncated`` says so.
MAX_INFRA_REFERENCES_PER_FILE = 500

#: Longest reference string kept. A longer one is not a reference, and is skipped.
MAX_REFERENCE_CHARS = 512

#: Dockerfile limits. Past either one the rest of the file is not read, and the file node's
#: ``infra_references_truncated`` says so. Real Dockerfiles are tens of lines.
MAX_DOCKERFILE_BYTES = 1_048_576
MAX_DOCKERFILE_LINES = 2_000

#: Target prefixes. A reference to code outside the repository is
#: ``<prefix><name>``; the ref, tag or digest stays in the edge's ``extra``, so every
#: version of one action or image shares one target.
EXTERNAL_GITHUB_ACTIONS_PREFIX = "external:github-actions:"
EXTERNAL_DOCKER_IMAGE_PREFIX = "external:docker-image:"

#: Target of a ``uses`` whose repository part is an expression, such as
#: ``${{ matrix.action }}``. It is never guessed, and its text is not stored.
DYNAMIC_GITHUB_ACTIONS_TARGET = EXTERNAL_GITHUB_ACTIONS_PREFIX + "dynamic"

#: Target of an image reference that uses a build argument or variable (``FROM ${BASE}``).
EXTERNAL_DYNAMIC_DOCKER_TARGET = EXTERNAL_DOCKER_IMAGE_PREFIX + "dynamic"

_OWNER_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_PATH_PART = re.compile(r"^[A-Za-z0-9_.+~-]+$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
#: A ref, tag or version: the only part of a query string that is ever kept.
_REF_VALUE = re.compile(r"^[A-Za-z0-9_.+/-]{1,255}$")
_TAG_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DOCKER_NAME = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_STAGE_ALIAS = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DIGEST_TAIL = re.compile(
    r"@([A-Za-z][A-Za-z0-9]*(?:[+._-][A-Za-z][A-Za-z0-9]*)*:[0-9A-Fa-f]{32,})$"
)
_ADDRESS_VALUE = re.compile(r"^[A-Za-z0-9_.~:/+%-]+$")


def is_reference_text(text: str) -> bool:
    """Whether *text* is short, single-line and free of control characters."""
    return (
        0 < len(text) <= MAX_REFERENCE_CHARS
        and all(ch >= " " and ch != "\x7f" for ch in text)
    )


def drop_userinfo(text: str) -> Optional[str]:
    """*text* without a leading ``userinfo@``, or ``None`` when that cannot be told safely.

    *text* starts at the authority: after ``scheme://``, or the whole of an scp-like
    ``user@host:path`` or ``user:password@registry/image``. The userinfo ends at the last ``@``
    before the first ``/``, whatever it holds (a user name, ``user:password``, a token). It is
    dropped, never stored. A ``?`` or ``#`` before that ``@`` could be the start of a query
    that contains an ``@`` or a password that contains one, so the text is unusable (``None``)
    rather than guessed at. The conventional ``git@`` user is dropped like any other.
    """
    segment = text.split("/", 1)[0]
    at = segment.rfind("@")
    if at < 0:
        return text
    if "?" in segment or "#" in segment:
        return None
    return text[at + 1:]


def is_plain_local_path(text: str) -> bool:
    """Whether *text* may be stored as a local path: no URL, getter, query or ``@``."""
    return (
        is_reference_text(text)
        and _URL_SCHEME.match(text) is None
        and _GETTER_PREFIX.match(text) is None
        and not any(ch in text for ch in "?#@")
    )


def clean_stage_alias(alias: str) -> str:
    """A Dockerfile stage alias, or ``""`` when it is not a plain identifier."""
    return alias if _STAGE_ALIAS.match(alias) else ""


def github_actions_target(owner_repo: str, path: str) -> str:
    """The external target of ``owner/repo`` with an optional in-repo ``path``."""
    return EXTERNAL_GITHUB_ACTIONS_PREFIX + (f"{owner_repo}/{path}" if path else owner_repo)


def parse_docker_image(raw: str) -> dict[str, Any]:
    """Split ``[registry/]name[:tag][@digest]`` into its parts.

    The raw text is not part of the result. Any ``userinfo@`` prefix and any ``?query`` or
    ``#fragment`` are dropped, and a reference with a ``$`` (an ``ARG`` or ``ENV``
    substitution), an unexpected character or an unusable tag is returned with ``dynamic`` set
    and no name. The name is lower-cased (Docker repository names are lower case). It is
    otherwise kept as written, so ``python`` and ``docker.io/library/python`` stay two names.
    """
    text = raw.strip()
    parts: dict[str, Any] = {
        "name": None,
        "registry": None,
        "tag": None,
        "digest": None,
        "dynamic": True,
    }
    if not is_reference_text(text) or "$" in text:
        return parts
    digest = None
    match = _DIGEST_TAIL.search(text)
    if match:
        digest = match.group(1)
        text = text[:match.start()]
    stripped = drop_userinfo(text)
    if stripped is None or any(ch in stripped for ch in "@?# \t"):
        return parts
    remainder = stripped
    # The tag is after the last ``:`` that follows the last ``/``; an earlier ``:`` is a
    # registry port (``registry.example.com:5000/base/python``).
    slash = remainder.rfind("/")
    colon = remainder.rfind(":")
    tag = None
    if colon > slash:
        tag = remainder[colon + 1:] or None
        remainder = remainder[:colon]
    if not remainder or not _DOCKER_NAME.match(remainder):
        return parts
    if tag is not None and not _TAG_VALUE.match(tag):
        return parts
    parts.update(name=remainder.lower(), tag=tag, digest=digest, dynamic=False)
    first, sep, _rest = remainder.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        parts["registry"] = first.lower()
    return parts


def docker_image_target(name: str) -> str:
    """The external target of an image *name* from :func:`parse_docker_image`."""
    return EXTERNAL_DOCKER_IMAGE_PREFIX + name


def format_docker_image(parts: dict[str, Any]) -> Optional[str]:
    """``name[:tag][@digest]`` of :func:`parse_docker_image` parts, ``None`` when dynamic."""
    if not parts["name"]:
        return None
    return parts["name"] + (f":{parts['tag']}" if parts["tag"] else "") + (
        f"@{parts['digest']}" if parts["digest"] else ""
    )


def parse_github_uses(raw: str, scope: str) -> Optional[dict[str, Any]]:
    """Parse one ``uses:`` value, or return ``None`` when it is not a reference.

    *scope* is ``"job"`` (a reusable workflow) or ``"step"`` (an action). The result has
    ``kind`` (``action``, ``reusable_workflow``, ``local`` or ``docker``) and ``dynamic``; the
    raw text of a remote reference is never kept. A remote reference adds ``owner_repo`` (lower
    case, as GitHub treats it), ``path`` (inside the repository, may be empty), ``ref`` and
    ``ref_type`` (``sha`` for a full commit hash, else ``ref``). A ``docker://`` reference
    carries the :func:`parse_docker_image` parts. A local one carries ``local_path`` when it
    is a plain relative path.

    Expressions (``${{ ... }}``) are never evaluated or stored. One in the repository part
    gives ``dynamic`` with no ``owner_repo``; one only in the ref keeps the repository and
    marks the ref dynamic. A ref with any character outside a ref's alphabet is dropped the
    same way, so a query string or credential cannot ride along in it.
    """
    text = raw.strip()
    if not is_reference_text(text):
        return None
    result: dict[str, Any] = {"dynamic": False}
    if text.startswith("docker://"):
        image = parse_docker_image(text[len("docker://"):])
        result.update(
            kind="docker",
            dynamic=image["dynamic"],
            image=image["name"],
            registry=image["registry"],
            tag=image["tag"],
            digest=image["digest"],
        )
        return result
    if text.startswith("./") or text.startswith("../"):
        if is_plain_local_path(text):
            result.update(kind="local", local_path=text)
        else:
            result.update(kind="local", dynamic=True)
        return result
    kind = "reusable_workflow" if scope == "job" else "action"
    head, at, ref = text.partition("@")
    result["kind"] = kind
    if "${{" in head:
        result["dynamic"] = True
        return result
    pieces = head.split("/")
    if len(pieces) < 2 or not all(_OWNER_REPO_PART.match(p) for p in pieces[:2]):
        return None
    if any(not p for p in pieces[2:]):
        return None
    if not all(_PATH_PART.match(p) for p in pieces[2:]):
        result["dynamic"] = True
        return result
    result["owner_repo"] = f"{pieces[0]}/{pieces[1]}".lower()
    result["path"] = "/".join(pieces[2:])
    if at and ref:
        if _REF_VALUE.match(ref):
            result["ref"] = ref
            result["ref_type"] = "sha" if _FULL_SHA.match(ref) else "ref"
        else:
            result["dynamic"] = True
            result["ref_type"] = "dynamic"
    return result


def is_github_workflow_path(parts: tuple[str, ...]) -> Optional[int]:
    """Index of the ``.github`` part when *parts* name a workflow or composite action file.

    ``.github/workflows/<name>.yml|yaml`` and ``.github/actions/**/action.yml|yaml``. *parts*
    is the path as lower-cased POSIX components. ``None`` for any other file.
    """
    if len(parts) < 3:
        return None
    name = parts[-1]
    if not name.endswith((".yml", ".yaml")):
        return None
    if parts[-3] == ".github" and parts[-2] == "workflows":
        return len(parts) - 3
    if name in ("action.yml", "action.yaml") and ".github" in parts[:-2]:
        github = parts.index(".github")
        if github + 1 < len(parts) - 1 and parts[github + 1] == "actions":
            return github
    return None


def is_dockerfile_name(name: str) -> bool:
    """Whether a file name is a Dockerfile: ``Dockerfile``, ``Dockerfile.*``, ``*.Dockerfile``.

    ``Containerfile`` (Podman and Buildah) is the same format and is matched the same way.
    """
    lower = name.lower()
    for stem in ("dockerfile", "containerfile"):
        if lower == stem or lower.startswith(stem + ".") or lower.endswith("." + stem):
            return True
    return False


#: Target of a Terragrunt source, include or dependency that is an expression, such as
#: ``find_in_parent_folders()``. It is never evaluated, and its text is not stored.
EXTERNAL_DYNAMIC_TERRAGRUNT_TARGET = "external:terragrunt:dynamic"

#: Target prefix of a remote Terraform module source (git, registry, archive).
EXTERNAL_TERRAFORM_MODULE_PREFIX = "external:terraform-module:"

#: Target of a remote Terraform ``module`` source that cannot be reduced safely, for example
#: one with an ambiguous credential. Its text is not stored.
EXTERNAL_DYNAMIC_TERRAFORM_TARGET = EXTERNAL_TERRAFORM_MODULE_PREFIX + "dynamic"

_GETTER_PREFIX = re.compile(r"^([A-Za-z0-9]+)::")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def parse_terragrunt_source(raw: str) -> dict[str, Any]:
    """Split a ``terraform { source = ... }`` value into where it points and which version.

    ``kind`` is ``local`` (a ``./``, ``../`` or absolute path), ``git`` (a ``git::`` prefix, a
    ``.git`` address or ``git@`` address), ``registry`` (``tfr://`` or ``<ns>/<name>/<provider>``),
    ``archive`` (``http(s)``, ``s3``, ``gcs``) or ``other``. A remote source has ``base``
    (``[scheme://]host/path`` without the ``//subdir``, the ``.git`` suffix, the query and
    any credentials), ``subdir`` and ``ref`` (the ``ref`` or ``version`` query parameter).
    The raw text is not part of the result. ``userinfo@`` (``user:password@``, a token, the
    ``git@`` of an scp-like address) is dropped, a fragment is dropped, and every query
    parameter other than ``ref`` and ``version`` is dropped, so a credential or signed URL
    parameter is never kept. A source that cannot be reduced safely has ``base`` ``None``.
    Nothing is fetched or evaluated.
    """
    text = raw.strip()
    parts: dict[str, Any] = {"kind": "other", "base": None, "subdir": None, "ref": None}
    if text.startswith(("./", "../", "/")):
        parts["kind"] = "local"
        return parts
    getter = None
    match = _GETTER_PREFIX.match(text)
    if match and not text.startswith(match.group(1) + "://"):
        getter = match.group(1).lower()
        text = text[match.end():]
    git_user = text.startswith("git@")
    scheme = _URL_SCHEME.match(text)
    prefix = scheme.group(0) if scheme else ""
    rest = drop_userinfo(text[scheme.end():] if scheme else text)
    if rest is None:
        return parts
    if not scheme:
        scp = re.match(r"^([^/:?#]+):(?!\d+(?:/|$))(.*)$", rest)
        if scp:  # ``host:org/repo``, the scp-like form
            rest = scp.group(1) + "/" + scp.group(2)
    rest = rest.split("#", 1)[0]
    address, _, query = rest.partition("?")
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key in ("ref", "version") and _REF_VALUE.match(value):
            parts["ref"] = value
    base, sep, subdir = address.partition("//")
    subdir = subdir.strip("/")
    dot_git = not scheme and base.endswith(".git")
    if base.endswith(".git"):
        base = base[:-4]
    base = prefix + base
    if (
        not base
        or not _ADDRESS_VALUE.match(base.replace("://", "/", 1))
        or (subdir and not _ADDRESS_VALUE.match(subdir))
    ):
        parts["ref"] = None
        return parts
    parts["base"] = base
    if sep:
        parts["subdir"] = subdir or None
    scheme_name = prefix[:-3].lower()
    if getter == "git" or git_user or dot_git:
        parts["kind"] = "git"
    elif scheme_name == "tfr" or (
        not scheme and not getter and re.fullmatch(r"\w[\w-]*/[\w.-]+/[\w.-]+", base)
    ):
        parts["kind"] = "registry"
    elif getter in ("s3", "gcs", "http", "https") or scheme_name in ("http", "https", "s3", "gcs"):
        parts["kind"] = "archive"
    elif not scheme and not getter and "." in base.split("/")[0]:
        parts["kind"] = "git"  # ``github.com/acme/mods``: go-getter's shorthand
    return parts


# -- Terragrunt ``inputs``: the allowlisted keys and their reducers ------------------------------

#: Typed replacement maps of the shared ``step_function`` module: map name ->
#: ``(service, target kind)`` of the plain names it holds.
TYPED_REPLACEMENT_VARS: dict[str, tuple[str, str]] = {
    "lambda_replacement_vars": ("lambda", "function"),
    "step_function_replacement_vars": ("states", "state-machine"),
    "sns_topic_replacement_vars": ("sns", "topic"),
}

#: The untyped map: only its entries whose value is an ARN of a known service are used.
UNTYPED_REPLACEMENT_VARS = "replacement_vars"

#: Every replacement map that is read, the untyped one first.
REPLACEMENT_VAR_MAPS: tuple[str, ...] = (UNTYPED_REPLACEMENT_VARS, *TYPED_REPLACEMENT_VARS)

#: Keys of a Lambda unit that are read: the handler, the deployed name and the source directory.
LAMBDA_INPUT_KEYS: tuple[str, ...] = ("handler", "function_name", "git_repo_sub_dir")

#: The deployed state machine name of a Step Functions unit (read only next to
#: ``step_function_file_path``).
STATE_MACHINE_NAME_KEY = "name"

#: Keys of a Lambda unit that list EventBridge rules or schedules: each item is an object of
#: which only :data:`EVENT_RULE_FIELDS` are read (never ``input``, ``description`` or a policy).
EVENT_RULE_LIST_KEYS: tuple[str, ...] = (
    "aws_cloudwatch_event_rules", "aws_cloudwatch_event_rules_ignore_changes", "schedules",
)
#: The fields of a rule or schedule item that are read.
EVENT_RULE_FIELDS: frozenset[str] = frozenset(
    {"name", "schedule_expression", "event_pattern", "is_enabled"}
)
#: Other trigger keys of a Lambda unit: names of existing SNS topics it subscribes to, and an
#: SQS queue it creates (``include_sqs`` with ``sqs_name``).
SNS_TOPICS_KEY = "existing_sns_topics"
SQS_KEYS: tuple[str, ...] = ("include_sqs", "sqs_name")

#: The only keys of a Terragrunt ``inputs`` object that are ever visited.
TERRAGRUNT_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "step_function_file_path", STATE_MACHINE_NAME_KEY, *REPLACEMENT_VAR_MAPS,
        *LAMBDA_INPUT_KEYS, *EVENT_RULE_LIST_KEYS, SNS_TOPICS_KEY, *SQS_KEYS,
    }
)

#: A unit is looked at only when it, or a file it includes, defines one of these.
TERRAGRUNT_INPUT_TRIGGER_KEYS: frozenset[str] = frozenset(
    {
        "step_function_file_path", *REPLACEMENT_VAR_MAPS, "handler", *EVENT_RULE_LIST_KEYS,
        SNS_TOPICS_KEY, "include_sqs",
    }
)

#: Most replacement entries kept for one unit.
MAX_REPLACEMENT_VARS = 500

#: ARN service -> ``(resource prefix, target kind)`` choices. The name follows the prefix.
_ARN_RESOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "lambda": (("function:", "function"),),
    "states": (("stateMachine:", "state-machine"),),
    "ecs": (("task-definition/", "task-definition"), ("cluster/", "cluster")),
    "sns": (("", "topic"),),
    "sqs": (("", "queue"),),
    "dynamodb": (("table/", "table"),),
    "batch": (("job-definition/", "job-definition"),),
    "glue": (("job/", "job"),),
    "codebuild": (("project/", "project"),),
}
_ARN_PARTITIONS = frozenset({"aws", "aws-us-gov", "aws-cn"})
_AWS_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")
_PLACEHOLDER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")
_SECRET_KEY = re.compile(
    r"secret|passw|token|credential|api[_-]?key|private[_-]?key|access[_-]?key", re.IGNORECASE,
)
_LAMBDA_HANDLER = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_/\-]{0,127}(?:\.[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}){0,8}"
    r"\.[A-Za-z_][A-Za-z0-9_]{0,127}$"
)
_LAMBDA_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,140}$")


def aws_external_target(service: str, name: str) -> str:
    """The external target of the AWS resource *name* of *service* (``external:aws-sns:x``)."""
    return f"external:aws-{service}:{name}"


def reduce_arn(value: str) -> Optional[tuple[str, str, str]]:
    """``(service, target kind, name)`` of an ARN of a known service, else ``None``.

    The account id and the region are dropped and may be anything, including a part that
    could not be evaluated. The partition, the service and the name must be literal. A
    version, alias or revision after the name is dropped.
    """
    parts = value.strip().split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[1] not in _ARN_PARTITIONS:
        return None
    service = parts[2]
    resource = parts[5]
    for prefix, kind in _ARN_RESOURCES.get(service, ()):
        if prefix and not resource.startswith(prefix):
            continue
        name = resource[len(prefix):]
        if kind in ("function", "state-machine", "task-definition", "job-definition"):
            name = name.split(":", 1)[0]
        elif kind == "table":
            name = name.split("/", 1)[0]
        if _AWS_NAME.match(name):
            return service, kind, name
        return None
    return None


def reduce_replacement_var(
    map_name: str, key: str, value: Optional[str],
) -> Optional[dict[str, str]]:
    """One entry of a replacement map as ``{key, service, kind, name}``, else ``None``.

    *key* is the ``${key}`` placeholder of the state machine definition. In the untyped map
    only an ARN of a known service is used, so a subnet id, an e-mail address, a bucket or
    any other plain value is skipped. A typed map holds plain names of its one service (an
    ARN of that service is accepted too). An entry whose key looks like a secret is skipped
    whatever its value, and the value itself is never part of the result.
    """
    if value is None or not _PLACEHOLDER_KEY.match(key) or _SECRET_KEY.search(key):
        return None
    if not is_reference_text(value.replace("\x00", "?")):
        return None
    typed = TYPED_REPLACEMENT_VARS.get(map_name)
    if value.startswith("arn:"):
        reduced = reduce_arn(value)
        if reduced is None or (typed is not None and reduced[0] != typed[0]):
            return None
        service, kind, name = reduced
    elif typed is not None and _AWS_NAME.match(value):
        service, kind = typed
        name = value
    else:
        return None
    return {"key": key, "service": service, "kind": kind, "name": name}


def reduce_lambda_handler(value: Optional[str]) -> Optional[str]:
    """A ``module.function`` handler string, or ``None`` for any other shape.

    ``pkg/module.function`` and ``pkg.module.function`` are kept as written. A Java handler
    (``com.acme.Handler::handleRequest``) is not of this shape and is not kept.
    """
    if value is None or not _LAMBDA_HANDLER.match(value):
        return None
    return value


_TRIGGER_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")
_SCHEDULE_EXPRESSION = re.compile(
    r"^(?:rate\([0-9]{1,5} [a-z]{4,7}\)|cron\([0-9A-Za-z*?/,\-#L ]{5,100}\))$"
)
_EVENT_SOURCE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
MAX_EVENT_SOURCES = 10
MAX_EVENT_RULES = 50
MAX_SNS_TOPICS = 50


def reduce_trigger_name(value: Optional[str]) -> Optional[str]:
    """A rule, topic or queue name, or ``None`` for anything that is not a plain name.

    A name that looks like a credential (:func:`value_looks_secret`) is dropped.
    """
    from .config_keys import value_looks_secret

    if value is None or not _TRIGGER_NAME.match(value) or value_looks_secret(value):
        return None
    return value


def reduce_event_rule(item: dict[str, Optional[str]]) -> Optional[dict[str, Any]]:
    """``{name, schedule_expression?, event_pattern_sources?, enabled?}`` of one rule item.

    Only a plain name, a ``rate(...)``/``cron(...)`` expression, the ``source`` strings of a JSON
    event pattern and ``is_enabled = false`` are kept. The item's ``input``, ``description`` and
    every other entry are never passed in, and a value that looks like a secret is dropped.
    """
    from .config_keys import storable_config_value

    name = reduce_trigger_name(item.get("name"))
    if name is None:
        return None
    out: dict[str, Any] = {"name": name}
    schedule = item.get("schedule_expression")
    if schedule is not None and _SCHEDULE_EXPRESSION.match(schedule) \
            and storable_config_value("schedule_expression", schedule) is not None:
        out["schedule_expression"] = schedule
    pattern = item.get("event_pattern")
    if pattern is not None and pattern.lstrip().startswith("{") and len(pattern) <= 4000:
        try:
            parsed = json.loads(pattern)
        except ValueError:
            parsed = None
        sources = parsed.get("source") if isinstance(parsed, dict) else None
        if isinstance(sources, list):
            kept = [x for x in sources if isinstance(x, str) and _EVENT_SOURCE.match(x)]
            if kept:
                out["event_pattern_sources"] = kept[:MAX_EVENT_SOURCES]
    if item.get("is_enabled") == "false":
        out["enabled"] = False
    return out


def reduce_lambda_name(value: Optional[str]) -> Optional[str]:
    """A Lambda function name, or ``None`` when *value* is not a plain name."""
    if value is None or not _LAMBDA_NAME.match(value):
        return None
    return value
