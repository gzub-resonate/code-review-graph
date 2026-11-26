"""Shared, value-free handling for Spring configuration property keys."""

from __future__ import annotations

import re
from pathlib import Path

_SPRING_CONFIG_NAME = re.compile(
    r"^(?:application|bootstrap)(?:-[A-Za-z0-9_.-]+)?\.(?:properties|ya?ml)$",
    re.IGNORECASE,
)


def is_spring_config_path(path: Path) -> bool:
    """Return whether *path* uses Spring Boot's application or bootstrap file convention."""
    return bool(_SPRING_CONFIG_NAME.fullmatch(path.name))


def normalize_spring_config_key(key: str) -> str:
    """Canonicalize relaxed-binding spellings without inspecting their values."""
    normalized: list[str] = []
    for segment in key.strip().split("."):
        match = re.fullmatch(r"(.*?)(\[[0-9]+\])?", segment)
        base = match.group(1) if match else segment
        index = match.group(2) or "" if match else ""
        tokens = [token for token in re.split(r"[-_]+", base) if token]
        if not tokens:
            normalized.append(index)
            continue
        head = tokens[0].lower() if base.isupper() or len(tokens) > 1 else tokens[0]
        tail = "".join(token[:1].upper() + token[1:].lower() for token in tokens[1:])
        normalized.append(f"{head}{tail}{index}")
    return ".".join(normalized)


# -- Secret handling and storable values -----------------------------------------------------

#: Tokens that mark a property key as a secret (any token of the key matching is enough).
SECRET_TOKENS = frozenset({
    "password", "passwd", "pwd", "pass", "passphrase", "secret", "secrets", "token", "tokens",
    "key", "keys", "credential", "credentials", "cred", "creds", "private", "auth",
    "authorization", "authentication", "apikey", "accesskey", "signature", "sig", "cert",
    "certificate", "keystore", "truststore", "salt",
})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_PLACEHOLDER_NAME = re.compile(r"\$\{([^${}:]*)")

#: Longest value kept on a node; a longer one is cut and flagged ``value_truncated``.
MAX_STORED_VALUE = 200

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")
_TOKEN_PREFIX = re.compile(
    r"(?:ghp_|gho_|ghs_|github_pat_|xox[abprs]-|sk-|AIza|AKIA|ASIA|glpat-)[A-Za-z0-9_\-]{8,}",
)
_USERINFO_URL = re.compile(r"://[^/\s@]*@")
_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")
_RANDOM_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])")


def is_secret_key(key: str) -> bool:
    """Whether a property key names something that looks like a secret (any token matches)."""
    flat = re.sub(r"\[[0-9]+\]", "", key)
    for part in re.split(r"[.\-_]+", flat):
        for token in _CAMEL.split(part):
            if token.lower() in SECRET_TOKENS:
                return True
    lowered = flat.lower().replace("-", "").replace("_", "").replace(".", "")
    return any(w in lowered for w in ("password", "secret", "apikey", "accesskey", "credential"))


def _looks_random(run: str) -> bool:
    """A long separator-free run that mixes cases and digits: a token, not a name."""
    return bool(
        _RANDOM_RUN.fullmatch(run) and re.search(r"[a-z]", run)
        and re.search(r"[A-Z]", run) and re.search(r"[0-9]", run)
    )


def value_looks_secret(value: str) -> bool:
    """Whether a value looks like a private key, JWT, access token, credential URL or random run."""
    if "-----BEGIN" in value or _JWT.search(value) or _TOKEN_PREFIX.search(value):
        return True
    if _USERINFO_URL.search(value) or _HEX_RUN.search(value):
        return True
    return any(_looks_random(run) for run in re.split(r"[\s,;]+", value))


def storable_config_value(key: str, raw: object) -> tuple[str, bool] | None:
    """``(value, truncated)`` when a configuration value may be kept on its node, else ``None``.

    Never kept: the value of a key that looks like a secret, a placeholder whose variable looks
    like a secret (``${DB_PASSWORD:fallback}``), multi-line text, and anything that looks like a
    private key, a JWT, an access token, a URL with credentials or a long random string. A
    12-digit AWS account id is replaced by ``ACCOUNT``.
    """
    if not isinstance(raw, str) or is_secret_key(key):
        return None
    value = raw.strip()
    if not value or "\n" in value or "\r" in value or value_looks_secret(value):
        return None
    if any(is_secret_key(name) for name in _PLACEHOLDER_NAME.findall(value)):
        return None
    value = _ACCOUNT_ID.sub("ACCOUNT", value)  # an AWS account id inside an ARN or queue URL
    if len(value) > MAX_STORED_VALUE:
        return value[:MAX_STORED_VALUE], True
    return value, False
