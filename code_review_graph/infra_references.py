"""Reference-shaped strings in infrastructure files: workflow ``uses``, Docker images, sources.

The graph keeps configuration VALUES out (see ``config_keys.py`` and the value-free YAML, JSON
and Spring parsers). The one exception made here is an allowlist of strings that NAME another
unit of code: a GitHub Actions ``uses:`` reference, a Dockerfile base image, a Terragrunt
module source. Each is parsed into its parts and stored as an ``IMPORTS_FROM`` edge to a
stable external target, so a question such as "which repositories call this shared workflow"
has an answer in the graph.

Nothing else is read. ``env``, ``with``, ``secrets``, ``args``, ``ENV``, ``ARG`` values,
``LABEL`` and Terragrunt ``inputs`` are never looked at, so no value in them can reach a node
or an edge. Nor does a URL credential (``user:password@``, a token), a query string other
than a ``ref`` or ``version``, or a fragment: they are dropped from every reference and the
raw remote string is never stored. The helpers here are pure: they take a string and return
its parts, and the emitters in ``parser.py`` decide where the edges go.
"""

from __future__ import annotations

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
