"""Names of Terraform resources that are not a plain quoted string, read from the files alone.

``aws_terraform_wiring`` and ``aws_sdk_calls`` only know a resource whose name is a literal on a
``resource`` block. This module reduces the rest, and only when every part of the name is known:

* **T1** a name built from ``locals``, ``variable`` defaults, string interpolation, ``lower`` /
  ``upper`` and a ternary on known strings. A variable the deploying Terragrunt unit sets in its
  ``inputs`` (its own or an included file's) takes that value; otherwise its default is used.
* **T2** a name set inside a wrapper module that this file calls with a local ``source``
  (``../../resources/sns``): the module's resource reads ``var.name``, the module block passes
  an argument that is evaluated here.
* **S2** is the same rule for an SNS topic created through such a module (any resource type in
  ``aws_terraform_wiring.DECLARED``, ``DATA_STORES``, ``IDENTITY_TYPES`` (an SSM parameter or an
  IAM role) or ``aws_s3_bucket``).

The expression is reduced by :class:`~code_review_graph.terragrunt_static.TerragruntEvaluator`
in its Terraform mode (nothing is run; ``data.*``, ``each.*``, ``count.*``, module outputs and a
variable nobody sets stay unknown). A name that depends on an unknown value is not written.
When several units deploy the same module (``dev``, ``prod`` ...) every distinct name they give
is a real name of that module, so each one is written.

Bounds: units found per repository, files read per directory, contexts and module blocks per file.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .aws_terraform_wiring import DATA_STORES, DECLARED, IDENTITY_TYPES
from .terragrunt_static import (
    TerragruntEvaluator,
    attribute_expression,
    labelled_blocks,
)

#: Terraform type -> ``(service, target kind, name attribute)`` of what this module can name.
NAMEABLE: dict[str, tuple[str, str, str]] = {
    **DECLARED, **DATA_STORES, **IDENTITY_TYPES, "aws_s3_bucket": ("s3", "bucket", "bucket"),
}

MAX_UNITS = 5_000
MAX_CONTEXTS = 16
MAX_MODULES_PER_FILE = 64
MAX_FILES_PER_DIR = 20
MAX_FILE_BYTES = 262_144
_CACHE_SECONDS = 20.0
_SKIP_DIRS = frozenset({
    ".git", ".terraform", ".terragrunt-cache", "node_modules", ".venv", "venv", "__pycache__",
})
_MODULE_START = re.compile(r'^[ \t]*module[ \t]+"', re.M)
_PLAIN_QUOTED = re.compile(r'"[^"\n$%]*"')

Parse = Callable[[bytes], Optional[Any]]
Lookup = Callable[[str], Optional[str]]

# repo root -> (built at, module directory -> unit files)
_UNIT_CACHE: dict[str, tuple[float, dict[str, list[Path]]]] = {}


def clear_cache() -> None:
    """Forget the unit index (tests, and a long-lived process after files changed)."""
    _UNIT_CACHE.clear()


def wants(source: bytes) -> bool:
    """Cheap test: the file calls a module or holds a nameable resource."""
    text = source.decode("utf-8", "replace")
    if _MODULE_START.search(text):
        return True
    return any(f'"{rtype}"' in text for rtype in NAMEABLE)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read(path: Path) -> Optional[bytes]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _local_source(value: str, base: Path, root: Path) -> Optional[Path]:
    """The directory a local module source names, inside the repository, else ``None``."""
    if "://" in value or value.startswith(("git::", "git@", "tfr:")):
        return None
    text = value.replace("//", "/") if not value.startswith(("./", "../")) else value
    if not text.startswith(("./", "../", "/")) and not os.path.isabs(text):
        return None  # a registry address such as ``org/name/aws``
    resolved = Path(os.path.realpath(text if os.path.isabs(text) else base / text))
    if not _within(resolved, root) or not resolved.is_dir():
        return None
    return resolved


class _Dir:
    """The ``.tf`` files of one directory: trees, variable declarations."""

    def __init__(self, directory: Path, parse: Parse) -> None:
        self.directory = directory
        self.files: list[tuple[Path, Any]] = []
        self.declared: dict[str, tuple[Path, Any, Optional[Any]]] = {}
        names = sorted(p for p in directory.glob("*.tf") if p.is_file())[:MAX_FILES_PER_DIR]
        for path in names:
            data = _read(path)
            tree = parse(data) if data is not None else None
            if tree is None:
                continue
            self.files.append((path, tree))
            for labels, body in labelled_blocks(tree, "variable"):
                if len(labels) == 1 and labels[0] not in self.declared:
                    self.declared[labels[0]] = (
                        path, tree, attribute_expression(body, "default"),
                    )


def _make_evaluator(
    root: Path, path: Path, tree: Any, parse: Parse, variables: Lookup,
) -> TerragruntEvaluator:
    return TerragruntEvaluator(root, path, tree, parse, lambda _p: False, variables=variables)


def _default_lookup(directory: _Dir, root: Path, parse: Parse) -> Lookup:
    """``var.x`` as its ``default``, for a variable that has a plain one."""
    cache: dict[str, Optional[str]] = {}

    def lookup(name: str) -> Optional[str]:
        if name in cache:
            return cache[name]
        cache[name] = None  # a default that refers to itself is unknown
        entry = directory.declared.get(name)
        value: Optional[str] = None
        if entry is not None and entry[2] is not None:
            path, tree, expr = entry
            value = _make_evaluator(root, path, tree, parse, lookup).evaluate(expr)[0]
        cache[name] = value
        return value

    return lookup


def _unit_index(root: Path, parse: Parse) -> dict[str, list[Path]]:
    """Module directory -> the ``terragrunt.hcl`` units whose local ``terraform.source`` is it."""
    key = str(root)
    cached = _UNIT_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    index: dict[str, list[Path]] = {}
    seen = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        if "terragrunt.hcl" not in files:
            continue
        seen += 1
        if seen > MAX_UNITS:
            break
        unit = Path(current) / "terragrunt.hcl"
        data = _read(unit)
        tree = parse(data) if data is not None else None
        if tree is None:
            continue
        for _labels, body in labelled_blocks(tree, "terraform"):
            expr = attribute_expression(body, "source")
            if expr is None:
                continue
            evaluator = TerragruntEvaluator(root, unit, tree, parse, lambda _p: False)
            value, _reason = evaluator.evaluate(expr)
            target = _local_source(value, unit.parent, root) if value else None
            if target is not None:
                index.setdefault(str(target), []).append(unit)
    _UNIT_CACHE[key] = (now, index)
    return index


def _contexts(
    directory: _Dir, root: Path, parse: Parse,
) -> list[Lookup]:
    """One ``var.x`` lookup per deploying unit, or the defaults alone when none deploys it.

    An input the unit sets wins over the default. An input that is set but does not reduce to
    a string is unknown: the default is not used, since it is not what the unit passes.
    """
    defaults = _default_lookup(directory, root, parse)
    units = _unit_index(root, parse).get(str(directory.directory), [])[:MAX_CONTEXTS]
    if not units:
        return [defaults]
    lookups: list[Lookup] = []
    for unit in units:
        data = _read(unit)
        tree = parse(data) if data is not None else None
        if tree is None:
            continue
        unit_eval = TerragruntEvaluator(root, unit, tree, parse, lambda _p: False)

        def lookup(name: str, _eval: TerragruntEvaluator = unit_eval) -> Optional[str]:
            if name not in directory.declared:
                return None
            value, _reason, found = _eval.input_value(name)
            return value if found else defaults(name)

        lookups.append(lookup)
    return lookups or [defaults]


def _sites(tree: Any) -> list[tuple[str, str, Any, int]]:
    """``(type, label, name expression, line)`` of every nameable resource whose name is written."""
    found: list[tuple[str, str, Any, int]] = []
    for labels, body in labelled_blocks(tree, "resource"):
        if len(labels) != 2 or labels[0] not in NAMEABLE:
            continue
        expr = attribute_expression(body, NAMEABLE[labels[0]][2])
        if expr is not None:
            found.append((labels[0], labels[1], expr, body.start_point[0] + 1))
    return found


def _is_plain_literal(expr: Any) -> bool:
    return _PLAIN_QUOTED.fullmatch(expr.text.decode("utf-8", "replace").strip()) is not None


def scan(
    path: Path,
    source: bytes,
    repo_root: Optional[Path],
    parse: Parse,
    out: Any,
    val: Callable[..., Any],
    name_ok: Callable[[str, str], bool],
    max_edges: int,
) -> None:
    """Write a ``declare`` edge for each name of this file that can be proven (see module doc)."""
    if repo_root is None:
        return
    root = Path(os.path.realpath(repo_root))
    entry = Path(os.path.realpath(path if path.is_absolute() else root / path))
    if not _within(entry, root) or entry.suffix != ".tf":
        return
    tree = parse(source)
    if tree is None:
        return
    directory = _Dir(entry.parent, parse)
    here = [(p, t) for p, t in directory.files if p == entry]
    if not here:
        directory.files.append((entry, tree))
    direct = [s for s in _sites(tree) if not _is_plain_literal(s[2])]
    modules: list[tuple[str, int, _Dir, dict[str, Any]]] = []
    for labels, body in labelled_blocks(tree, "module")[:MAX_MODULES_PER_FILE]:
        src = attribute_expression(body, "source")
        if not labels or src is None or not _is_plain_literal(src):
            continue
        target = _local_source(src.text.decode("utf-8", "replace").strip().strip('"'),
                               entry.parent, root)
        if target is None or target == entry.parent:
            continue
        callee = _Dir(target, parse)
        if not any(_sites(t) for _p, t in callee.files):
            continue
        args = {
            _name(a): e for a in body.children if a.type == "attribute"
            for e in [next((c for c in a.children if c.type == "expression"), None)]
            if e is not None and _name(a) not in ("source", "providers", "count", "for_each")
        }
        modules.append((labels[0], body.start_point[0] + 1, callee, args))
    if not direct and not modules:
        return
    found: dict[tuple[str, str, str, int], set[str]] = {}
    for lookup in _contexts(directory, root, parse):
        ev = _make_evaluator(root, entry, tree, parse, lookup)
        for rtype, label, expr, line in direct:
            value, _reason = ev.evaluate(expr)
            _keep(found, (rtype, label, "", line), value, name_ok)
        for mlabel, mline, callee, args in modules:
            callee_default = _default_lookup(callee, root, parse)

            def into(name: str, _args: dict[str, Any] = args, _ev: TerragruntEvaluator = ev,
                     _d: Lookup = callee_default) -> Optional[str]:
                if name in _args:
                    return _ev.evaluate(_args[name])[0]
                return _d(name)

            for cpath, ctree in callee.files:
                cev = _make_evaluator(root, cpath, ctree, parse, into)
                for rtype, label, expr, _cline in _sites(ctree):
                    value, _reason = cev.evaluate(expr)
                    _keep(found, (rtype, label, mlabel, mline), value, name_ok)
    for (rtype, label, mlabel, line), names in sorted(
        found.items(), key=lambda kv: (kv[0][3], kv[0][2], kv[0][0], kv[0][1]),
    ):
        service, kind, _attr = NAMEABLE[rtype]
        extra: dict[str, Any] = {"terraform_type": rtype, "name_evaluated": True}
        if mlabel:
            extra["terraform_module"] = mlabel
        elif len(names) == 1:
            extra["terraform_label"] = label
        for name in sorted(names):
            if len(out.edges) >= max_edges:
                return
            source_scope = out.scopes.resource(rtype, label) if not mlabel else None
            out.add(service, "declare", val(name), line, "terraform", kind=kind,
                    access="declare", source=source_scope, extra=dict(extra))


def _name(attribute: Any) -> str:
    ident = next((c for c in attribute.children if c.type == "identifier"), None)
    return ident.text.decode("utf-8", "replace") if ident is not None else ""


def _keep(
    found: dict[tuple[str, str, str, int], set[str]],
    key: tuple[str, str, str, int], value: Optional[str], name_ok: Callable[[str, str], bool],
) -> None:
    if not value or "\x00" in value:
        return
    kind = NAMEABLE[key[0]][1]
    if name_ok(kind, value):
        found.setdefault(key, set()).add(value)
