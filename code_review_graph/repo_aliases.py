"""Optional hand-maintained repo aliases ("a second name for a repository").

A ``registry.json`` entry holds one alias, and ``register --alias`` replaces
it, so a repository cannot be known by a second name.  This module reads an
optional file next to ``registry.json`` that adds more names::

    # $CRG_HOME/repo-aliases.toml  (default: ~/.code-review-graph/)
    [aliases]
    old-service-name = "registered-repo-alias-or-folder"

Names resolve in this order everywhere a repository is picked by name: a real
registry alias, then a folder name, then this file.  Nothing in the file can
shadow a registered repo, and the target is looked up through the first two
rules only (no chains).  The file is never written by ``register`` or
``unregister``.

The loader is deliberately defensive: a broken file never raises.  Invalid
entries are skipped with a ``logger.warning``, and a file that cannot be
parsed at all yields no aliases.  At most ``MAX_REPO_ALIASES`` entries are
honoured.

See docs/REPO_ALIASES.md for the reference.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from .constants import crg_home

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: File name, relative to :func:`~code_review_graph.constants.crg_home`.
REPO_ALIASES_FILENAME = "repo-aliases.toml"

#: Hard cap on the number of aliases honoured from one file.  The file is
#: hand-maintained, so a few dozen is typical; the cap only bounds the work
#: and the log output if the file is generated badly or hostile.
MAX_REPO_ALIASES = 200

#: Alias names: letters, digits, ``.``, ``_`` and ``-``, 1-100 characters.
#: Case-sensitive on purpose; names are never case-folded.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def repo_aliases_path(home: Optional[Path] = None) -> Path:
    """Return the full path to ``repo-aliases.toml``."""
    return (Path(home) if home is not None else crg_home()) / REPO_ALIASES_FILENAME


def load_repo_aliases(home: Optional[Path] = None) -> dict[str, str]:
    """Load and validate ``<home>/repo-aliases.toml``.

    Returns a mapping of alias name -> target (a registered alias or folder
    name).  Always returns (possibly empty); a broken file never raises.

    Args:
        home: Directory holding the file.  Defaults to ``crg_home()``.
    """
    path = repo_aliases_path(home)
    try:
        if not path.is_file():
            return {}  # No file: the common case, not worth a log line.
    except OSError:
        return {}

    if tomllib is None:
        logger.warning(
            "%s found but TOML parsing requires the 'tomli' package on "
            "Python < 3.11 - no repo aliases loaded",
            path,
        )
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Cannot read %s: %s - no repo aliases loaded", path, exc)
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Malformed TOML in %s: %s - no repo aliases loaded", path, exc)
        return {}

    table = data.get("aliases")
    if table is None:
        return {}
    if not isinstance(table, dict):
        logger.warning("%s: [aliases] must be a table - no repo aliases loaded", path)
        return {}

    result: dict[str, str] = {}
    for name, target in table.items():
        if len(result) >= MAX_REPO_ALIASES:
            logger.warning(
                "%s defines more than %d repo aliases - ignoring the rest",
                path, MAX_REPO_ALIASES,
            )
            break
        if not _NAME_RE.match(name):
            logger.warning(
                "%s: invalid repo alias name %r (expected letters, digits, "
                "'.', '_' or '-', 1-100 characters) - skipping",
                path, name,
            )
            continue
        if not isinstance(target, str) or not target.strip():
            logger.warning(
                "%s: repo alias %r needs a non-empty string target - skipping",
                path, name,
            )
            continue
        result[name] = target.strip()
    return result


class RepoNameResolver:
    """Resolve repo names against registry entries; the one shared lookup.

    Precedence: (1) a real registry alias, (2) a folder name, (3) the alias
    file.  The file is read at most once per resolver, and only when a name
    matched neither of the first two.  A file target is resolved through
    rules 1 and 2 only, so chains and cycles cannot occur.

    Args:
        repos: Registry entries (dicts with ``path`` and optional ``alias``).
        home: Directory holding the alias file.  Defaults to ``crg_home()``.
    """

    def __init__(self, repos: Sequence[dict[str, Any]], home: Optional[Path] = None) -> None:
        self._home = home
        self._aliases: Optional[dict[str, str]] = None
        self._by_alias: dict[str, list[int]] = {}
        self._by_folder: dict[str, list[int]] = {}
        for index, entry in enumerate(repos):
            alias = entry.get("alias")
            # Only an EXPLICIT alias goes in the alias namespace; an entry
            # without one is addressable by its folder name alone, so it
            # cannot outrank another entry's deliberate alias.
            if alias:
                self._by_alias.setdefault(alias, []).append(index)
            self._by_folder.setdefault(Path(entry["path"]).name, []).append(index)

    def _registered(self, name: str) -> list[int]:
        return self._by_alias.get(name) or self._by_folder.get(name) or []

    def lookup(self, name: str) -> list[int]:
        """Return indexes of the entries ``name`` selects (empty: unresolved)."""
        matches = self._registered(name)
        if matches:
            return matches
        if self._aliases is None:
            self._aliases = load_repo_aliases(self._home)
        target = self._aliases.get(name)
        if target is None:
            return []
        matches = self._registered(target)
        if not matches:
            logger.warning(
                "%s: repo alias %r points at %r, which matches no registered "
                "repository - ignoring",
                repo_aliases_path(self._home), name, target,
            )
        return matches
