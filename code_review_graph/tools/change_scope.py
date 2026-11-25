"""One shared "what is the scope of this change review?" rule for the change tools.

Every change-oriented tool (``detect_changes``, ``get_review_context``,
``get_impact_radius``, ``get_affected_flows`` and ``pr_blast_radius``) used to treat a
call that named no change differently: one erred, one silently looked at the last commit,
one at the working tree. The rule is now the same everywhere:

* a call that names no ``pr``, ``head``, ``base`` or ``changed_files`` reviews the **last
  commit** (``HEAD~1..HEAD``, or the empty tree up to ``HEAD`` in a one-commit
  repository), and the result says so in a ``scope`` object and a ``note`` line;
* a call that names a change keeps its behaviour and reports ``scope.kind == "explicit"``;
* when no commit range can be resolved at all (no git history), the error carries the
  same hint on how to widen the scope.
"""

from __future__ import annotations

import contextvars
import functools
import subprocess
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..constants import discovery_timeout as _discovery_timeout
from ..errors import ChangeDiscoveryError
from ..incremental import (
    DEFAULT_REVIEW_BASE,
    _empty_tree,
    _git_is_shallow,
    detect_vcs,
    discover_review_changes,
    get_changed_files,
)

#: The one hint every change tool returns when it chose the scope itself.
CHANGE_SCOPE_HINT = (
    "To widen or change the scope, pass pr=<number>, head=<ref or commit> (with "
    "base=<branch> for a stacked pull request), base=<ref> for a larger range such as "
    'base="HEAD~20", or changed_files=[...].'
)

#: Why the scope is the last commit.
LAST_COMMIT_REASON = "no pr/head/base/changed_files given"

KIND_LAST_COMMIT = "last_commit"
KIND_EXPLICIT = "explicit"

_SCOPE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "crg_change_scope", default=None,
)

F = TypeVar("F", bound=Callable[..., Any])


def uses_default_scope(
    changed_files: Any = None, base: str | None = DEFAULT_REVIEW_BASE,
    pr: int | None = None, head: str | None = None,
) -> bool:
    """True when the caller named no change at all."""
    return (
        changed_files is None and pr is None and not head
        and (base is None or base == DEFAULT_REVIEW_BASE)
    )


def scope_note(scope: dict[str, Any]) -> str:
    """The human-readable line for a scope the tool chose itself."""
    files = scope.get("files_considered")
    count = f", {files} file(s)" if isinstance(files, int) else ""
    return (
        f"Scope: last commit ({scope['base'][:12]}..{scope['head'][:12]}{count}); "
        f"{scope['reason']}. {CHANGE_SCOPE_HINT}"
    )


def build_scope(
    kind: str, *, base: str | None = None, head: str | None = None,
    files_considered: int | None = None, reason: str = "", **extra: Any,
) -> dict[str, Any]:
    """Assemble a ``scope`` object."""
    scope: dict[str, Any] = {"kind": kind, "base": base, "head": head}
    if files_considered is not None:
        scope["files_considered"] = files_considered
    scope["reason"] = reason
    scope.update(extra)
    if kind == KIND_LAST_COMMIT:
        scope["hint"] = CHANGE_SCOPE_HINT
    return scope


def record_scope(scope: dict[str, Any]) -> None:
    """Remember the scope of the running tool call; ``scoped`` attaches it to the result."""
    _SCOPE.set(scope)


def record_explicit_scope(
    source: str, files_considered: int | None = None, *,
    base: str | None = None, head: str | None = None,
) -> None:
    """Record that the caller named the change (``changed_files``, ``base``, ``pr``, ``head``)."""
    record_scope(build_scope(
        KIND_EXPLICIT, base=base, head=head, files_considered=files_considered,
        reason=f"{source} given", source=source,
    ))


def _git(root: Path, *args: str, timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(root), timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeDiscoveryError(
            f"could not look up the last commit: git could not be run ({exc}). "
            f"{CHANGE_SCOPE_HINT}"
        ) from exc
    return done.returncode, done.stdout.strip()


def resolve_last_commit(root: Path, timeout: float | None = None) -> tuple[str, str, list[str]]:
    """Return ``(base, head, notes)`` of the last commit.

    ``base`` is the parent commit, or the empty tree when HEAD has no parent (a one-commit
    repository, or a shallow clone whose parent is not fetched). Raises
    :class:`ChangeDiscoveryError`, carrying the shared hint, when there is no commit at all.
    """
    budget = timeout if timeout is not None else _discovery_timeout()
    if detect_vcs(root) != "git":
        raise ChangeDiscoveryError(
            "no commit range can be resolved: this folder has no git history. "
            + CHANGE_SCOPE_HINT
        )
    code, head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", timeout=budget)
    if code != 0 or not head:
        raise ChangeDiscoveryError(
            "no commit range can be resolved: this repository has no commits yet. "
            + CHANGE_SCOPE_HINT
        )
    code, parent = _git(
        root, "rev-parse", "--verify", "--quiet", "HEAD~1^{commit}", timeout=budget,
    )
    if code == 0 and parent:
        return parent, head, []
    note = (
        "HEAD has no parent here (a shallow clone); the whole of HEAD is treated as the last "
        "commit. Run `git fetch --deepen=1` for the real last commit."
        if _git_is_shallow(root, budget)
        else "this repository has one commit; the scope is the whole of that commit."
    )
    return _empty_tree(root, budget), head, [note]


def discover_changes_with_scope(
    root: Path, base: str = DEFAULT_REVIEW_BASE, *, notes: list[str] | None = None,
    discover: Callable[..., tuple[list[str], str]] = discover_review_changes,
) -> tuple[list[str], str]:
    """``discover_review_changes`` plus the shared scope rule.

    With the default base the last commit is reviewed (also in a one-commit repository);
    any other base behaves as before and is reported as explicit. Returns
    ``(changed_files, resolved_base)`` like ``discover_review_changes``, which *discover*
    stands for (the calling tool module passes its own binding of it).
    """
    if not uses_default_scope(None, base):
        files, resolved = discover(root, base, notes=notes)
        record_explicit_scope("base", len(files), base=resolved)
        return files, resolved

    if detect_vcs(root) == "svn":
        # No commit range to name; keep the VCS's own default.
        files, resolved = discover(root, base, notes=notes)
        record_scope(build_scope(
            "working_copy", base=resolved, files_considered=len(files),
            reason=LAST_COMMIT_REASON,
        ))
        return files, resolved

    budget = _discovery_timeout()
    parent, head, scope_notes = resolve_last_commit(root, budget)
    if scope_notes:
        # No parent commit: the base is the empty tree, which discovery would reject as
        # "not a known revision", so diff against it directly.
        files = get_changed_files(root, parent, timeout=budget, require_vcs=True)
    else:
        files, parent = discover(root, parent, notes=notes)
    if notes is not None:
        notes.extend(scope_notes)
    record_scope(build_scope(
        KIND_LAST_COMMIT, base=parent, head=head, files_considered=len(files),
        reason=LAST_COMMIT_REASON + "; reviewing the last commit",
    ))
    return files, parent


def attach_scope(result: Any, scope: dict[str, Any] | None) -> Any:
    """Put ``scope`` (and, for a scope the tool chose, the shared ``note``) on a result."""
    if scope is None or not isinstance(result, dict) or result.get("status") != "ok":
        return result
    result["scope"] = scope
    if scope["kind"] == KIND_LAST_COMMIT:
        note = scope_note(scope)
        existing = result.get("note")
        result["note"] = f"{existing} {note}" if existing else note
    return result


def scoped(func: F) -> F:
    """Decorator: attach the scope recorded during the call to an ``ok`` result."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _SCOPE.set(None)
        try:
            result = func(*args, **kwargs)
            return attach_scope(result, _SCOPE.get())
        finally:
            _SCOPE.reset(token)

    return wrapper  # type: ignore[return-value]
