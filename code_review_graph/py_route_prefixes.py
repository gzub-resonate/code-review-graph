"""Join FastAPI/Flask route prefixes across the files of one repository.

:mod:`code_review_graph.py_api_extract` records, per file, the routers it builds
with their own prefix, the ``include_router`` / ``register_blueprint`` calls it
makes and its imports.  A route's full path is the prefix of every router above
it plus its own path, so this module follows the mounts upward from the router
a route is declared on.

Nothing here is guessed.  A router that is never mounted is the root, so its own
prefix is the whole prefix.  A mount whose prefix is not a literal, or a chain
that loops or runs too deep, contributes no route at all.  A router mounted in
two places yields one route per place (at most :data:`MAX_VARIANTS`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from .py_api_extract import join_path

__all__ = ["MAX_VARIANTS", "PrefixResolver", "RouterMap", "module_files"]

MAX_VARIANTS = 8
_MAX_DEPTH = 8


@dataclass(frozen=True)
class RouterMap:
    """What one file says about routers, as ``py_api_extract`` stored it."""

    file: str
    routers: Mapping[str, Sequence[Any]]
    mounts: Sequence[Mapping[str, Any]]
    imports: Mapping[str, Sequence[Any]]


def module_files(files: Iterable[str]) -> dict[str, list[str]]:
    """Index every ``.py`` file by each dotted tail its path could be imported as."""
    index: dict[str, list[str]] = {}
    for file in files:
        if not file.endswith(".py"):
            continue
        parts = file[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for start in range(len(parts)):
            index.setdefault(".".join(parts[start:]), []).append(file)
    return index


def _locate(
    module: str, level: int, here: str, index: Mapping[str, list[str]], known: frozenset[str],
) -> Optional[str]:
    """The file a (possibly relative) import names, or ``None`` when not unique."""
    if level:
        base = here.split("/")[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        tail = module.replace(".", "/") if module else ""
        for candidate in (
            "/".join([*base, tail]) + ".py" if tail else "",
            "/".join([*base, tail, "__init__.py"]) if tail else "/".join([*base, "__init__.py"]),
        ):
            if candidate and candidate in known:
                return candidate
        return None
    options = index.get(module, [])
    if len(options) == 1:
        return options[0]
    if len(options) > 1:
        near = [f for f in options if f.split("/")[0] == here.split("/")[0]]
        return near[0] if len(near) == 1 else None
    return None


class PrefixResolver:
    """Follows mounts upward; ``prefixes(file, var)`` lists a router's full prefixes.

    A ``None`` entry marks a variant that cannot be known; callers drop it.
    """

    def __init__(self, maps: Mapping[str, RouterMap], files: Iterable[str]) -> None:
        self.maps = maps
        self.known = frozenset(files)
        self.index = module_files(self.known)
        #: (file, router var) -> [(mounting file, mounting var, mount prefix or None)]
        self.parents: dict[tuple[str, str], list[tuple[str, str, Optional[str]]]] = {}
        for rmap in maps.values():
            for mount in rmap.mounts:
                child = self._child(rmap, mount.get("child") or {})
                if child is not None:
                    self.parents.setdefault(child, []).append(
                        (rmap.file, str(mount.get("parent")), mount.get("prefix")),
                    )

    def _import_file(self, rmap: RouterMap, local: str) -> Optional[tuple[str, str]]:
        """``(file, name)`` an ``import``-bound local name points at."""
        spec = rmap.imports.get(local)
        if not spec or spec[1] is None:
            return None
        module, name, level = str(spec[0]), str(spec[1]), int(spec[2])
        target = _locate(module, level, rmap.file, self.index, self.known)
        return (target, name) if target else None

    def _child(self, rmap: RouterMap, ref: Mapping[str, str]) -> Optional[tuple[str, str]]:
        if "var" in ref:
            var = ref["var"]
            if var in rmap.routers:
                return rmap.file, var
            return self._import_file(rmap, var)
        obj, attr = ref.get("obj"), ref.get("attr")
        spec = rmap.imports.get(obj or "")
        if not obj or not attr or not spec:
            return None
        module, name, level = str(spec[0]), spec[1], int(spec[2])
        dotted = f"{module}.{name}" if name and module else (name or module)
        target = _locate(str(dotted), level, rmap.file, self.index, self.known)
        if target is None and name is None:
            target = _locate(module, level, rmap.file, self.index, self.known)
        return (target, attr) if target else None

    def _own(self, file: str, var: str) -> tuple[str, str, Optional[str]]:
        """``(file, var, own prefix)``, following an import to where it is built."""
        rmap = self.maps.get(file)
        if rmap is not None and var in rmap.routers:
            prefix = rmap.routers[var][1]
            return file, var, prefix if prefix is None else str(prefix)
        if rmap is not None:
            hop = self._import_file(rmap, var)
            if hop is not None and hop != (file, var):
                target = self.maps.get(hop[0])
                if target is not None and hop[1] in target.routers:
                    prefix = target.routers[hop[1]][1]
                    return hop[0], hop[1], prefix if prefix is None else str(prefix)
        return file, var, ""

    def prefixes(
        self, file: str, var: str, seen: tuple[tuple[str, str], ...] = (),
    ) -> list[Optional[str]]:
        file, var, own = self._own(file, var)
        if own is None:
            return [None]
        if (file, var) in seen or len(seen) >= _MAX_DEPTH:
            return [None]
        mounted = self.parents.get((file, var))
        if not mounted:
            return [join_path(own) if own else ""]
        out: list[Optional[str]] = []
        for parent_file, parent_var, mount_prefix in mounted:
            if mount_prefix is None:
                out.append(None)
                continue
            for above in self.prefixes(parent_file, parent_var, (*seen, (file, var))):
                out.append(None if above is None else join_path(above, mount_prefix, own))
        unique = list(dict.fromkeys(out))
        return unique[:MAX_VARIANTS]
