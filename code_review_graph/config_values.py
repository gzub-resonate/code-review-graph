"""Resolve Spring configuration values from the graph at query time.

``ConfigProperty`` nodes carry the literal value (``extra.value``, see
:func:`code_review_graph.config_keys.storable_config_value`; secret-looking values are never
stored). This module reads those nodes of one repository graph and answers "what is key K worth":

* Lookup uses Spring's relaxed binding: ``queue-name``, ``queueName``, ``queue_name`` and the
  environment form ``QUEUE_NAME`` all find the same property. An exact (normalized) match wins;
  a relaxed match that reaches two different properties is ambiguous and finds nothing.
* A value is read per profile: ``application.yml`` is ``default``, ``application-<p>.yml`` and a
  YAML document bound with ``spring.config.activate.on-profile`` are ``<p>``. When a key is
  defined twice for one profile with different values (two modules, two files) that profile is
  ambiguous and is left out: nothing is guessed.
* ``${KEY:default}`` and ``${ENV_VAR}`` placeholders are expanded when they refer to another
  property of the same repo (same profile first, then the default profile); otherwise the
  placeholder's default is used; with no default the value stays unresolved and the variable is
  named.
* The default profile is preferred; the result says which profile supplied the value and lists
  the other profiles' values when they differ. With no default value and several differing
  profiles there is no chosen value, only the alternatives.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .config_keys import is_secret_key, normalize_spring_config_key

DEFAULT_PROFILE = "default"
_PLACEHOLDER = re.compile(r"\$\{([^${}]*)\}")
_PROFILE_FILE = re.compile(
    r"^(?:application|bootstrap)(?:-([A-Za-z0-9_.\-]+))?\.(?:properties|ya?ml)$", re.IGNORECASE,
)
_MAX_DEPTH = 6
_MAX_ALTERNATIVES = 8


def canonical(key: str) -> str:
    """The relaxed-binding form of a key: lower case letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


def module_root(file_path: str) -> str:
    """The directory above ``src/main`` (empty when the path has none)."""
    head, sep, _ = file_path.partition("/src/main/")
    return head if sep else ""


@dataclass
class _Entry:
    key: str
    profile: str
    value: Optional[str]
    truncated: bool
    root: str


@dataclass
class Resolved:
    """The answer for one key. ``value`` is ``None`` when nothing usable was found."""

    key: str
    defined: bool = False
    value: Optional[str] = None
    profile: Optional[str] = None
    #: profile -> value for the other profiles whose (expanded) value differs
    alternatives: dict[str, str] = field(default_factory=dict)
    #: human-readable steps: the property read and each placeholder that was expanded
    via: list[str] = field(default_factory=list)
    #: the environment variable left unexpanded when no profile gave a value
    env_placeholder: Optional[str] = None
    #: ``not_defined`` | ``no_value`` | ``env_placeholder`` | ``profiles_differ`` | ``ambiguous``
    reason: Optional[str] = None


class ConfigValues:
    """The ``ConfigProperty`` nodes of one graph, indexed for relaxed lookup."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._exact: dict[str, list[_Entry]] = {}
        self._relaxed: dict[str, list[_Entry]] = {}
        rows = conn.execute(
            "SELECT name, file_path, extra FROM nodes WHERE kind = 'ConfigProperty' "
            "ORDER BY file_path, line_start, id",
        ).fetchall()
        for name, file_path, raw in rows:
            if "/src/test/" in file_path or "/test/resources/" in file_path:
                continue
            try:
                extra = json.loads(raw) if raw else {}
            except ValueError:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            file_name = file_path.rsplit("/", 1)[-1]
            match = _PROFILE_FILE.match(file_name)
            if match is None:
                continue
            profiles = extra.get("doc_profiles")
            if not (isinstance(profiles, list) and profiles):
                profiles = [match.group(1) or DEFAULT_PROFILE]
            value = extra.get("value")
            for profile in profiles:
                entry = _Entry(
                    name, str(profile), value if isinstance(value, str) else None,
                    bool(extra.get("value_truncated")), module_root(file_path),
                )
                self._exact.setdefault(normalize_spring_config_key(name), []).append(entry)
                self._relaxed.setdefault(canonical(name), []).append(entry)

    # -- lookup ------------------------------------------------------------------------------

    def _entries(self, key: str, root: str) -> tuple[list[_Entry], bool]:
        """``(entries, ambiguous)`` for *key*, preferring the caller's module."""
        exact = self._exact.get(normalize_spring_config_key(key))
        found = exact
        if not found:
            found = self._relaxed.get(canonical(key), [])
            if len({e.key for e in found}) > 1:
                return [], True
        if root:
            same = [e for e in found if e.root == root]
            found = same or found
        return list(found), False

    def defines(self, key: str, root: str = "") -> bool:
        """Whether any configuration file of the repo defines *key* (relaxed binding)."""
        return bool(self._entries(key, root)[0])

    def _by_profile(
        self, key: str, root: str,
    ) -> tuple[dict[str, Optional[tuple[str, bool]]], bool]:
        """``profile -> (value, truncated)``, ``None`` for a profile with conflicting values."""
        entries, ambiguous = self._entries(key, root)
        out: dict[str, Optional[tuple[str, bool]]] = {}
        for entry in entries:
            if entry.value is None:
                out.setdefault(entry.profile, None)
                continue
            seen = out.get(entry.profile)
            if entry.profile in out and seen != (entry.value, entry.truncated):
                out[entry.profile] = None
            else:
                out[entry.profile] = (entry.value, entry.truncated)
        return out, ambiguous

    # -- expansion ---------------------------------------------------------------------------

    def _expand(
        self, raw: str, profile: str, root: str, trail: tuple[str, ...], via: list[str],
        env: list[str],
    ) -> Optional[str]:
        value = raw
        for _ in range(16):
            found = _PLACEHOLDER.search(value)
            if found is None:
                return value
            name, has_default, default = found.group(1).partition(":")
            name = name.strip()
            replacement = self._placeholder(
                name, default if has_default else None, profile, root, trail, via, env,
            )
            if replacement is None:
                return None
            value = value[:found.start()] + replacement + value[found.end():]
        return None

    def _placeholder(
        self, name: str, default: Optional[str], profile: str, root: str,
        trail: tuple[str, ...], via: list[str], env: list[str],
    ) -> Optional[str]:
        canon = canonical(name)
        if name and not is_secret_key(name) and canon not in trail and len(trail) < _MAX_DEPTH:
            by_profile, ambiguous = self._by_profile(name, root)
            if not ambiguous:
                for source in (profile, DEFAULT_PROFILE):
                    got = by_profile.get(source)
                    if got is None or got[1]:  # missing, conflicting or truncated
                        continue
                    inner = self._expand(got[0], source, root, (*trail, canon), via, env)
                    if inner is not None:
                        via.append(f"${{{name}}} = property {name} ({source})")
                        return inner
        if default is not None and default.strip():
            via.append(f"${{{name}}} = default {default.strip()!r}")
            return default.strip()
        if name and re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name) and not env:
            env.append(name)
        return None

    # -- the answer --------------------------------------------------------------------------

    def resolve(self, key: str, root: str = "") -> Resolved:
        """The value of *key* per profile, expanded, with the profile that supplied it."""
        result = Resolved(key=key)
        by_profile, ambiguous = self._by_profile(key, root)
        if ambiguous:
            result.reason = "ambiguous"
            return result
        if not by_profile:
            result.reason = "not_defined"
            return result
        result.defined = True
        if is_secret_key(key):
            result.reason = "no_value"
            return result
        expanded: dict[str, tuple[str, list[str]]] = {}
        env: list[str] = []
        for profile, got in by_profile.items():
            if got is None or got[1]:
                continue
            steps: list[str] = []
            value = self._expand(got[0], profile, root, (canonical(key),), steps, env)
            if value is not None and value.strip():
                expanded[profile] = (value.strip(), [f"{key} = {got[0]}", *steps])
        if not expanded:
            result.reason = "env_placeholder" if env else "no_value"
            result.env_placeholder = env[0] if env else None
            return result
        order = sorted(expanded, key=lambda p: (p != DEFAULT_PROFILE, p))
        if DEFAULT_PROFILE in expanded:
            chosen = DEFAULT_PROFILE
        else:
            distinct = {expanded[p][0] for p in order}
            if len(distinct) > 1:
                result.reason = "profiles_differ"
                result.alternatives = {p: expanded[p][0] for p in order[:_MAX_ALTERNATIVES]}
                return result
            chosen = order[0]
        result.value, result.profile = expanded[chosen][0], chosen
        result.via = expanded[chosen][1]
        result.alternatives = {
            p: expanded[p][0] for p in order if expanded[p][0] != result.value
        }
        for profile in list(result.alternatives)[_MAX_ALTERNATIVES:]:
            del result.alternatives[profile]
        return result
