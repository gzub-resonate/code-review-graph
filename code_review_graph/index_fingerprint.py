"""Fingerprint of everything that can change what a graph build writes.

A pin bump of this package does not always change graph contents: most
commits touch the query side (MCP tools, formatting, read-side helpers). A
deployment that rebuilds every graph on each version change can compare this
fingerprint instead and rebuild only when it moves.

The fingerprint hashes, in a deterministic order:

* the transitive in-package import closure of the build entry points (full
  build, incremental update, post-processing, embedding, schema migrations),
  found with ``ast`` over every ``import`` and ``from ... import`` node,
  including function-level and relative ones, ``__init__`` modules of parent
  packages, and ``importlib.import_module`` calls with a constant name;
* each included module as a normalised syntax tree (docstrings removed, no
  line or column attributes), so comment, docstring and formatting edits do
  not change it;
* the installed versions of the parsing and embedding dependencies, and the
  environment variables that select an embedding model or switch a build
  stage on;
* the Python minor version, and this module's own normalised source.

Anything that cannot be classified goes in: a false rebuild is cheap, a stale
graph is a silent bug. ``compute_fingerprint`` never raises; it returns
``"unknown"`` when it cannot compute, which callers must treat as "no
fingerprint available".

Run ``python -m code_review_graph.index_fingerprint`` to print it.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sys
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path

UNKNOWN = "unknown"

#: Bumped when the hashing scheme itself changes.
FORMAT_VERSION = "1"

_HEX_LEN = 16

#: Modules the build path starts from, relative to the package. ``tools.build``
#: runs ``build`` and ``update`` (parse, store, resolver passes, flows,
#: communities, search index); ``tools.docs`` runs ``embed``; ``postprocessing``
#: is the shared post-build pipeline of the other entry points; ``migrations``
#: owns the graph schema. Everything they import is pulled in by the closure.
BUILD_ENTRY_POINTS: tuple[str, ...] = (
    "tools.build",
    "tools.docs",
    "postprocessing",
    "embeddings",
    "migrations",
    "graph",
    "parser",
    "incremental",
)

#: Distributions whose version can change what a build writes. Every
#: ``tree-sitter*`` distribution is added on top of these.
DEPENDENCIES: tuple[str, ...] = (
    "tree-sitter",
    "tree-sitter-language-pack",
    "jedi",
    "igraph",
    "networkx",
    "pyyaml",
    "sentence-transformers",
    "numpy",
    "google-genai",
)

#: Environment variables that pick an embedding model or dimension, or switch a
#: build stage. Their values are hashed; credentials and paths are not.
ENV_VARS: tuple[str, ...] = (
    "CRG_EMBEDDING_MODEL",
    "CRG_OPENAI_MODEL",
    "CRG_OPENAI_DIMENSION",
    "CRG_VOYAGE_MODEL",
    "CRG_VOYAGE_OUTPUT_DIMENSION",
    "CRG_VOYAGE_OUTPUT_DTYPE",
    "CRG_ENABLE_ENRICHMENT",
    "CRG_DISABLE_ENRICHMENT",
    "CRG_RECURSE_SUBMODULES",
    "CRG_FOLLOW_SYMLINKS",
)

#: Non-Python files that a build reads from inside the package. None today
#: (grammars come from the installed tree-sitter packages); list them here,
#: relative to the package, if one is added. They are hashed by bytes.
DATA_FILES: tuple[str, ...] = ()

_cache: dict[str, str] = {}


# --- normalised hashing -------------------------------------------------------


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _normalised(source: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(source)), include_attributes=False)


# --- import closure -----------------------------------------------------------


def _module_map(root: Path) -> dict[str, Path]:
    """Dotted name relative to the package ("" is the package itself) -> file."""
    mods: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        mods[".".join(parts)] = path
    return mods


def _candidates(target: str, names: Iterable[str]) -> set[str]:
    """*target* with every ancestor, and ``target.name`` for each imported name."""
    out = {target}
    for name in names:
        out.add("%s.%s" % (target, name) if target else name)
    expanded: set[str] = set()
    for dotted in out:
        parts = dotted.split(".") if dotted else []
        for i in range(len(parts) + 1):
            expanded.add(".".join(parts[:i]))
    return expanded


def _strip_package(dotted: str, package: str) -> str | None:
    if dotted == package:
        return ""
    if dotted.startswith(package + "."):
        return dotted[len(package) + 1:]
    return None


def _constant_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _imports(tree: ast.AST, name: str, is_pkg: bool, package: str) -> set[str]:
    """Names of in-package modules *tree* may import, over-approximated."""
    here = name.split(".") if name else []
    base_pkg = here if is_pkg else here[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rel = _strip_package(alias.name, package)
                if rel is not None:
                    found |= _candidates(rel, ())
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names if a.name != "*"]
            if node.level:
                keep = len(base_pkg) - (node.level - 1)
                if keep < 0:
                    continue
                prefix = base_pkg[:keep]
                target = ".".join(prefix + ([node.module] if node.module else []))
            elif node.module:
                rel = _strip_package(node.module, package)
                if rel is None:
                    continue
                target = rel
            else:
                continue
            found |= _candidates(target, names)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called not in ("import_module", "__import__") or not node.args:
                continue
            literal = _constant_str(node.args[0])
            if literal is None:
                continue
            if literal.startswith("."):
                pkg_kw = next((k.value for k in node.keywords if k.arg == "package"), None)
                anchor = _constant_str(pkg_kw)
                if anchor is None:
                    anchor_rel = ".".join(base_pkg)
                else:
                    anchor_rel = _strip_package(anchor, package)
                    if anchor_rel is None:
                        continue
                dots = len(literal) - len(literal.lstrip("."))
                parts = anchor_rel.split(".") if anchor_rel else []
                parts = parts[: len(parts) - (dots - 1)] if dots > 1 else parts
                tail = literal.lstrip(".")
                found |= _candidates(".".join(parts + ([tail] if tail else [])), ())
            else:
                rel = _strip_package(literal, package)
                if rel is not None:
                    found |= _candidates(rel, ())
    return found


def import_closure(
    root: Path, entry_points: Sequence[str] = BUILD_ENTRY_POINTS
) -> dict[str, Path]:
    """Modules reachable from *entry_points*, as ``{dotted name: file}``.

    Raises ``ValueError`` when an entry point does not exist, so a rename
    cannot quietly shrink the closure.
    """
    root = Path(root)
    mods = _module_map(root)
    missing = [e for e in entry_points if e not in mods]
    if missing:
        raise ValueError("entry points not found in %s: %s" % (root, ", ".join(missing)))
    package = root.name
    seen: dict[str, Path] = {}
    stack = list(entry_points)
    while stack:
        name = stack.pop()
        if name in seen or name not in mods:
            continue
        path = mods[name]
        seen[name] = path
        # Importing a.b.c runs a/__init__ and a/b/__init__ first.
        parts = name.split(".") if name else []
        stack.extend(".".join(parts[:i]) for i in range(len(parts)))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack.extend(_imports(tree, name, path.name == "__init__.py", package) - seen.keys())
    return seen


# --- inputs other than the package's own code ----------------------------------


def _dependency_names() -> list[str]:
    names = {n.lower() for n in DEPENDENCIES}
    try:
        for dist in metadata.distributions():
            dist_name = (dist.metadata["Name"] or "").lower().replace("_", "-")
            if dist_name.startswith("tree-sitter"):
                names.add(dist_name)
    except Exception:  # discovery is best effort; the fixed list still applies
        pass
    return sorted(names)


def _dependency_lines() -> list[str]:
    lines = []
    for name in _dependency_names():
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "absent"
        lines.append("dep %s=%s" % (name, version))
    return lines


def _env_lines() -> list[str]:
    return ["env %s=%s" % (name, os.environ.get(name, "")) for name in ENV_VARS]


def _self_lines() -> list[str]:
    try:
        own = _normalised(Path(__file__).read_text(encoding="utf-8"))
    except Exception:
        own = "unreadable"
    return [
        "format %s" % FORMAT_VERSION,
        "python %d.%d" % sys.version_info[:2],
        "self %s" % hashlib.sha256(own.encode()).hexdigest(),
    ]


def _compute(root: Path, entry_points: Sequence[str]) -> str:
    digest = hashlib.sha256()

    def feed(line: str) -> None:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")

    for line in _self_lines() + _dependency_lines() + _env_lines():
        feed(line)
    for name, path in sorted(import_closure(root, entry_points).items()):
        tree_hash = hashlib.sha256(_normalised(path.read_text(encoding="utf-8")).encode())
        feed("module %s %s" % (name or "<package>", tree_hash.hexdigest()))
    for rel in sorted(DATA_FILES):
        feed("data %s %s" % (rel, hashlib.sha256((root / rel).read_bytes()).hexdigest()))
    return digest.hexdigest()[:_HEX_LEN]


def compute_fingerprint(
    package_root: Path | str | None = None,
    entry_points: Sequence[str] | None = None,
    *,
    use_cache: bool = True,
) -> str:
    """Short stable hex fingerprint of the graph-affecting code, or ``"unknown"``.

    Cached per process for the default package. Never raises.
    """
    try:
        default = package_root is None and entry_points is None
        if default and use_cache and "value" in _cache:
            return _cache["value"]
        root = Path(package_root) if package_root is not None else Path(__file__).parent
        value = _compute(root, tuple(entry_points) if entry_points else BUILD_ENTRY_POINTS)
        if default and use_cache:
            _cache["value"] = value
        return value
    except Exception:
        return UNKNOWN


def main() -> int:
    value = compute_fingerprint()
    print(value)
    return 0 if value != UNKNOWN else 1


if __name__ == "__main__":
    sys.exit(main())
