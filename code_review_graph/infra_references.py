"""Reference-shaped strings in infrastructure files: workflow ``uses``, Docker images, sources.

The graph keeps configuration VALUES out (see ``config_keys.py`` and the value-free YAML, JSON
and Spring parsers). The one exception made here is an allowlist of strings that NAME another
unit of code: a GitHub Actions ``uses:`` reference, a Dockerfile base image, a Terragrunt
module source. Each is parsed into its parts and stored as an ``IMPORTS_FROM`` edge to a
stable external target, so a question such as "which repositories call this shared workflow"
has an answer in the graph.

Nothing else is read. ``env``, ``with``, ``secrets``, ``args``, ``ENV``, ``ARG`` values,
``LABEL`` and Terragrunt ``inputs`` are never looked at, so no value in them can reach a node
or an edge. The helpers here are pure: they take a string and return its parts, and the
emitters in ``parser.py`` decide where the edges go.
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
#: ``${{ matrix.action }}``. The raw text is on the edge; it is never guessed.
DYNAMIC_GITHUB_ACTIONS_TARGET = EXTERNAL_GITHUB_ACTIONS_PREFIX + "dynamic"

#: Target of an image reference that uses a build argument or variable (``FROM ${BASE}``).
EXTERNAL_DYNAMIC_DOCKER_TARGET = EXTERNAL_DOCKER_IMAGE_PREFIX + "dynamic"

_OWNER_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def is_reference_text(text: str) -> bool:
    """Whether *text* is short, single-line and free of control characters."""
    return (
        0 < len(text) <= MAX_REFERENCE_CHARS
        and all(ch >= " " and ch != "\x7f" for ch in text)
    )


def github_actions_target(owner_repo: str, path: str) -> str:
    """The external target of ``owner/repo`` with an optional in-repo ``path``."""
    return EXTERNAL_GITHUB_ACTIONS_PREFIX + (f"{owner_repo}/{path}" if path else owner_repo)


def parse_docker_image(raw: str) -> dict[str, Any]:
    """Split ``[registry/]name[:tag][@digest]`` into its parts.

    A reference with a ``$`` is an ``ARG`` or ``ENV`` substitution and is returned with
    ``dynamic`` set and no name: the build argument's value is not read, so the image is not
    known. The name is lower-cased (Docker repository names are lower case). It is otherwise
    kept as written, so ``python`` and ``docker.io/library/python`` stay two names.
    """
    text = raw.strip()
    parts: dict[str, Any] = {
        "raw": text,
        "name": None,
        "registry": None,
        "tag": None,
        "digest": None,
        "dynamic": False,
    }
    if not is_reference_text(text):
        parts["dynamic"] = True
        return parts
    if "$" in text:
        parts["dynamic"] = True
        return parts
    remainder, _, digest = text.partition("@")
    if digest:
        parts["digest"] = digest
    # The tag is after the last ``:`` that follows the last ``/``; an earlier ``:`` is a
    # registry port (``registry.example.com:5000/base/python``).
    slash = remainder.rfind("/")
    colon = remainder.rfind(":")
    if colon > slash:
        parts["tag"] = remainder[colon + 1:] or None
        remainder = remainder[:colon]
    if not remainder:
        parts["dynamic"] = True
        return parts
    parts["name"] = remainder.lower()
    first, sep, _rest = remainder.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        parts["registry"] = first.lower()
    return parts


def docker_image_target(name: str) -> str:
    """The external target of an image *name* from :func:`parse_docker_image`."""
    return EXTERNAL_DOCKER_IMAGE_PREFIX + name


def parse_github_uses(raw: str, scope: str) -> Optional[dict[str, Any]]:
    """Parse one ``uses:`` value, or return ``None`` when it is not a reference.

    *scope* is ``"job"`` (a reusable workflow) or ``"step"`` (an action). The result has
    ``kind`` (``action``, ``reusable_workflow``, ``local`` or ``docker``), ``dynamic`` and the
    raw text. A remote reference adds ``owner_repo`` (lower case, as GitHub treats it),
    ``path`` (inside the repository, may be empty), ``ref`` and ``ref_type`` (``sha`` for a
    full commit hash, else ``ref``). A ``docker://`` reference carries the
    :func:`parse_docker_image` parts. A local one carries ``local_path``.

    Expressions (``${{ ... }}``) are never evaluated. One in the repository part gives
    ``dynamic`` with no ``owner_repo``; one only in the ref keeps the repository and marks the
    ref dynamic.
    """
    text = raw.strip()
    if not is_reference_text(text):
        return None
    result: dict[str, Any] = {"raw": text, "dynamic": False}
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
        result.update(kind="local", local_path=text)
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
    result["owner_repo"] = f"{pieces[0]}/{pieces[1]}".lower()
    result["path"] = "/".join(pieces[2:])
    if at and ref:
        result["ref"] = ref
        if "${{" in ref:
            result["dynamic"] = True
            result["ref_type"] = "dynamic"
        else:
            result["ref_type"] = "sha" if _FULL_SHA.match(ref) else "ref"
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
#: ``find_in_parent_folders()``. The raw text is on the edge; it is never evaluated.
EXTERNAL_DYNAMIC_TERRAGRUNT_TARGET = "external:terragrunt:dynamic"

#: Target prefix of a remote Terraform module source (git, registry, archive).
EXTERNAL_TERRAFORM_MODULE_PREFIX = "external:terraform-module:"

_GETTER_PREFIX = re.compile(r"^([A-Za-z0-9]+)::")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def parse_terragrunt_source(raw: str) -> dict[str, Any]:
    """Split a ``terraform { source = ... }`` value into where it points and which version.

    ``kind`` is ``local`` (a ``./``, ``../`` or absolute path), ``git`` (a ``git::`` prefix, a
    ``.git`` URL or ``git@`` address), ``registry`` (``tfr://`` or ``<ns>/<name>/<provider>``),
    ``archive`` (``http(s)``, ``s3``, ``gcs``) or ``other``. A remote source has ``base``
    (the address without the ``//subdir`` and without the query), ``subdir`` and ``ref``
    (the ``ref`` or ``version`` query parameter). Nothing is fetched or evaluated.
    """
    text = raw.strip()
    parts: dict[str, Any] = {"raw": text, "kind": "other", "base": None, "subdir": None,
                             "ref": None}
    if text.startswith(("./", "../", "/")):
        parts["kind"] = "local"
        return parts
    getter = None
    match = _GETTER_PREFIX.match(text)
    if match and not text.startswith(match.group(1) + "://"):
        getter = match.group(1).lower()
        text = text[match.end():]
    address, _, query = text.partition("?")
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key in ("ref", "version") and value:
            parts["ref"] = value
    scheme = _URL_SCHEME.match(address)
    head = address[scheme.end():] if scheme else address
    base, sep, subdir = head.partition("//")
    if sep:
        parts["subdir"] = subdir.strip("/") or None
    base = (address[:scheme.end()] if scheme else "") + base
    if base.endswith(".git"):
        base = base[:-4]
    parts["base"] = base or None
    scheme_name = scheme.group(0)[:-3].lower() if scheme else ""
    if getter == "git" or head.startswith("git@") or address.split("//")[0].endswith(".git"):
        parts["kind"] = "git"
    elif scheme_name == "tfr" or (
        not scheme and not getter and re.fullmatch(r"\w[\w-]*/[\w.-]+/[\w.-]+", base or "")
    ):
        parts["kind"] = "registry"
    elif getter in ("s3", "gcs", "http", "https") or scheme_name in ("http", "https", "s3", "gcs"):
        parts["kind"] = "archive"
    elif not scheme and not getter and base and "." in base.split("/")[0]:
        parts["kind"] = "git"  # ``github.com/acme/mods``: go-getter's shorthand
    return parts
