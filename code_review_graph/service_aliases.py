"""Service-name aliases and per-repo declared identities, for the cross-repo joins.

A Feign client names a service (``@FeignClient(name = "billing-api-accounts")``), and the
repository that answers is often registered under another name. This module lets the
``cross_repo_requests`` join reach it, at query time (nothing here is stored or built):

1. **Explicit map** (highest priority). An optional JSON or YAML file whose path is in
   ``CRG_SERVICE_ALIASES_FILE``::

       {"shared-catalog": "catalog-service", "prefix:billing-api-": "billing-api"}

   A plain key is a service name (exact, or equal once case and ``-``/``_`` are folded); a
   ``prefix:`` key is a prefix rule (longest prefix wins). The value is a registered repo
   alias or folder name, looked up like every other repo name (no chains). A top-level
   ``{"aliases": {...}}`` wrapper is accepted too. A broken file never raises: it is skipped
   with a warning, entry by entry.
2. **Inference** from what a repo itself declares: its Spring ``spring.application.name``,
   the ECS service names, ECS task-definition families and ECR repository names its own
   Terraform declares (only a repo that declares at most :data:`MAX_OWN_DECLARATIONS` of
   them; a repo with more is an infrastructure repo, whose names belong to other repos), and
   its registry alias and folder name. Names are compared after :func:`normalize_identity`
   (case, ``-``/``_`` and one trailing environment suffix such as ``-dev``, ``-qa``,
   ``-prod`` ignored). Only equality of the normalised name matches; a name that reaches
   several repos is ``ambiguous`` with the candidates, never a guess.

``matched_via`` is one of :data:`VIA_ORDER`. The facts a repo declares (:class:`RepoFacts`) are
read from its finished graph and, for the Spring name and the Terraform attributes, from the
files named by the graph's nodes, read-only and bounded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import feign_links as fl
from .aws_config_names import _Values
from .aws_terraform_wiring import _literal, top_attrs
from .errors import GraphStoreError
from .graph import GraphStore

try:  # PyYAML is a hard dependency of the package; guard anyway
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "ALIASES_FILE_ENV", "AliasRules", "IdentityIndex", "RepoFacts", "FactsCache",
    "VIA_ORDER", "load_alias_rules", "normalize_identity", "resolve_service",
]

#: Environment variable holding the path of the optional alias file.
ALIASES_FILE_ENV = "CRG_SERVICE_ALIASES_FILE"

EXPLICIT_ALIAS = "explicit_alias"
SPRING_APPLICATION_NAME = "spring_application_name"
ECS_SERVICE = "ecs_service"
ECR_REPO = "ecr_repo"
REPO_NAME = "repo_name"
#: Every ``matched_via`` value, strongest first (also the tie-break inside one repo).
VIA_ORDER: tuple[str, ...] = (
    EXPLICIT_ALIAS, SPRING_APPLICATION_NAME, ECS_SERVICE, ECR_REPO, REPO_NAME,
)

#: Hard caps: the alias file is hand-maintained, so these only bound the work.
MAX_RULES = 500
_MAX_FILE_BYTES = 1_000_000
_MAX_CONFIG_BYTES = 2_000_000
#: A repo declaring more ECS/ECR names than this is an infrastructure repo.
MAX_OWN_DECLARATIONS = 3
_MAX_DIRS_PER_REPO = 5000

#: Environment suffixes :func:`normalize_identity` strips (one, at the end).
ENV_SUFFIXES = ("dev", "qa", "prod", "stage", "staging", "test", "uat")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_PLACEHOLDER = re.compile(r"\$\{([^${}]*)\}")
_SEPARATORS = re.compile(r"[-_]+")
_PREFIX_KEY = "prefix:"
_SPRING_NAME_KEY = "spring.application.name"
#: Directory names that say nothing about a service.
_GENERIC_DIRS = frozenset({
    "modules", "module", "src", "terraform", "terragrunt", "live", "infra", "infrastructure",
    "stacks", "stack", "main", "common", "shared", "global", "env", "envs", "environments",
    ".terragrunt-cache", ".terraform",
})
_BLOCK_TYPES = {
    "aws_ecr_repository": ("ecr", "name"),
    "aws_ecs_service": ("ecs_service", "name"),
    "aws_ecs_task_definition": ("ecs_task_definition", "family"),
}


def normalize_identity(name: str) -> str:
    """Fold a service identity for equality: case, ``-``/``_`` runs, one env suffix.

    ``Orders_API-prod`` and ``orders-api`` give the same result. A name that is nothing but an
    environment suffix keeps it (``dev`` stays ``dev``). Nothing else is removed: ``orders``
    never equals ``orders-api``.
    """
    folded = _SEPARATORS.sub("-", name.strip().lower()).strip("-")
    for suffix in ENV_SUFFIXES:
        tail = "-" + suffix
        if folded.endswith(tail) and len(folded) > len(tail):
            return folded[: -len(tail)]
    return folded


# -- The explicit alias file ---------------------------------------------------------------------


@dataclass
class AliasRules:
    """The parsed alias file: exact names and prefix rules, both -> a repo name."""

    exact: dict[str, str] = field(default_factory=dict)
    #: ``(folded prefix, target)``, longest prefix first.
    prefixes: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.exact or self.prefixes)

    def target_for(self, service: str) -> Optional[str]:
        """The repo name an explicit rule maps *service* to, or ``None``."""
        if service in self.exact:
            return self.exact[service]
        folded = fl.normalize_service_name(service)
        for key, target in self.exact.items():
            if fl.normalize_service_name(key) == folded:
                return target
        for prefix, target in self.prefixes:
            if folded.startswith(prefix):
                return target
        return None


def _read_mapping(path: Path) -> Optional[dict[str, Any]]:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            logger.warning("%s is larger than %d bytes - no service aliases loaded",
                           path, _MAX_FILE_BYTES)
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Cannot read %s: %s - no service aliases loaded", path, exc)
        return None
    data: Any = None
    if path.suffix.lower() != ".json" and _yaml is not None:
        try:
            data = _yaml.safe_load(text)
        except (_yaml.YAMLError, RecursionError, ValueError) as exc:
            logger.warning("Malformed YAML in %s: %s - no service aliases loaded", path, exc)
            return None
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            logger.warning("Malformed JSON in %s: %s - no service aliases loaded", path, exc)
            return None
    if isinstance(data, dict) and set(data) == {"aliases"} and isinstance(data["aliases"], dict):
        data = data["aliases"]
    if not isinstance(data, dict):
        logger.warning("%s must hold one JSON/YAML mapping - no service aliases loaded", path)
        return None
    return data


def load_alias_rules(path: Optional[str] = None) -> AliasRules:
    """Load ``CRG_SERVICE_ALIASES_FILE`` (or *path*); always returns, never raises.

    Invalid entries are skipped with a warning, and only the first :data:`MAX_RULES` entries
    are looked at.
    """
    raw_path = path if path is not None else os.environ.get(ALIASES_FILE_ENV, "").strip()
    rules = AliasRules()
    if not raw_path:
        return rules
    file = Path(raw_path).expanduser()
    data = _read_mapping(file)
    if data is None:
        return rules
    for seen, (key, target) in enumerate(data.items()):
        if seen >= MAX_RULES:
            logger.warning("%s defines more than %d service aliases - ignoring the rest",
                           file, MAX_RULES)
            break
        if not isinstance(key, str) or not isinstance(target, str) or not target.strip():
            logger.warning("%s: service alias %r needs a string name and a non-empty string "
                           "target - skipping", file, key)
            continue
        name = key.strip()
        target = target.strip()
        if name.startswith(_PREFIX_KEY):
            prefix = name[len(_PREFIX_KEY):].strip()
            if not _NAME_RE.match(prefix):
                logger.warning("%s: invalid alias prefix %r - skipping", file, key)
                continue
            rules.prefixes.append((fl.normalize_service_name(prefix), target))
        elif _NAME_RE.match(name):
            rules.exact[name] = target
        else:
            logger.warning("%s: invalid service alias name %r - skipping", file, key)
    rules.prefixes.sort(key=lambda item: -len(item[0]))
    return rules


# -- What a repo declares about itself -----------------------------------------------------------


@dataclass(frozen=True)
class Decl:
    """One declared name with where it is declared (file relative to the repo)."""

    name: str
    file: str
    line: int


@dataclass
class RepoFacts:
    """The service-identity facts one repo declares."""

    spring: list[str] = field(default_factory=list)
    ecr: list[Decl] = field(default_factory=list)
    ecs_services: list[Decl] = field(default_factory=list)
    task_families: list[Decl] = field(default_factory=list)
    #: ``normalised dir name -> (raw name, repo-relative dir, stack kind)``, the kind being
    #: ``terragrunt_stack`` or ``terraform_module``.
    dirs: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    readable: bool = True

    def own_declaration_count(self) -> int:
        return len({normalize_identity(d.name) for d in
                    (*self.ecr, *self.ecs_services, *self.task_families)})


def _spring_names(store: GraphStore) -> list[str]:
    """Distinct ``spring.application.name`` values of a repo's main configuration files."""
    files = [
        row[0] for row in store._conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'ConfigProperty' AND name = ?",
            (_SPRING_NAME_KEY,),
        ).fetchall()
        if "/src/test/" not in row[0] and "/test/resources/" not in row[0]
    ]
    values = _Values({_SPRING_NAME_KEY})
    found: list[str] = []
    for file_path in sorted(files):
        for kv in values.read(file_path).values():
            name = _clean_spring_value(kv.get(_SPRING_NAME_KEY))
            if name and name not in found:
                found.append(name)
    return found[:5]


def _clean_spring_value(raw: Any) -> Optional[str]:
    """The literal (or ``${VAR:default}`` default) of a Spring name value, else ``None``."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().strip("'\"")
    for _ in range(4):
        found = _PLACEHOLDER.search(value)
        if found is None:
            break
        _variable, _, default = found.group(1).partition(":")
        if not default.strip():
            return None
        value = value[:found.start()] + default.strip() + value[found.end():]
    return value if _NAME_RE.match(value) else None


def _read_lines(path: Path, cache: dict[str, list[str]]) -> list[str]:
    key = str(path)
    if key not in cache:
        try:
            if path.stat().st_size > _MAX_CONFIG_BYTES:
                cache[key] = []
            else:
                cache[key] = path.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            cache[key] = []
    return cache[key]


def _relative(file_path: str, repo_path: Path) -> str:
    try:
        return str(Path(file_path).relative_to(repo_path))
    except ValueError:
        return file_path


def _terraform_decls(store: GraphStore, repo_path: Path, facts: RepoFacts) -> None:
    """ECR repositories, ECS services and task-definition families, by literal attribute."""
    cache: dict[str, list[str]] = {}
    for rtype, (kind, attr) in _BLOCK_TYPES.items():
        like = f"resource.{rtype}.%".replace("_", "\\_")
        rows = store._conn.execute(
            "SELECT file_path, line_start, line_end FROM nodes WHERE language = 'hcl' "
            "AND kind = 'Class' AND name LIKE ? ESCAPE '\\' ORDER BY file_path, line_start",
            (like,),
        ).fetchall()
        for file_path, start, end in rows:
            if not start or not end:
                continue
            lines = _read_lines(Path(file_path), cache)
            name = _literal(top_attrs("\n".join(lines[start - 1:end])).get(attr))
            if not name or not _NAME_RE.match(name):
                continue
            decl = Decl(name, _relative(file_path, repo_path), int(start))
            target = {
                "ecr": facts.ecr, "ecs_service": facts.ecs_services,
                "ecs_task_definition": facts.task_families,
            }[kind]
            if decl not in target:
                target.append(decl)


def _stack_dirs(store: GraphStore, repo_path: Path, facts: RepoFacts) -> None:
    """Directory names of the Terraform/Terragrunt files of a repo (stack and module dirs)."""
    rows = store._conn.execute(
        "SELECT file_path FROM nodes WHERE kind = 'File' AND language = 'hcl' "
        "ORDER BY file_path",
    ).fetchall()
    for (file_path,) in rows:
        rel = Path(_relative(file_path, repo_path))
        parent = rel.parent
        if not parent.parts or any(p in (".terragrunt-cache", ".terraform") for p in parent.parts):
            continue
        raw = parent.parts[-1]
        norm = normalize_identity(raw)
        if (
            norm in facts.dirs or raw.lower() in _GENERIC_DIRS or norm in ENV_SUFFIXES
            or not _NAME_RE.match(raw) or len(facts.dirs) >= _MAX_DIRS_PER_REPO
        ):
            continue
        kind = "terragrunt_stack" if rel.name == "terragrunt.hcl" else "terraform_module"
        facts.dirs[norm] = (raw, str(parent), kind)


def read_repo_facts(entry: Mapping[str, Any]) -> RepoFacts:
    """Read one registered repo's identity facts, read-only. Never raises."""
    from .terraform_links import open_store

    store = open_store(dict(entry))
    facts = RepoFacts()
    if store is None:
        facts.readable = False
        return facts
    repo_path = Path(entry["path"])
    try:
        facts.spring = _spring_names(store)
        _terraform_decls(store, repo_path, facts)
        _stack_dirs(store, repo_path, facts)
    except (sqlite3.Error, GraphStoreError, OSError, ValueError) as exc:
        logger.warning("Cannot read service identity of %s: %s", entry.get("path"), exc)
        facts.readable = False
    finally:
        store.close()
    return facts


class FactsCache:
    """Each registered repo's :class:`RepoFacts`, read at most once per call."""

    def __init__(self, repos: Sequence[Mapping[str, Any]]) -> None:
        self.repos = repos
        self._facts: dict[int, RepoFacts] = {}

    def get(self, index: int) -> RepoFacts:
        if index not in self._facts:
            self._facts[index] = read_repo_facts(self.repos[index])
        return self._facts[index]


# -- The identity index --------------------------------------------------------------------------


def repo_names(entry: Mapping[str, Any]) -> list[str]:
    """The registry alias and folder name of an entry, without duplicates."""
    names = [entry.get("alias"), Path(entry["path"]).name]
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def identities_of(entry: Mapping[str, Any], facts: RepoFacts) -> list[tuple[str, str]]:
    """``(name, matched_via)`` for every name a repo is known by, without the alias file.

    The ECS and ECR names count only for a repo that declares at most
    :data:`MAX_OWN_DECLARATIONS` of them (an infrastructure repo's names are other repos').
    """
    out = [(name, REPO_NAME) for name in repo_names(entry)]
    out += [(name, SPRING_APPLICATION_NAME) for name in facts.spring]
    if facts.own_declaration_count() <= MAX_OWN_DECLARATIONS:
        out += [(d.name, ECS_SERVICE) for d in facts.ecs_services + facts.task_families]
        out += [(d.name, ECR_REPO) for d in facts.ecr]
    return out


class IdentityIndex:
    """``normalised identity -> {repo index: {matched_via}}`` over a whole registry."""

    def __init__(self, repos: Sequence[Mapping[str, Any]], facts: FactsCache) -> None:
        self.by_name: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        for index, entry in enumerate(repos):
            for name, via in identities_of(entry, facts.get(index)):
                key = normalize_identity(name)
                if key:
                    self.by_name[key][index].add(via)

    def lookup(self, service: str) -> dict[int, str]:
        """``repo index -> matched_via`` for the repos *service* normalises onto."""
        found = self.by_name.get(normalize_identity(service), {})
        return {
            index: next(v for v in VIA_ORDER if v in vias) for index, vias in found.items()
        }


# -- The join step -------------------------------------------------------------------------------


def resolve_service(
    service: str,
    lookup: Callable[[str], Sequence[int]],
    normalized: Mapping[str, Any],
    rules: AliasRules,
    identity: Callable[[], IdentityIndex],
) -> fl.TargetResolution:
    """Resolve a Feign service name to registry positions, with ``matched_via``.

    In order: the explicit alias file, then the registered repo names the plain join already
    tries (``repo_name``), then the repos' declared identities (*identity* is called only when
    an earlier step found nothing, so the registry facts are read lazily). A name that reaches
    several repos is returned with ``candidates`` and no ``indexes``: ambiguous, never picked.
    """
    target = rules.target_for(service) if rules else None
    if target:
        hit = tuple(lookup(target))
        if len(hit) == 1:
            return fl.TargetResolution(hit, how="alias", matched_via=EXPLICIT_ALIAS)
        if hit:
            return fl.TargetResolution(
                (), reason="service_name_ambiguous", matched_via=EXPLICIT_ALIAS, candidates=hit,
            )
        logger.debug("service alias for %r points at an unregistered repo", service)
    base = fl.resolve_service(service, lookup, normalized)
    if base.indexes:
        return fl.TargetResolution(base.indexes, how=base.how, matched_via=REPO_NAME)
    if base.reason == "service_name_ambiguous":
        return base
    found = identity().lookup(service)
    if len(found) == 1:
        (index, via), = found.items()
        return fl.TargetResolution((index,), how="inferred", matched_via=via)
    if found:
        return fl.TargetResolution(
            (), reason="service_name_ambiguous", candidates=tuple(sorted(found)),
        )
    return base
