"""Name the AWS resources that application code reads from Spring configuration, per profile.

``aws_sdk_calls`` and ``messaging_edges`` write an edge to ``external:aws-<service>:?`` when a
bucket, queue or topic name is not in the source but in a property (``@Value("${a.b.queue}")``
or a ``@ConfigurationProperties`` getter such as ``props.getQueueName()``). This build-time pass
(run after every build or update, over the finished graph) reads the repository's Spring
configuration files and records on that edge ``extra.resolved_names``: ``{"default": "...",
"dev": "...", "prod": "..."}``, only for the profiles that define the key, so the query-time join
(``aws_sdk_links``) can match it against the resource a Terraform declaration names.

Files read: ``application``/``bootstrap`` ``.yml``/``.yaml``/``.properties`` and their
``-<profile>`` variants (never test resources), including multi-document YAML or ``#---``
properties files whose documents carry ``spring.config.activate.on-profile`` (or the legacy
``spring.profiles``). A document with a negated or compound profile expression is skipped. When
several files of one module define the same key for one profile with different values, that
profile stays unresolved: nothing is guessed. ``${ENV_VAR:default}`` stores the default; with no
default the profile stays unresolved and the variable's name is kept as ``env_placeholder``.

Value policy (strict, decided by the repository owner). A value is stored only when all hold:

* the key is the name of an AWS resource at an SDK or messaging call: a bucket, queue, topic,
  table, rule, function, stream or delivery stream. Every other configuration value is never
  read into the graph, and a secret id or parameter name is never stored;
* no token of the key looks like a secret (password, secret, token, key, credential, private,
  auth, certificate, signature, passphrase ...). Such a key is masked even though it is used as a
  name: the edge says ``names_masked`` and nothing else;
* the value has no whitespace, no ``@`` (no credentials in a URL), at most 128 characters and
  only ``[A-Za-z0-9._:/${}-]``;
* a queue URL or an ARN is reduced to the final resource name: the account id and the host
  are never stored; an account id on its own, an access-key id or a long token-like run is
  rejected;
* the result is a valid name for that kind of resource.

``resolved_names`` therefore never holds anything but resource names, and at most 12 profiles.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

try:  # PyYAML is a hard dependency of the package; guard anyway so a build never breaks on it
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

from .aws_sdk_calls import _NAME_RE, Val, _name_from_arn, resolve_name
from .config_keys import SECRET_TOKENS as _SECRET_TOKENS  # noqa: F401
from .config_keys import is_secret_key, normalize_spring_config_key

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_KINDS", "config_prefixes", "is_secret_key", "resolve_config_names",
    "sanitize_name", "config_key_for_getter",
]

#: Resource kinds whose configured value is ever stored.
ALLOWED_KINDS = frozenset({
    "bucket", "queue", "topic", "table", "rule", "function", "stream", "delivery-stream",
})
_MAX_VALUE = 128
_MAX_PROFILES = 12
_MAX_CONFIG_BYTES = 2_000_000
_VALUE_CHARS = re.compile(r"[A-Za-z0-9._:/${}\-]+")
_PLACEHOLDER = re.compile(r"\$\{([^${}]*)\}")
_ENV_VAR = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_ACCOUNT_ID = re.compile(r"\d{12}")
_ACCESS_KEY = re.compile(r"(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{12,}")
_TOKEN_PREFIX = re.compile(r"(?:ghp_|gho_|ghs_|xox[abprs]-|eyJ|sk-|AIza)")
_PROFILE_FILE = re.compile(
    r"^(?:application|bootstrap)(?:-([A-Za-z0-9_.\-]+))?\.(?:properties|ya?ml)$", re.IGNORECASE,
)
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


# -- Value policy -----------------------------------------------------------------------------


def _looks_like_token(name: str) -> bool:
    if _ACCOUNT_ID.fullmatch(name) or _ACCESS_KEY.fullmatch(name) or _TOKEN_PREFIX.match(name):
        return True
    # A long run with no separator that mixes letters and digits is a hash or a token.
    return (
        len(name) >= 32 and not re.search(r"[._\-]", name)
        and bool(re.search(r"[A-Za-z]", name)) and bool(re.search(r"[0-9]", name))
    )


def _reduce(kind: str, value: str) -> Optional[str]:
    """The resource name inside a URL or an ARN, or the value itself; ``None`` when unusable."""
    if value.startswith("arn:"):
        name = _name_from_arn(kind, value)
        if name is None and kind in ("queue", "topic"):
            parts = value.split(":", 5)
            name = parts[5] if len(parts) == 6 and "/" not in parts[5] else None
        return name
    if "://" in value:
        scheme, _, rest = value.partition("://")
        if scheme == "s3" and kind == "bucket":
            return rest.split("/", 1)[0]
        if scheme in ("http", "https") and kind == "queue":
            host, _, path = rest.partition("/")
            segments = [s for s in path.split("/") if s]
            # ``https://sqs.<region>.amazonaws.com/<account>/<queue>``: the final segment only.
            return segments[-1] if host and segments else None
        return None
    return value


def sanitize_name(kind: str, key: str, raw: Any) -> tuple[Optional[str], Optional[str], bool]:
    """``(name, env_placeholder, masked)`` for the value of *key* used as a *kind* name.

    ``name`` is ``None`` when nothing may be stored. ``masked`` is true when the key looks like a
    secret, whatever its value. ``env_placeholder`` is the variable whose value is not known.
    """
    if is_secret_key(key):
        return None, None, True
    if kind not in ALLOWED_KINDS or not isinstance(raw, str):
        return None, None, False
    value = raw.strip()
    if not value or len(value) > _MAX_VALUE or not _VALUE_CHARS.fullmatch(value):
        return None, None, False
    env: Optional[str] = None
    for _ in range(4):  # defaults may themselves nest a placeholder
        found = _PLACEHOLDER.search(value)
        if found is None:
            break
        variable, _, default = found.group(1).partition(":")
        if not default.strip():
            env = variable.strip() if _ENV_VAR.fullmatch(variable.strip()) else None
            return None, env, False
        value = value[:found.start()] + default.strip() + value[found.end():]
    if "${" in value or "}" in value or "{" in value or len(value) > _MAX_VALUE:
        return None, None, False
    name = _reduce(kind, value)
    pattern = _NAME_RE.get(kind)
    if not name or pattern is None or not pattern.match(name) or _looks_like_token(name):
        return None, None, False
    return name, None, False


# -- Reading configuration files ----------------------------------------------------------------


def _profile_of_file(path: Path) -> Optional[str]:
    match = _PROFILE_FILE.match(path.name)
    if not match:
        return None
    return match.group(1) or "default"


def _doc_profiles(raw: Any) -> Optional[list[str]]:
    """Profiles a document is bound to; ``None`` when it states none, ``[]`` when unusable."""
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if not _PROFILE_NAME.fullmatch(text):
            return []  # ``!prod``, ``a & b`` and the like cannot be attributed
        out.append(text)
    return out


def _flatten(obj: Any, prefix: str, into: dict[str, str], wanted: set[str], depth: int = 0) -> None:
    if depth > 24:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), into, wanted, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", into, wanted, depth + 1)
    elif isinstance(obj, str) and prefix:
        key = normalize_spring_config_key(prefix)
        if key in wanted:
            into[key] = obj


_Docs = list[tuple[Optional[list[str]], dict[str, str]]]


def _yaml_documents(text: str, wanted: set[str]) -> _Docs:
    if _yaml is None:
        return []
    out: _Docs = []
    try:
        for doc in _yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            into: dict[str, str] = {}
            activation: Any = None
            spring = doc.get("spring")
            if isinstance(spring, dict):
                cfg = spring.get("config")
                act = cfg.get("activate") if isinstance(cfg, dict) else None
                if isinstance(act, dict):
                    activation = act.get("on-profile", act.get("onProfile"))
                if activation is None:
                    activation = spring.get("profiles")
                    if isinstance(activation, dict):  # ``spring.profiles.active`` is not it
                        activation = None
            else:
                flat: dict[str, str] = {}
                _flatten(doc, "", flat, {"spring.config.activate.onProfile"})
                activation = flat.get("spring.config.activate.onProfile")
            _flatten(doc, "", into, wanted)
            out.append((_doc_profiles(activation), into))
    except (_yaml.YAMLError, RecursionError, ValueError):
        return []
    return out


_PROP_ASSIGN = re.compile(r"^\s*((?:\\.|[^\s=:\\])+)\s*[=:\s]\s*(.*)$")


def _properties_documents(text: str, wanted: set[str]) -> _Docs:
    docs: list[list[str]] = [[]]
    for line in text.splitlines():
        if re.match(r"^\s*[#!]\s*---\s*$", line):
            docs.append([])
        else:
            docs[-1].append(line)
    out: _Docs = []
    for lines in docs:
        into: dict[str, str] = {}
        activation: Any = None
        pending = ""
        for line in lines:
            if pending:
                line = pending + line.lstrip()
                pending = ""
            if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
                pending = line.rstrip()[:-1]
                continue
            stripped = line.strip()
            if not stripped or stripped[0] in "#!":
                continue
            match = _PROP_ASSIGN.match(line)
            if not match:
                continue
            raw_key, value = match.group(1).replace("\\", ""), match.group(2).strip()
            key = normalize_spring_config_key(raw_key)
            if key in ("spring.config.activate.onProfile", "spring.profiles"):
                activation = value
            elif key in wanted:
                into[key] = value
        out.append((_doc_profiles(activation), into))
    return out


class _Values:
    """``(file, profile) -> {key: raw value}``, only for keys some edge asked for."""

    def __init__(self, wanted: set[str]) -> None:
        self.wanted = wanted
        self.by_file: dict[str, dict[str, dict[str, str]]] = {}

    def read(self, file_path: str) -> dict[str, dict[str, str]]:
        if file_path in self.by_file:
            return self.by_file[file_path]
        found: dict[str, dict[str, str]] = {}
        self.by_file[file_path] = found
        path = Path(file_path)
        base = _profile_of_file(path)
        if base is None:
            return found
        try:
            if path.stat().st_size > _MAX_CONFIG_BYTES:
                return found
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return found
        suffix = path.suffix.lower()
        docs = (
            _properties_documents(text, self.wanted) if suffix == ".properties"
            else _yaml_documents(text, self.wanted)
        )
        for profiles, values in docs:
            for profile in ([base] if profiles is None else profiles):
                found.setdefault(profile, {}).update(values)
        return found


def _module_root(file_path: str) -> str:
    """The directory above ``src/main`` (empty when the path has none)."""
    head, sep, _ = file_path.partition("/src/main/")
    return head if sep else ""


# -- Which edges, which keys ---------------------------------------------------------------------


def config_prefixes(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Class simple name -> its ``@ConfigurationProperties`` prefixes, from the graph."""
    out: dict[str, set[str]] = {}
    rows = conn.execute(
        "SELECT source_qualified, json_extract(extra, '$.config_key') FROM edges "
        "WHERE kind = 'DEPENDS_ON_CONFIG' AND json_valid(extra) "
        "AND json_extract(extra, '$.resolution') = 'configuration_properties'",
    ).fetchall()
    for source, key in rows:
        if isinstance(key, str) and key:
            out.setdefault(source.rsplit("::", 1)[-1].rsplit(".", 1)[-1], set()).add(key)
    return out


def config_key_for_getter(prefixes: dict[str, set[str]], getter: Any) -> Optional[str]:
    """The property key a bean getter reads; only a type with exactly one prefix is used."""
    if not isinstance(getter, dict):
        return None
    found = prefixes.get(str(getter.get("type", "")), set())
    prop = str(getter.get("property", ""))
    if len(found) != 1 or not prop:
        return None
    return normalize_spring_config_key(f"{next(iter(found))}.{prop}")


_CANDIDATES = (
    "SELECT id, kind, target_qualified, file_path, extra FROM edges WHERE json_valid(extra) AND ("
    "(kind = 'REFERENCES' AND json_extract(extra, '$.resolution') = 'aws_sdk' "
    "AND json_extract(extra, '$.unresolved') = 1) "
    "OR (kind IN ('CONSUMES', 'PRODUCES') AND json_extract(extra, '$.unresolved') = 1 "
    "AND (target_qualified LIKE 'external:aws-sqs:%' OR target_qualified LIKE 'external:aws-sns:%')"
    ") OR json_extract(extra, '$.resolved_names') IS NOT NULL "
    "OR json_extract(extra, '$.names_masked') IS NOT NULL "
    "OR json_extract(extra, '$.env_placeholder') IS NOT NULL)"
)
_KIND_BY_SERVICE = {"sqs": "queue", "sns": "topic"}
_OUTPUT_FIELDS = ("resolved_names", "env_placeholder", "names_masked")


def _edge_target(row: Any, info: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """``(kind, key)`` for a candidate edge."""
    target = row[2]
    if row[1] == "REFERENCES":
        kind = info.get("target_kind")
    else:
        service = target.split(":", 2)[1].removeprefix("aws-") if target.count(":") >= 2 else ""
        kind = _KIND_BY_SERVICE.get(service)
    key = info.get("placeholder") or info.get("config_key")
    return (kind if isinstance(kind, str) else None), (key if isinstance(key, str) else None)


def resolve_config_names(conn: sqlite3.Connection, commit: bool = True) -> dict[str, int]:
    """Record ``resolved_names`` on every unresolved AWS edge whose property key is defined.

    Idempotent: results of an earlier run are recomputed, and removed when the configuration no
    longer yields them. Returns ``{"candidates", "resolved", "masked"}``.
    """
    rows = conn.execute(_CANDIDATES).fetchall()
    prefixes: Optional[dict[str, set[str]]] = None
    planned: list[tuple[Any, dict[str, Any], Optional[str], Optional[str]]] = []
    wanted: set[str] = set()
    for row in rows:
        try:
            info = json.loads(row[4])
        except ValueError:
            continue
        if not isinstance(info, dict):
            continue
        kind, key = _edge_target(row, info)
        if key is None and info.get("config_getter") and info.get("unresolved"):
            prefixes = config_prefixes(conn) if prefixes is None else prefixes
            key = config_key_for_getter(prefixes, info["config_getter"])
        key = normalize_spring_config_key(key) if key else None
        if key and info.get("unresolved") and not is_secret_key(key) and kind in ALLOWED_KINDS:
            wanted.add(key)
        planned.append((row, info, kind, key))
    if not planned:
        return {"candidates": 0, "resolved": 0, "masked": 0}
    config_files = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'ConfigProperty'",
        ).fetchall()
        if "/src/test/" not in r[0] and "/test/resources/" not in r[0]
    ] if wanted else []
    values = _Values(wanted)
    constants = _resource_constants(conn, {
        info["const_ref"] for _row, info, _kind, key in planned
        if key is None and info.get("unresolved") and isinstance(info.get("const_ref"), str)
    })
    resolved = masked = 0
    updates: list[tuple[str, int]] = []
    for row, info, kind, key in planned:
        before = json.dumps({k: info.get(k) for k in _OUTPUT_FIELDS}, sort_keys=True)
        for field in _OUTPUT_FIELDS:
            info.pop(field, None)
        if info.get("unresolved") and key and kind:
            names, env, is_masked = _names_for(values, config_files, row[3], kind, key)
            if is_masked:
                info["names_masked"] = True
                masked += 1
            if names:
                info["resolved_names"] = names
                resolved += 1
            if env:
                info["env_placeholder"] = env
        elif info.get("unresolved") and kind and isinstance(info.get("const_ref"), str):
            found = _constant_name(constants, row[3], kind, info["const_ref"])
            if found:
                info["resolved_names"] = {"default": found}
                resolved += 1
        after = json.dumps({k: info.get(k) for k in _OUTPUT_FIELDS}, sort_keys=True)
        if before != after:
            updates.append((json.dumps(info, separators=(",", ":")), row[0]))
    if updates:
        conn.executemany("UPDATE edges SET extra = ? WHERE id = ?", updates)
        if commit:
            conn.commit()
    return {"candidates": len(planned), "resolved": resolved, "masked": masked}


def _resource_constants(
    conn: sqlite3.Connection, names: set[str],
) -> dict[str, list[tuple[str, str]]]:
    """``constant name -> [(file, value)]`` of the resource-like module constants of *names*."""
    if not names:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    rows = conn.execute(
        "SELECT name, file_path, json_extract(extra, '$.resource_value') FROM nodes "
        "WHERE kind = 'Variable' AND json_valid(extra) "
        "AND json_extract(extra, '$.resource_value') IS NOT NULL",
    ).fetchall()
    for name, file_path, value in rows:
        if name in names and isinstance(value, str):
            out.setdefault(name, []).append((file_path, value))
    return out


def _constant_name(
    constants: dict[str, list[tuple[str, str]]], edge_file: str, kind: str, ref: str,
) -> Optional[str]:
    """The resource name a constant stands for, if the repo has one value for it.

    The constants in the edge's own directory are preferred over the rest of the repo. Two
    different values (two modules, two environments) give no answer rather than a guess.
    """
    candidates = constants.get(ref, [])
    here = edge_file.rsplit("/", 1)[0]
    near = [c for c in candidates if c[0].rsplit("/", 1)[0] == here]
    values = {v for _f, v in (near or candidates)}
    if len(values) != 1:
        return None
    name, _placeholder = resolve_name(kind, Val(next(iter(values))))
    return name


def _names_for(
    values: _Values, config_files: list[str], edge_file: str, kind: str, key: str,
) -> tuple[dict[str, str], Optional[str], bool]:
    root = _module_root(edge_file)
    files = [f for f in config_files if not root or _module_root(f) == root]
    if not files:
        files = config_files
    by_profile: dict[str, set[str]] = {}
    env: Optional[str] = None
    if is_secret_key(key):
        return {}, None, True
    masked = False
    for file_path in sorted(files):
        for profile, kv in values.read(file_path).items():
            if key not in kv:
                continue
            name, var, is_masked = sanitize_name(kind, key, kv[key])
            masked = masked or is_masked
            if is_masked:
                continue
            by_profile.setdefault(profile, set()).add(name if name else "\x00")
            if var and env is None:
                env = var
    names: dict[str, str] = {}
    for profile in sorted(by_profile, key=lambda p: (p != "default", p)):
        candidates = by_profile[profile]
        if len(candidates) == 1 and "\x00" not in candidates and len(names) < _MAX_PROFILES:
            names[profile] = next(iter(candidates))
    return names, env, masked
