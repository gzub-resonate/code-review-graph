"""Relate a code repository to the infrastructure declared for it, at query time.

``cross_repo_requests`` with ``origin`` ``terraform``, ``stepfunction`` or ``awssdk`` adds an
``infrastructure`` summary per selected repository. It lists what the whole registry declares
that belongs to that repo, each item with ``matched_via`` saying which of the repo's names it
matched (``explicit_alias``, ``spring_application_name``, ``repo_name`` ...) or
``declared_in_repo`` / ``literal`` / ``resolved_name``:

* ``ecr_repositories``: an ``aws_ecr_repository`` whose literal ``name`` equals a repo name;
* ``ecs_services`` and ``task_definitions``: an ``aws_ecs_service`` ``name`` or an
  ``aws_ecs_task_definition`` ``family`` equal to one;
* ``stacks``: a Terragrunt stack or Terraform module directory whose name equals one;
* ``resources``: a declared queue, topic, bucket, table, function ... (Terraform ``declare``
  edges of every registered repo) whose name equals a name the repo's code uses, written out or
  resolved from Spring configuration (``resolved_names``), plus what the repo itself declares.

Equality is :func:`~code_review_graph.service_aliases.normalize_identity` for names and
directories, and exact for resource names. Nothing is guessed, and nothing is stored. Strings
that come from a repository go through ``_sanitize_name`` with whitespace folded; each list is
capped (``total`` keeps the real count).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from . import aws_sdk_links as al
from . import service_aliases as sa
from .errors import GraphStoreError
from .graph import GraphStore, _sanitize_name
from .repo_cache import MEMO, parallel_map
from .terraform_links import open_store

logger = logging.getLogger(__name__)

__all__ = ["Declaration", "build_infrastructure", "declared_by_for", "read_declarations"]

#: Longest list per category of one repo's summary.
MAX_ITEMS = 10
#: Most repos that get a summary in one call (the rest are counted).
MAX_REPOS = 20
MAX_NOT_CHECKED_NAMES = 50
_MAX_IDENTITIES = 8

#: ``(kind key, summary list key)``.
_CATEGORIES = (
    ("ecr_repository", "ecr_repositories"),
    ("ecs_service", "ecs_services"),
    ("ecs_task_definition", "task_definitions"),
    ("stack", "stacks"),
    ("resource", "resources"),
)


def _clean(value: Any) -> str:
    return " ".join(_sanitize_name(str(value)).split())


def _clean_any(value: Any) -> Any:
    """:func:`_clean` applied to every string in *value*: list items, dict keys and values.

    Numbers, booleans and ``None`` pass through; list lengths are kept.
    """
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, (list, tuple)):
        return [_clean_any(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean_any(v) for k, v in value.items()}
    return value


class Declaration(dict):  # type: ignore[type-arg]
    """One Terraform ``declare`` edge: service, kind, name, file (relative), line, type."""


def _relative(file_path: str, repo_path: Path) -> str:
    try:
        return str(Path(file_path).relative_to(repo_path))
    except ValueError:
        return file_path


def read_declarations(store: GraphStore, repo_path: Path) -> list[Declaration]:
    """The Terraform declarations (queue, topic, bucket, table, function ...) of one graph."""
    return [
        Declaration(
            service=edge["service"], kind=edge["kind"], name=edge["name"],
            file=_relative(edge["file_path"], repo_path), line=edge["line"],
            terraform_type=edge["extra"].get("terraform_type", ""),
        )
        for edge in al.read_declare_edges(store)
    ]


class _Declarations:
    """Every registered repo's declarations, read at most once per call."""

    def __init__(self, repos: Sequence[Mapping[str, Any]]) -> None:
        self.repos = repos
        self._found: dict[int, list[Declaration]] = {}

    def get(self, index: int) -> list[Declaration]:
        if index not in self._found:
            self._found[index] = self._read(index)
        return self._found[index]

    def prefetch(self, indexes: Sequence[int], workers: int) -> None:
        """Read the declarations of *indexes* on up to *workers* threads, ahead of ``get``."""
        todo = [i for i in indexes if i not in self._found]
        for index, found in zip(todo, parallel_map(self._read, todo, workers)):
            self._found[index] = found

    def _read(self, index: int) -> list[Declaration]:
        entry = dict(self.repos[index])
        failed: list[bool] = []

        def compute() -> list[Declaration]:
            store = open_store(entry)
            if store is None:
                failed.append(True)
                return []
            try:
                return read_declarations(store, Path(entry["path"]))
            except (sqlite3.Error, GraphStoreError, OSError) as exc:
                logger.warning("Cannot read declarations of %s: %s", entry["path"], exc)
                failed.append(True)
                return []
            finally:
                store.close()

        # Kept while the repo's graph is the same file; a failed read is not kept.
        return MEMO.get("declarations", entry, compute, cacheable=lambda _found: not failed)


def declared_by_for(
    repos: Sequence[Mapping[str, Any]], targets: Sequence[str], skip: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    """``external:aws-<service>:<name>`` -> where another registered repo declares it.

    Used by ``query_graph external_targets`` for names its own graph does not declare. At most
    the first declaration found per target is returned, with the declaring repo's label.
    """
    wanted = set(targets)
    found: dict[str, dict[str, Any]] = {}
    if not wanted:
        return found
    for entry in repos:
        if len(found) == len(wanted):
            break
        path = Path(entry["path"])
        if skip is not None and path == skip:
            continue
        store = open_store(dict(entry))
        if store is None:
            continue
        try:
            for decl in read_declarations(store, path):
                target = f"external:aws-{decl['service']}:{decl['name']}"
                if target in wanted and target not in found:
                    found[target] = {
                        "repo": _clean(sa.repo_names(entry)[0]),
                        "file": _clean(decl["file"]), "line": decl["line"],
                    }
        except (sqlite3.Error, GraphStoreError, OSError):
            continue
        finally:
            store.close()
    return found


def _code_names(store: GraphStore) -> dict[tuple[str, str], tuple[str, list[str]]]:
    """``(service, name) -> (how, profiles)`` for the names a repo's code uses.

    ``how`` is ``literal`` for a name written in the code, ``resolved_name`` for one read from
    Spring configuration (the profiles that define it are listed).
    """
    names: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for edge in al.read_edges(store):
        if edge["sdk"] == "terraform":
            continue
        key_service = al.decl_key(edge["service"], edge["kind"])
        if not edge["unresolved"] and edge["name"] != "?":
            names.setdefault((key_service, edge["name"]), ("literal", []))
        resolved = edge["extra"].get("resolved_names")
        if isinstance(resolved, dict):
            for profile, name in resolved.items():
                if isinstance(name, str):
                    how, profiles = names.setdefault(
                        (key_service, name), ("resolved_name", []),
                    )
                    if how == "resolved_name" and str(profile) not in profiles:
                        profiles.append(str(profile))
    return names


def _code_names_of(
    entry: Mapping[str, Any],
) -> dict[tuple[str, str], tuple[str, list[str]]]:
    """:func:`_code_names` of one repo, kept while its graph file is the same one."""
    failed: list[bool] = []

    def compute() -> dict[tuple[str, str], tuple[str, list[str]]]:
        store = open_store(dict(entry))
        if store is None:
            return {}
        try:
            return _code_names(store)
        except (sqlite3.Error, GraphStoreError, OSError) as exc:
            logger.warning("Cannot read AWS names of %s: %s", entry["path"], exc)
            failed.append(True)
            return {}
        finally:
            store.close()

    return MEMO.get("code-names", dict(entry), compute, cacheable=lambda _v: not failed)


class _Matcher:
    """Does an infrastructure name belong to one repo, and through which of its names."""

    def __init__(
        self, index: int, repos: Sequence[Mapping[str, Any]], facts: sa.FactsCache,
        rules: sa.AliasRules, lookup: Any,
    ) -> None:
        self.exact: dict[str, str] = {}
        for name, via in sa.identities_of(repos[index], facts.get(index)):
            if via in (sa.ECS_SERVICE, sa.ECR_REPO):
                continue  # the repo's own infrastructure names are reported as owned
            self.exact.setdefault(sa.normalize_identity(name), via)
        self.prefixes: list[str] = []
        for alias, target in rules.exact.items():
            if index in lookup(target):
                self.exact[sa.normalize_identity(alias)] = sa.EXPLICIT_ALIAS
        for prefix, target in rules.prefixes:
            if index in lookup(target):
                self.prefixes.append(prefix)

    def via(self, name: str) -> Optional[str]:
        """``matched_via`` for *name*, or ``None``; an explicit alias rule outranks the rest."""
        folded = sa._SEPARATORS.sub("-", name.strip().lower())
        if any(folded.startswith(prefix) for prefix in self.prefixes):
            return sa.EXPLICIT_ALIAS
        return self.exact.get(sa.normalize_identity(name))


def _item(kind: str, repo: str, name: str, file: str, line: Optional[int], via: str,
          **extra: Any) -> dict[str, Any]:
    return _clean_any({
        "kind": kind, "name": name, "repo": repo, "file": file, "line": line,
        "matched_via": via, **extra,
    })


def _summarise_repo(
    index: int, repos: Sequence[Mapping[str, Any]], facts: sa.FactsCache,
    decls: _Declarations, rules: sa.AliasRules, lookup: Any,
    code_names: Optional[dict[tuple[str, str], tuple[str, list[str]]]] = None,
) -> dict[str, Any]:
    entry = repos[index]
    label = sa.repo_names(entry)[0]
    matcher = _Matcher(index, repos, facts, rules, lookup)
    items: dict[str, list[dict[str, Any]]] = {kind: [] for kind, _ in _CATEGORIES}
    own = facts.get(index)
    # What the repo itself declares for deployment (a service repo with its own Terraform).
    if own.own_declaration_count() <= sa.MAX_OWN_DECLARATIONS:
        for decl in own.ecr:
            items["ecr_repository"].append(
                _item("ecr_repository", label, decl.name, decl.file, decl.line, "declared_in_repo"),
            )
        for decl in own.ecs_services:
            items["ecs_service"].append(
                _item("ecs_service", label, decl.name, decl.file, decl.line, "declared_in_repo"),
            )
        for decl in own.task_families:
            items["ecs_task_definition"].append(
                _item("ecs_task_definition", label, decl.name, decl.file, decl.line,
                      "declared_in_repo"),
            )
    if code_names is None:
        code_names = _code_names_of(entry)
    for other in range(len(repos)):
        other_label = sa.repo_names(repos[other])[0]
        other_facts = facts.get(other)
        if other != index:
            for kind, found in (
                ("ecr_repository", other_facts.ecr), ("ecs_service", other_facts.ecs_services),
                ("ecs_task_definition", other_facts.task_families),
            ):
                for decl in found:
                    via = matcher.via(decl.name)
                    if via:
                        items[kind].append(
                            _item(kind, other_label, decl.name, decl.file, decl.line, via),
                        )
            for norm, (raw, rel_dir, stack_kind) in other_facts.dirs.items():
                via = matcher.via(raw)
                if via:
                    items["stack"].append(_item(
                        "stack", other_label, raw, rel_dir, None, via, stack_kind=stack_kind,
                    ))
        if other != index and not code_names:
            # A repo whose code names nothing matches no other repo's declaration.
            continue
        for decl in decls.get(other):
            used = code_names.get((al.decl_key(decl["service"], decl["kind"]), decl["name"]))
            if used is not None:
                items["resource"].append(_item(
                    decl["kind"] or "resource", other_label, decl["name"], decl["file"],
                    decl["line"], used[0], service=decl["service"],
                    owned=other == index, terraform_type=decl["terraform_type"],
                    **({"profiles": used[1][:5]} if used[1] else {}),
                ))
            elif other == index:
                items["resource"].append(_item(
                    decl["kind"] or "resource", other_label, decl["name"], decl["file"],
                    decl["line"], "declared_in_repo", service=decl["service"], owned=True,
                    terraform_type=decl["terraform_type"],
                ))
    summary: dict[str, Any] = {
        "identity": _clean_any([
            {"name": name, "matched_via": via}
            for name, via in _identity_names(entry, own, rules, lookup, index)[:_MAX_IDENTITIES]
        ]),
    }
    counts: dict[str, int] = {}
    for kind, key in _CATEGORIES:
        found = _dedupe(items[kind])
        if found:
            counts[key] = len(found)
            summary[key] = found[:MAX_ITEMS]
    summary["counts"] = counts
    return summary


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["kind"], row["name"], row["repo"], row["file"], row["line"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _identity_names(
    entry: Mapping[str, Any], facts: sa.RepoFacts, rules: sa.AliasRules, lookup: Any, index: int,
) -> list[tuple[str, str]]:
    names = [(alias, sa.EXPLICIT_ALIAS) for alias, target in rules.exact.items()
             if index in lookup(target)]
    names += [(f"prefix:{prefix}", sa.EXPLICIT_ALIAS) for prefix, target in rules.prefixes
              if index in lookup(target)]
    return names + sa.identities_of(entry, facts)


def build_infrastructure(
    repos: Sequence[Mapping[str, Any]],
    selected: Sequence[int],
    facts: sa.FactsCache,
    rules: sa.AliasRules,
    lookup: Any,
    budget: float = 0,
    workers: int = 1,
) -> dict[str, Any]:
    """The ``infrastructure`` block of a reply: ``{"repos": {label: summary}, ...}``.

    Only selected repos that have at least one match appear; ``repos_total`` counts those,
    ``repos_checked`` how many were looked at, and ``truncated`` is set when the repo cap
    or the time budget (seconds, ``0`` for none) stopped the work early.
    When it did, ``repos_not_checked`` names the selected repos left out (up to 50, with
    ``repos_not_checked_total``), so a call with ``repos`` narrowed to them covers the rest.
    """
    started = time.monotonic()
    decls = _Declarations(repos)
    code_names: dict[int, dict[tuple[str, str], tuple[str, list[str]]]] = {}
    if selected:
        # Every summary looks at every repo's facts, so read them all ahead, on *workers*
        # threads. A repo's declarations matter only against the names some selected repo's
        # code uses (and to the repo itself), so they are read for every repo only then.
        everything = range(len(repos))
        facts.prefetch(everything, workers)
        todo = list(selected[:MAX_REPOS])
        for index, names in zip(todo, parallel_map(
            lambda i: _code_names_of(repos[i]), todo, workers,
        )):
            code_names[index] = names
        decls.prefetch(everything if any(code_names.values()) else todo, workers)
    summaries: dict[str, Any] = {}
    checked = 0
    truncated = False
    matched_total = 0
    left: list[str] = []
    for position, index in enumerate(selected):
        if checked >= MAX_REPOS or (budget and time.monotonic() - started > budget):
            truncated = True
            left = [_clean(sa.repo_names(repos[i])[0]) for i in selected[position:]]
            break
        checked += 1
        summary = _summarise_repo(
            index, repos, facts, decls, rules, lookup, code_names.get(index),
        )
        if summary["counts"]:
            matched_total += 1
            summaries[_clean(sa.repo_names(repos[index])[0])] = summary
    block: dict[str, Any] = {
        "repos": summaries, "repos_total": matched_total, "repos_checked": checked,
        "truncated": truncated or checked < len(selected),
    }
    if truncated:
        # Name what was not looked at, so a caller can ask again with ``repos`` narrowed to it.
        block["repos_not_checked"] = left[:MAX_NOT_CHECKED_NAMES]
        block["repos_not_checked_total"] = len(left)
    return block
