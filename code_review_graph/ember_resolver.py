"""Post-build Ember.js resolver.

Ember connects modules by naming convention rather than by imports. The
parser records each such connection as a placeholder (``ember:<kind>:<name>``
edge target, see :mod:`code_review_graph.ember_extract` and
:mod:`code_review_graph.ember_template`); this pass binds the placeholders to
the files and classes the convention names, once every module is known.

What it binds, in this order:

1. **Module-prefix imports.** ``import X from '<app>/services/x'`` names a file
   under ``app/``, ``addon/`` or a dummy app, never a package. A prefix counts
   only when at least two distinct imports under it name files in the
   repository, so ``lodash/get`` is never mistaken for ``app/get.js``.
2. **Service and controller injection** (``INJECTS``): ``@service foo``,
   ``@service('name') foo``, ``foo: service()``, ``Ember.inject.service()``.
   The lookup name is the explicit name or the property name, dasherized.
3. **Calls through an injected property**: ``this.foo.bar()`` and
   ``this.get('foo').bar()`` go to method ``bar`` of the injected service.
4. **Ember Data**: ``store.findRecord('post')`` and ``belongsTo('post')`` go to
   the model class (``REFERENCES``).
5. **Templates**: each component, helper or modifier a ``.hbs`` file invokes
   goes to its file (``IMPORTS_FROM``). A component's class file also gets an
   ``IMPORTS_FROM`` to its template, as Ember's co-location does at build time.
6. **Routes**: each route in ``router.js`` gets ``HANDLES`` edges from the
   route, controller and template modules the naming convention finds.

Conflict rules, applied everywhere a name can match several files:

* The consumer's own app root wins (an app overrides an addon it merges with).
* Otherwise exactly one distinct candidate binds; several do not bind, and the
  edge records ``ember_reason: "ambiguous"``. No candidate records ``"no_module"``
  (usually an addon or a built-in that is not in the repository).
* A file that only re-exports (``export { default } from '...'``) is followed to
  the one module it re-exports when that module is in the repository.
* Every pass starts from the original placeholder, so deleting or adding a
  module changes the result on the next pass instead of leaving a stale bind.

The pass only reads the graph: it never opens a project file, never executes
project code, and cannot leave the repository because every path it uses was
already stored by the parser.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Optional

from .ember_extract import PLACEHOLDER_PREFIX, dasherize
from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_DERIVED = "ember_derived"
_SCRIPT_EXTS = (".js", ".ts", ".gjs", ".gts")
_JS_LANGUAGES = ("javascript", "typescript", "tsx")
_KIND_DIRS = {
    "routes": "route", "controllers": "controller", "components": "component",
    "services": "service", "models": "model", "adapters": "adapter",
    "serializers": "serializer", "helpers": "helper", "modifiers": "modifier",
    "mixins": "mixin", "transforms": "transform", "templates": "template",
}
_POD_STEMS = {
    "route": "route", "controller": "controller", "component": "component",
    "service": "service", "model": "model", "adapter": "adapter",
    "serializer": "serializer", "helper": "helper", "modifier": "modifier",
    "template": "template",
}
_ROOT_RE = re.compile(
    r"^(?P<root>.+?/(?:app|addon))/(?:" + "|".join(_KIND_DIRS) + r"|utils|initializers)/"
)
_MEMBER_CALL_RE = re.compile(r"^this\.([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$")
_MAX_REEXPORT_DEPTH = 3
_TIER_INFERRED, _TIER_UNRESOLVED = "INFERRED", "UNRESOLVED"
_SCORE_INFERRED, _SCORE_UNRESOLVED = 0.8, 0.2


def _loads(raw: Optional[str]) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _strip_ext(name: str) -> str:
    for ext in (".d.ts", *_SCRIPT_EXTS, ".hbs"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", path))


class _ModuleIndex:
    """Files of the repository by Ember role and module name, per app root."""

    def __init__(self, files: set[str], extra_roots: set[str]) -> None:
        roots: set[str] = set(extra_roots)
        for path in files:
            match = _ROOT_RE.match(path)
            if match:
                roots.add(match.group("root"))
        self.roots = sorted(roots, key=lambda r: (-len(r), r))
        self.files = files
        # root -> (kind, name) -> sorted file list
        self.by_root: dict[str, dict[tuple[str, str], list[str]]] = {r: {} for r in self.roots}
        for path in sorted(files):
            root = self.root_of(path)
            if root is None:
                continue
            for key in self._keys(path[len(root) + 1:]):
                self.by_root[root].setdefault(key, []).append(path)

    def root_of(self, path: str) -> Optional[str]:
        for root in self.roots:
            if path.startswith(root + "/"):
                return root
        return None

    @staticmethod
    def _keys(rel: str) -> list[tuple[str, str]]:
        parts = rel.split("/")
        base = _strip_ext(parts[-1])
        is_hbs = parts[-1].endswith(".hbs")
        if not is_hbs and not parts[-1].endswith(_SCRIPT_EXTS):
            return []
        keys: list[tuple[str, str]] = []
        head = parts[0]
        if len(parts) >= 2 and head in _KIND_DIRS:
            kind = _KIND_DIRS[head]
            names = parts[1:-1] + [base]
            if kind == "template" and names[0] == "components":
                keys.append(("component_template", "/".join(names[1:])))
            elif kind == "component":
                if base in ("component", "template", "index") and len(names) > 1:
                    names = names[:-1]
                keys.append(("component_template" if is_hbs else "component", "/".join(names)))
            elif kind == "template":
                keys.append(("template", "/".join(names)))
            elif not is_hbs:
                keys.append((kind, "/".join(names)))
        if len(parts) >= 2 and base in _POD_STEMS and head not in _KIND_DIRS:
            kind = _POD_STEMS[base]
            name = "/".join(parts[:-1])
            if kind == "template" and is_hbs:
                keys.append(("template", name))
            elif kind == "component":
                keys.append(("component", name))
            elif not is_hbs:
                keys.append((kind, name))
        return keys

    def lookup(self, kind: str, name: str, consumer: str) -> tuple[Optional[str], str]:
        """(file, "") when one module is named, else (None, reason)."""
        own = self.root_of(consumer)
        if own is not None:
            hits = self.by_root.get(own, {}).get((kind, name), [])
            if len(hits) == 1:
                return hits[0], ""
            if len(hits) > 1:
                return None, "ambiguous"
        found: list[str] = []
        for root in self.roots:
            if root == own:
                continue
            found.extend(self.by_root[root].get((kind, name), []))
        distinct = sorted(set(found))
        if len(distinct) == 1:
            return distinct[0], ""
        return (None, "ambiguous") if distinct else (None, "no_module")


class _Resolver:
    def __init__(self, store: "GraphStore") -> None:
        self.store = store
        self.conn = store._conn
        self.stats = {
            "imports_bound": 0, "injects_bound": 0, "injects_unresolved": 0,
            "calls_bound": 0, "model_refs_bound": 0, "template_refs_bound": 0,
            "template_refs_unresolved": 0, "route_handles": 0, "component_templates": 0,
        }
        self._class_cache: dict[str, list[str]] = {}
        self._fn_cache: dict[str, list[tuple[str, str, Optional[str]]]] = {}
        self._noop_cache: dict[str, Optional[str]] = {}

    # -- helpers ---------------------------------------------------------
    def _classes(self, path: str) -> list[str]:
        if path not in self._class_cache:
            self._class_cache[path] = [
                r["qualified_name"] for r in self.conn.execute(
                    "SELECT qualified_name FROM nodes WHERE kind = 'Class' AND file_path = ? "
                    "ORDER BY line_start, qualified_name", (path,))
            ]
        return self._class_cache[path]

    def _functions(self, path: str) -> list[tuple[str, str, Optional[str]]]:
        if path not in self._fn_cache:
            self._fn_cache[path] = [
                (r["name"], r["qualified_name"], r["parent_name"]) for r in self.conn.execute(
                    "SELECT name, qualified_name, parent_name FROM nodes "
                    "WHERE kind = 'Function' AND file_path = ? ORDER BY qualified_name", (path,))
            ]
        return self._fn_cache[path]

    def _entity(self, path: str) -> str:
        """The class a module defines when it defines exactly one, else the file."""
        classes = self._classes(path)
        return classes[0] if len(classes) == 1 else path

    def _follow_reexport(self, path: str, reexports: dict[str, list[str]]) -> str:
        for _ in range(_MAX_REEXPORT_DEPTH):
            if self._classes(path) or self._functions(path):
                return path
            targets = reexports.get(path, [])
            if len(targets) != 1:
                return path
            path = targets[0]
        return path

    def _set_edge(self, edge_id: int, target: str, extra: dict, bound: bool) -> None:
        self.conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ?, confidence_tier = ?, "
            "confidence = ? WHERE id = ?",
            (target, json.dumps(extra), _TIER_INFERRED if bound else _TIER_UNRESOLVED,
             _SCORE_INFERRED if bound else _SCORE_UNRESOLVED, edge_id),
        )

    # -- passes ----------------------------------------------------------
    def clear_derived(self) -> None:
        self.conn.execute(
            "DELETE FROM edges WHERE extra LIKE ?", (f'%"{_DERIVED}": true%',))

    def bind_prefix_imports(self, index: _ModuleIndex) -> dict[str, list[str]]:
        """Bind ``<prefix>/<rest>`` imports; return each file's resolved imports."""
        rows = self.conn.execute(
            "SELECT e.id, e.source_qualified, e.target_qualified, e.extra FROM edges e "
            "JOIN nodes n ON n.qualified_name = e.source_qualified AND n.kind = 'File' "
            "WHERE e.kind = 'IMPORTS_FROM' AND n.language IN (?, ?, ?) "
            "AND (e.target_qualified NOT LIKE '/%' OR e.extra LIKE '%\"ember_import_spec\"%') "
            "ORDER BY e.source_qualified, e.line, e.id", _JS_LANGUAGES,
        ).fetchall()
        candidates: list[tuple[int, str, str, dict, str, Optional[str]]] = []
        prefixes: dict[str, set[str]] = {}
        for row in rows:
            extra = _loads(row["extra"])
            spec = extra.get("ember_import_spec") or row["target_qualified"]
            if (spec.startswith((".", "ember:", "@ember/", "@glimmer/")) or _is_absolute(spec)
                    or "/" not in spec):
                continue
            segments = spec.split("/")
            take = 2 if spec.startswith("@") else 1
            if len(segments) <= take:
                continue
            prefix, rest = "/".join(segments[:take]), "/".join(segments[take:])
            hit = self._module_file(index, rest, row["source_qualified"])
            candidates.append((row["id"], row["source_qualified"], spec, extra, prefix, hit))
            if hit:
                prefixes.setdefault(prefix, set()).add(rest)
        accepted = {p for p, rests in prefixes.items() if len(rests) >= 2}
        resolved_by_file: dict[str, list[str]] = {}
        for edge_id, source, spec, extra, prefix, hit in candidates:
            if hit and prefix in accepted:
                extra["ember_import_spec"] = spec
                extra["ember_resolved"] = True
                self._set_edge(edge_id, hit, extra, True)
                resolved_by_file.setdefault(source, []).append(hit)
                self.stats["imports_bound"] += 1
            elif "ember_import_spec" in extra:
                # A module that used to resolve is gone: back to the plain specifier.
                for key in ("ember_import_spec", "ember_resolved"):
                    extra.pop(key, None)
                self.conn.execute(
                    "UPDATE edges SET target_qualified = ?, extra = ?, confidence_tier = "
                    "'EXTRACTED', confidence = 1.0 WHERE id = ?",
                    (spec, json.dumps(extra), edge_id))
        return resolved_by_file

    @staticmethod
    def _module_file(index: _ModuleIndex, rest: str, consumer: str) -> Optional[str]:
        """File a module path ``rest`` names, relative to some app root."""
        own = index.root_of(consumer)
        roots = ([own] if own else []) + [r for r in index.roots if r != own]
        found: list[str] = []
        for root in roots:
            bases = [root]
            if rest.startswith(("config/", "tests/")) and root.endswith("/app"):
                bases.append(str(PurePosixPath(root).parent))
            for base in bases:
                for suffix in (".js", ".ts", ".gjs", ".gts", "/index.js", "/index.ts"):
                    candidate = f"{base}/{rest}{suffix}"
                    if candidate in index.files:
                        found.append(candidate)
                        break
            if found and root == own:
                return found[0]
        distinct = sorted(set(found))
        return distinct[0] if len(distinct) == 1 else None

    def reexports(self, index: _ModuleIndex) -> dict[str, list[str]]:
        rows = self.conn.execute(
            "SELECT e.source_qualified, e.target_qualified FROM edges e "
            "WHERE e.kind = 'IMPORTS_FROM' AND e.target_qualified LIKE '/%' "
            "AND e.source_qualified = e.file_path "
            "AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.file_path = e.file_path "
            "AND n.kind IN ('Class', 'Function')) "
            "ORDER BY e.source_qualified, e.target_qualified").fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_qualified"], []).append(row["target_qualified"])
        return out

    def bind_injections(self, index: _ModuleIndex, reexports: dict[str, list[str]]):
        """Bind INJECTS; return {(file, class or None): {prop: (file, entity)}}."""
        rows = self.conn.execute(
            "SELECT id, source_qualified, target_qualified, extra, file_path FROM edges "
            "WHERE kind = 'INJECTS' AND extra LIKE '%\"framework\": \"ember\"%' "
            "ORDER BY file_path, line, id").fetchall()
        injection_map: dict[tuple[str, Optional[str]], dict[str, tuple[str, str]]] = {}
        for row in rows:
            extra = _loads(row["extra"])
            kind = extra.get("ember_inject")
            raw = extra.get("ember_name")
            prop = extra.get("field_name")
            if kind not in ("service", "controller") or not raw or not prop:
                continue
            name = "/".join(dasherize(part) for part in raw.split("/"))
            consumer = row["file_path"]
            path, reason = index.lookup(kind, name, consumer)
            if path is not None:
                path = self._follow_reexport(path, reexports)
                entity = self._entity(path)
                extra.update({"ember_resolved": True, "ember_resolution": "bound"})
                extra.pop("ember_reason", None)
                self._set_edge(row["id"], entity, extra, True)
                source = row["source_qualified"]
                cls = source.split("::", 1)[1] if "::" in source else None
                injection_map.setdefault((consumer, cls), {})[prop] = (path, entity)
                self.stats["injects_bound"] += 1
            else:
                extra.pop("ember_resolved", None)
                extra.update({"ember_resolution": "unresolved", "ember_reason": reason})
                self._set_edge(row["id"], f"{PLACEHOLDER_PREFIX}{kind}:{raw}", extra, False)
                self.stats["injects_unresolved"] += 1
        return injection_map

    def bind_calls(self, injection_map) -> None:
        files = sorted({key[0] for key in injection_map})
        for start in range(0, len(files), 450):
            batch = files[start:start + 450]
            marks = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                "SELECT id, source_qualified, target_qualified, extra, file_path FROM edges "
                "WHERE kind = 'CALLS' AND file_path IN (" + marks + ") "  # noqa: S608
                "AND (extra LIKE '%\"member_call\": \"this.%' OR extra LIKE '%ember_receiver%') "
                "ORDER BY file_path, line, id", batch).fetchall()
            for row in rows:
                extra = _loads(row["extra"])
                prop = method = None
                if extra.get("ember_receiver"):
                    prop, method = extra["ember_receiver"], extra.get("ember_method")
                else:
                    match = _MEMBER_CALL_RE.match(str(extra.get("member_call", "")))
                    if match:
                        prop, method = match.group(1), match.group(2)
                if not prop or not method:
                    continue
                source = row["source_qualified"]
                symbol = source.split("::", 1)[1] if "::" in source else ""
                parts = symbol.split(".") if symbol else []
                keys: list[tuple[str, Optional[str]]] = [
                    (row["file_path"], ".".join(parts[:k])) for k in range(len(parts) - 1, 0, -1)
                ]
                keys.append((row["file_path"], None))
                bound = next(
                    (injection_map[k][prop] for k in keys
                     if k in injection_map and prop in injection_map[k]), None)
                if bound is None:
                    continue
                svc_file, entity = bound
                target = self._method_target(svc_file, entity, method)
                extra.setdefault("ember_method", method)
                extra.pop("bare_call_target", None)
                extra.update({"ember_resolved": True, "ember_property": prop})
                self.conn.execute(
                    "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
                    (target, json.dumps(extra), row["id"]))
                self.stats["calls_bound"] += 1

    def _method_target(self, svc_file: str, entity: str, method: str) -> str:
        funcs = [f for f in self._functions(svc_file) if f[0] == method]
        if entity != svc_file:
            owner = entity.split("::", 1)[1]
            scoped = [f for f in funcs if f[2] == owner]
        else:
            scoped = [f for f in funcs if not f[2]]
        if len(scoped) == 1:
            return scoped[0][1]
        # Absent or ambiguous: same convention as the Spring resolver, a
        # qualified name that names no node, which stays an unresolved call.
        return f"{entity}.{method}" if entity != svc_file else f"{svc_file}::{method}"

    def bind_placeholders(self, index: _ModuleIndex, reexports: dict[str, list[str]]) -> None:
        rows = self.conn.execute(
            "SELECT id, source_qualified, kind, extra, file_path FROM edges "
            "WHERE kind IN ('REFERENCES', 'IMPORTS_FROM') AND extra LIKE '%\"ember_ref\"%' "
            "ORDER BY file_path, line, id").fetchall()
        for row in rows:
            extra = _loads(row["extra"])
            ref, raw = extra.get("ember_ref"), extra.get("ember_name")
            if not ref or not raw:
                continue
            name = "/".join(dasherize(part) for part in raw.split("/"))
            consumer = row["file_path"]
            if ref == "model":
                path, reason = index.lookup("model", name, consumer)
                target = self._entity(self._follow_reexport(path, reexports)) if path else None
            elif ref == "invocable":
                comp = self._component(index, name, consumer)
                helper = index.lookup("helper", name, consumer)
                if comp[0] and helper[0]:
                    path, reason = None, "ambiguous"
                elif comp[0]:
                    path, reason = comp
                elif helper[0]:
                    path, reason = helper
                else:
                    path, reason = None, ("ambiguous" if "ambiguous" in (comp[1], helper[1])
                                          else "no_module")
                target = path
            elif ref == "component":
                path, reason = self._component(index, name, consumer)
                target = path
            elif ref in ("helper", "modifier"):
                path, reason = index.lookup(ref, name, consumer)
                target = path
            else:
                continue
            was_template = row["kind"] == "IMPORTS_FROM"
            if target:
                extra.update({"ember_resolved": True, "ember_resolution": "bound"})
                extra.pop("ember_reason", None)
                self._set_edge(row["id"], target, extra, True)
                key = "template_refs_bound" if was_template else "model_refs_bound"
                self.stats[key] += 1
            else:
                extra.pop("ember_resolved", None)
                extra.update({"ember_resolution": "unresolved", "ember_reason": reason})
                self._set_edge(row["id"], f"{PLACEHOLDER_PREFIX}{ref}:{raw}", extra, False)
                if was_template:
                    self.stats["template_refs_unresolved"] += 1

    @staticmethod
    def _component(index: _ModuleIndex, name: str, consumer: str) -> tuple[Optional[str], str]:
        path, reason = index.lookup("component", name, consumer)
        if path is not None or reason == "ambiguous":
            return path, reason
        return index.lookup("component_template", name, consumer)

    def link_component_templates(self, index: _ModuleIndex) -> None:
        for root in index.roots:
            table = index.by_root[root]
            for (kind, name), classes in sorted(table.items()):
                if kind != "component" or len(classes) != 1:
                    continue
                templates = table.get(("component_template", name), [])
                if len(templates) != 1 or templates[0] == classes[0]:
                    continue
                self.store.upsert_edge(EdgeInfo(
                    kind="IMPORTS_FROM", source=classes[0], target=templates[0],
                    file_path=classes[0], line=0,
                    extra={"framework": "ember", "ember_link": "colocated_template",
                           _DERIVED: True, "ember_resolved": True,
                           "confidence": _SCORE_INFERRED, "confidence_tier": _TIER_INFERRED},
                ))
                self.stats["component_templates"] += 1

    def link_routes(self, index: _ModuleIndex, reexports: dict[str, list[str]]) -> None:
        rows = self.conn.execute(
            "SELECT qualified_name, file_path, line_start, extra FROM nodes "
            "WHERE kind = 'Endpoint' AND extra LIKE '%\"framework\": \"ember\"%' "
            "ORDER BY file_path, line_start, qualified_name").fetchall()
        for row in rows:
            extra = _loads(row["extra"])
            if extra.get("ember_kind") != "route":
                continue
            name = str(extra.get("route_name", "")).replace(".", "/")
            consumer = row["file_path"]
            for kind, role in (("route", "route"), ("controller", "controller"),
                               ("template", "template")):
                path, _ = index.lookup(kind, name, consumer)
                if path is None:
                    continue
                handler = path if kind == "template" else self._entity(
                    self._follow_reexport(path, reexports))
                self.store.upsert_edge(EdgeInfo(
                    kind="HANDLES", source=handler, target=row["qualified_name"],
                    file_path=consumer, line=row["line_start"] or 0,
                    extra={"framework": "ember", "ember_role": role,
                           "route_name": extra.get("route_name"),
                           _DERIVED: True, "ember_resolved": True,
                           "confidence": _SCORE_INFERRED, "confidence_tier": _TIER_INFERRED},
                ))
                self.stats["route_handles"] += 1


def _has_ember_graph(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM edges WHERE kind = 'IMPORTS_FROM' AND (target_qualified LIKE '@ember/%' "
        "OR target_qualified LIKE 'ember:%' OR extra LIKE '%\"ember_resolved\"%') LIMIT 1"
    ).fetchone()
    return row is not None


def resolve_ember(store: "GraphStore") -> dict:
    """Bind Ember placeholders to modules. Safe to run repeatedly."""
    conn = store._conn
    if not _has_ember_graph(conn):
        return {"ember_files": 0}
    resolver = _Resolver(store)
    resolver.clear_derived()
    files = {
        r["qualified_name"] for r in conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind = 'File' AND language IN (?, ?, ?, ?)",
            (*_JS_LANGUAGES, "handlebars"))
    }
    router_roots = {
        str(PurePosixPath(r["file_path"]).parent) for r in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'Endpoint' "
            "AND extra LIKE '%\"framework\": \"ember\"%'")
    }
    index = _ModuleIndex(files, router_roots)
    resolver.bind_prefix_imports(index)
    reexports = resolver.reexports(index)
    injection_map = resolver.bind_injections(index, reexports)
    resolver.bind_calls(injection_map)
    resolver.bind_placeholders(index, reexports)
    resolver.link_component_templates(index)
    resolver.link_routes(index, reexports)
    store.commit()
    stats = dict(resolver.stats)
    stats["ember_files"] = len(files)
    logger.info("Ember resolver: %s", stats)
    return stats
