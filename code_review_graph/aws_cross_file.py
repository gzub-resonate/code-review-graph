"""Python AWS SDK calls that cross a file boundary: a client factory, and a name passed in.

``aws_sdk_calls`` reads one file at a time. Two shapes need another file of the same repository:

**A client from a factory.** ``_sfn.get_client().start_execution(stateMachineArn=arn)`` makes
no row on its own: the client is built in ``_sfn.py``. The factory is found through the import
(``from . import _sfn``, ``import pkg._sfn as _sfn``, ``from ._sfn import get_client``) to a file
under the repo root, and counts only when every ``return`` of the function is, directly or
through a module-level cache (``_client = None`` plus ``global _client``), a client of one
literal service: ``boto3.client("stepfunctions")``, ``boto3.Session(...).client("sfn")`` or
``make_client("stepfunctions")``. A factory that cannot be found, or whose service is not a
literal, makes no row. The row is ``client_inferred: factory`` at confidence at most 0.5, and
``client_factory`` names the function it was read from (``<repo-relative file>::<function>``).

**A name passed through a parameter.** When the resource argument of such a call is a plain
parameter of its function (``def submit(*, state_machine_arn): ... start_execution(
stateMachineArn=state_machine_arn)``), the row of that call says so (``parameter``,
``parameter_function``, ``unresolved_reason: name_is_parameter``). Each call of that function,
in any file of the repo that imports it (or in the same file), that passes the parameter **by
keyword** gets a row of its own at its own line: the same service, operation and access, and

* ``target_source: parameter``, ``parameter`` and ``bound_via`` (``<file>::<function>(<param>)``)
  say the name was bound through the parameter, and ``arg_form`` how the caller wrote it:
  ``literal`` or ``constant`` (an ARN or name, or a constant of this file or of a module it
  imports), ``default_function`` (a call of a function that takes no argument and returns
  constants, ``default_arn()``), ``or`` (``x or default_arn()``) or ``conditional``;
* the ARN is reduced to the resource name as everywhere else (the account and region are
  dropped), so a development and a production ARN of one machine give one target;
* ``confidence`` 0.4 and tier ``INFERRED`` (0.3 when the name comes from a default function,
  an ``or`` or a conditional); ``override_possible: true`` when some side of an ``or`` or a
  conditional could not be read, so the call may name something else; ``default_from`` names
  the function a default came from; ``target_alternatives`` counts the names when the branches
  differ (one row per name, at most four);
* an argument that cannot be reduced (another parameter, an attribute, a variable, an
  f-string) gives a row to ``?`` with ``unresolved_reason: argument_not_static``.

Only the direct callers of the function are bound (one hop), only a keyword argument whose
name looks like an ARN or a state machine (``*_arn``, ``arn``, ``*state_machine*``) is looked
at, and only a callee that is a plain function named by an import or defined in the same file.
A file is read at most once per parsed file, from inside the repo root, up to a megabyte.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, NamedTuple, Optional

from .aws_sdk_calls import (
    _DYN,
    _READ_PREFIXES,
    _WRITE_PREFIXES,
    Val,
    _Binding,
    _Out,
    _Python,
    _Scopes,
    _text,
    _walk,
    resolve_name,
)

__all__ = ["XFile", "PYTHON_PATTERNS", "ARGUMENT_REASON", "BOUND_SOURCE"]

#: ``unresolved_reason`` of a caller whose argument for the parameter cannot be reduced.
ARGUMENT_REASON = "argument_not_static"
#: ``target_source`` of a row whose name was bound through a parameter.
BOUND_SOURCE = "parameter"
_MAX_FILE = 1_000_000
_ANCESTORS = 6
_MAX_NAMES = 4
_MAX_DEFAULTS = 8
_BOUND_CONFIDENCE = 0.4
_DEFAULT_CONFIDENCE = 0.3
#: A keyword name (lower case) the caller side looks at.
_ARN_KEYWORD = re.compile(r"(?:^|_)arn(?:$|_)|state_?machine")
_SECRET_NAME = re.compile(
    r"SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|CREDENTIAL|PRIVATE|SIGNATURE", re.I,
)
_OPERATION_PREFIXES = "|".join(sorted({
    p.rstrip("_") for p in (*_READ_PREFIXES, *_WRITE_PREFIXES) if "_" not in p.rstrip("_")
}))
#: What makes a Python file worth reading although it names no SDK: a call of a zero-argument
#: function whose result gets an SDK-looking method (``get_client().start_execution(``), and a
#: keyword argument that looks like an ARN (``state_machine_arn=...``).
PYTHON_PATTERNS = (
    re.compile(rb"\w\(\s*\)\s*\.\s*(?:" + _OPERATION_PREFIXES.encode() + rb")_\w+\("),
    re.compile(rb"\b(?:\w*_arn|arn|\w*state_?machine\w*)\s*=(?!=)"),
)


class _Evaluated(NamedTuple):
    names: list[str]
    form: str
    override: bool = False
    default_from: str = ""


class Facts:
    """What one file of the repo says about its functions and constants (parsed once)."""

    def __init__(
        self, owner: XFile, path: Path, src: bytes, root: Any,
    ) -> None:
        self.owner = owner
        self.path = path
        self.src = src
        self.root = root
        self.py = _Python(src, root, None)  # type: ignore[arg-type]
        self._defs: Optional[dict[str, list[Any]]] = None
        self._sinks: Optional[dict[str, list[Any]]] = None

    def function(self, name: str) -> Optional[Any]:
        """The one function of that name in the file (a name defined twice is not used)."""
        if self._defs is None:
            self._defs = {}
            for node in _walk(self.root):
                if node.type == "function_definition":
                    ident = node.child_by_field_name("name")
                    if ident is not None:
                        self._defs.setdefault(_text(ident, self.src), []).append(node)
        found = self._defs.get(name, [])
        return found[0] if len(found) == 1 else None

    def label(self, name: str) -> str:
        return f"{self.owner.relative(self.path)}::{name}"

    def factory(self, name: str) -> Optional[_Binding]:
        func = self.function(name)
        if func is None:
            return None
        got = self.py.returned_service(func)
        if got is None:
            return None
        return _Binding(
            got.kind, got.svc, inferred=True, factory=self.label(name),
        )

    def constant(self, name: str) -> Optional[str]:
        if _SECRET_NAME.search(name):
            return None
        return self.py.consts.get(name)

    def defaults(self, name: str) -> Optional[list[str]]:
        """The strings a function without parameters returns, or ``None`` when any is not one."""
        func = self.function(name)
        if func is None:
            return None
        params = func.child_by_field_name("parameters")
        if params is not None and params.named_child_count:
            return None
        values: list[str] = []
        returns = 0
        for cur in self.py._own_nodes(func):
            if cur.type != "return_statement":
                continue
            returns += 1
            found = self._strings(cur.named_children[0]) if cur.named_child_count else None
            if found is None:
                return None
            values.extend(v for v in found if v not in values)
        if not returns or len(values) > _MAX_DEFAULTS:
            return None
        return values

    def _strings(self, node: Any) -> Optional[list[str]]:
        if node.type == "parenthesized_expression" and node.named_child_count:
            return self._strings(node.named_children[0])
        if node.type == "conditional_expression" and node.named_child_count == 3:
            left = self._strings(node.named_children[0])
            right = self._strings(node.named_children[2])
            if left is None or right is None:
                return None
            return [*left, *(v for v in right if v not in left)]
        if node.type == "identifier" and _SECRET_NAME.search(_text(node, self.src)):
            return None
        val = self.py.val(node)
        if val.template is not None and _DYN not in val.template:
            return [val.template]
        return None

    def sinks(self, name: str) -> list[Any]:
        """The calls inside a function whose resource argument is one of its parameters."""
        if self._sinks is None:
            out = _Out(_Scopes([], [], str(self.path)), lambda **kw: SimpleNamespace(**kw))
            twin = _Python(self.src, self.root, out)
            twin.xfile = self.owner.child(self.path, self.src, self.root)
            twin.run(bind_callers=False)
            self._sinks = {}
            for edge in out.edges:
                function = edge.extra.get("parameter_function")
                if function and edge.extra.get("parameter"):
                    self._sinks.setdefault(function, []).append(edge)
        return self._sinks.get(name, [])


class XFile:
    """The other files of the repo one Python file can see: its imports, resolved to files."""

    def __init__(
        self, path: Path, repo_root: Optional[Path], get_parser: Callable[[str], Any],
        src: bytes, root: Any, shared: Optional[dict[Path, Optional[Facts]]] = None,
    ) -> None:
        self.path = path
        self.get_parser = get_parser
        self.src, self.root = src, root
        self.shared: dict[Path, Optional[Facts]] = shared if shared is not None else {}
        self.repo_root: Optional[Path] = None
        self.here: Optional[Path] = None
        try:
            if repo_root is not None:
                self.repo_root = repo_root.resolve()
                self.here = path.resolve()
                self.here.relative_to(self.repo_root)
        except (OSError, ValueError):
            self.repo_root = self.here = None
        self._modules: dict[str, tuple[str, int]] = {}
        self._symbols: dict[str, tuple[str, int, str]] = {}
        self._read_imports()
        self._files: dict[tuple[str, int], Optional[Path]] = {}
        self._own: Optional[Facts] = None

    # ---- construction helpers
    def child(self, path: Path, src: bytes, root: Any) -> XFile:
        """A resolver for another file of the repo, sharing the files already read."""
        return XFile(path, self.repo_root, self.get_parser, src, root, self.shared)

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root or path).as_posix()
        except (OSError, ValueError):
            return path.name

    # ---- imports
    def _read_imports(self) -> None:
        src = self.src
        for node in _walk(self.root):
            if node.type == "import_statement":
                for item in node.children_by_field_name("name"):
                    if item.type == "aliased_import":
                        name = item.child_by_field_name("name")
                        alias = item.child_by_field_name("alias")
                        if name is not None and alias is not None:
                            self._modules[_text(alias, src)] = (_text(name, src), 0)
                    elif item.type == "dotted_name":
                        dotted = _text(item, src)
                        self._modules[dotted] = (dotted, 0)
            elif node.type == "import_from_statement":
                module = node.child_by_field_name("module_name")
                level, dotted = 0, ""
                if module is not None and module.type == "relative_import":
                    text = _text(module, src)
                    level = len(text) - len(text.lstrip("."))
                    dotted = text.lstrip(".")
                elif module is not None:
                    dotted = _text(module, src)
                for item in node.children_by_field_name("name"):
                    original, local = item, item
                    if item.type == "aliased_import":
                        got = item.child_by_field_name("name")
                        alias = item.child_by_field_name("alias")
                        if got is None or alias is None:
                            continue
                        original, local = got, alias
                    if original.type == "dotted_name":
                        self._symbols[_text(local, src)] = (dotted, level, _text(original, src))

    def module_file(self, dotted: str, level: int) -> Optional[Path]:
        """The file of a module under the repo root: a ``.py`` file or a package ``__init__``."""
        key = (dotted, level)
        if key not in self._files:
            self._files[key] = self._find(dotted, level)
        return self._files[key]

    def _find(self, dotted: str, level: int) -> Optional[Path]:
        if self.repo_root is None or self.here is None:
            return None
        parts = [p for p in dotted.split(".") if p]
        if any(not re.fullmatch(r"\w+", p) for p in parts):
            return None
        bases: list[Path] = []
        if level:
            base = self.here.parent
            for _ in range(level - 1):
                base = base.parent
            bases.append(base)
        else:
            bases.append(self.repo_root)
            ancestor = self.here.parent
            for _ in range(_ANCESTORS):
                if ancestor == self.repo_root or self.repo_root not in ancestor.parents:
                    break
                bases.append(ancestor)
                ancestor = ancestor.parent
        for base in bases:
            stem = base.joinpath(*parts)
            for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                if not parts and candidate.name != "__init__.py":
                    continue
                try:
                    real = candidate.resolve()
                    real.relative_to(self.repo_root)
                    if real.is_file() and real.stat().st_size <= _MAX_FILE:
                        return real
                except (OSError, ValueError):
                    continue
        return None

    # ---- files
    def facts(self, path: Path) -> Optional[Facts]:
        if self.here is not None and path == self.here:
            if self._own is None:
                self._own = Facts(self, path, self.src, self.root)
            return self._own
        if path not in self.shared:
            self.shared[path] = self._load(path)
        return self.shared[path]

    def _load(self, path: Path) -> Optional[Facts]:
        parser = self.get_parser("python")
        if parser is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) > _MAX_FILE:
            return None
        return Facts(self, path, data, parser.parse(data).root_node)

    def resolve_function(self, fn: Any) -> Optional[tuple[Facts, str]]:
        """``(facts of the file, function name)`` for the callee of a call, else ``None``."""
        if self.here is None:
            return None
        text = _text(fn, self.src)
        if fn.type == "identifier":
            symbol = self._symbols.get(text)
            if symbol is None:
                own = self.facts(self.here)
                return (own, text) if own is not None and own.function(text) else None
            module, level, original = symbol
            target = self.module_file(module, level)
            facts = self.facts(target) if target is not None else None
            return (facts, original) if facts is not None else None
        if fn.type != "attribute":
            return None
        recv = fn.child_by_field_name("object")
        attr = fn.child_by_field_name("attribute")
        if recv is None or attr is None:
            return None
        receiver = _text(recv, self.src)
        found: Optional[Path] = None
        if receiver in self._modules:
            found = self.module_file(*self._modules[receiver])
        elif receiver in self._symbols:
            module, level, original = self._symbols[receiver]
            dotted = f"{module}.{original}" if module else original
            found = self.module_file(dotted, level)
        facts = self.facts(found) if found is not None else None
        return (facts, _text(attr, self.src)) if facts is not None else None

    # ---- gap 1: a client from a factory
    def factory_binding(self, fn: Any) -> Optional[_Binding]:
        target = self.resolve_function(fn)
        if target is None:
            return None
        facts, name = target
        return facts.factory(name)

    # ---- gap 2: a name passed through a parameter
    def bind_callers(self, py: _Python) -> None:
        """A row at each call that passes a keyword argument to a function with a sink."""
        for node in _walk(py.root):
            if node.type != "call":
                continue
            _pos, kws, _splat = py._args(node)
            if not any(_ARN_KEYWORD.search(k) for k in kws):
                continue
            fn = node.child_by_field_name("function")
            target = self.resolve_function(fn) if fn is not None else None
            if target is None:
                continue
            facts, name = target
            for sink in facts.sinks(name):
                arg = kws.get(str(sink.extra["parameter"]).lower())
                if arg is not None:
                    self._bind(py, node, arg, facts, name, sink.extra)

    def _bind(
        self, py: _Python, call: Any, arg: Any, facts: Facts, function: str, sink: dict[str, Any],
    ) -> None:
        kind = sink["target_kind"]
        extra: dict[str, Any] = {
            "target_source": BOUND_SOURCE, "parameter": sink["parameter"],
            "bound_via": f"{facts.label(function)}({sink['parameter']})"[:300],
        }
        for field in ("client_inferred", "client_factory"):
            if field in sink:
                extra[field] = sink[field]
        done = self.evaluate(py, arg, kind)
        line = call.start_point[0] + 1
        common = (sink["service"], sink["operation"])
        if done is None or not done.names:
            py.out.add(
                *common, Val(), line, sink.get("sdk", "boto3"), kind=kind,
                access=sink.get("access"), extra={
                    **extra, "unresolved_reason": ARGUMENT_REASON, "confidence": 0.3,
                    "confidence_tier": "INFERRED",
                },
            )
            return
        extra["arg_form"] = done.form
        if done.override:
            extra["override_possible"] = True
        if done.default_from:
            extra["default_from"] = done.default_from[:300]
        if len(done.names) > 1:
            extra["target_alternatives"] = len(done.names)
        direct = done.form in ("literal", "constant") and not done.override
        extra.update(
            confidence=_BOUND_CONFIDENCE if direct else _DEFAULT_CONFIDENCE,
            confidence_tier="INFERRED",
        )
        for name in done.names[:_MAX_NAMES]:
            py.out.add(
                *common, Val(name), line, sink.get("sdk", "boto3"), kind=kind,
                access=sink.get("access"), extra=dict(extra),
            )

    def evaluate(self, py: _Python, node: Any, kind: str) -> Optional[_Evaluated]:
        """The resource names an argument expression stands for, or ``None``.

        Reads a string, a constant of this file or of an imported module, a call of a function
        that takes no argument and returns constants, and ``or`` / conditional expressions of
        those. ``override_possible`` is set when a side of an ``or`` or conditional could not
        be read, so what is named is only the default.
        """
        t = node.type
        if t == "parenthesized_expression" and node.named_child_count:
            return self.evaluate(py, node.named_children[0], kind)
        if t == "boolean_operator":
            op = node.child_by_field_name("operator")
            if op is None or _text(op, py.src) != "or":
                return None
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            return self._combine(
                py, [left, right], kind, "or",
            ) if left is not None and right is not None else None
        if t == "conditional_expression" and node.named_child_count == 3:
            return self._combine(
                py, [node.named_children[0], node.named_children[2]], kind, "conditional",
            )
        if t == "call":
            return self._from_default(py, node, kind)
        if t in ("string", "concatenated_string"):
            return self._reduced(kind, py.val(node).template, "literal")
        if t == "identifier":
            text = _text(node, py.src)
            if _SECRET_NAME.search(text):
                return None
            value = py.consts.get(text)
            if value is not None:
                return self._reduced(kind, value, "constant")
            return self._imported_constant(py, node, kind)
        if t == "attribute":
            return self._imported_constant(py, node, kind)
        return None

    @staticmethod
    def _reduced(kind: str, text: Optional[str], form: str) -> Optional[_Evaluated]:
        if text is None or _DYN in text:
            return None
        name, _placeholder = resolve_name(kind, Val(text))
        return _Evaluated([name], form) if name else None

    def _combine(
        self, py: _Python, nodes: list[Any], kind: str, form: str,
    ) -> Optional[_Evaluated]:
        parts = [self.evaluate(py, n, kind) for n in nodes]
        known = [p for p in parts if p is not None]
        if not known:
            return None
        names: list[str] = []
        for part in known:
            names.extend(n for n in part.names if n not in names)
        return _Evaluated(
            names, form, override=len(known) != len(parts) or any(p.override for p in known),
            default_from=next((p.default_from for p in known if p.default_from), ""),
        )

    def _from_default(self, py: _Python, call: Any, kind: str) -> Optional[_Evaluated]:
        args = call.child_by_field_name("arguments")
        fn = call.child_by_field_name("function")
        if fn is None or (args is not None and args.named_child_count):
            return None
        target = self.resolve_function(fn)
        if target is None:
            return None
        facts, name = target
        values = facts.defaults(name)
        if not values:
            return None
        names: list[str] = []
        for value in values:
            reduced, _placeholder = resolve_name(kind, Val(value))
            if reduced is None:
                return None
            if reduced not in names:
                names.append(reduced)
        return _Evaluated(names, "default_function", default_from=facts.label(name))

    def _imported_constant(self, py: _Python, node: Any, kind: str) -> Optional[_Evaluated]:
        """``NAME`` imported from a repo module, or ``module.NAME``."""
        target: Optional[Path] = None
        name = ""
        if node.type == "identifier":
            symbol = self._symbols.get(_text(node, py.src))
            if symbol is not None:
                target = self.module_file(symbol[0], symbol[1])
                name = symbol[2]
        else:
            recv = node.child_by_field_name("object")
            attr = node.child_by_field_name("attribute")
            if recv is not None and attr is not None:
                receiver = _text(recv, py.src)
                name = _text(attr, py.src)
                if receiver in self._modules:
                    target = self.module_file(*self._modules[receiver])
                elif receiver in self._symbols:
                    module, level, original = self._symbols[receiver]
                    target = self.module_file(f"{module}.{original}" if module else original, level)
        facts = self.facts(target) if target is not None else None
        value = facts.constant(name) if facts is not None else None
        return self._reduced(kind, value, "constant")
