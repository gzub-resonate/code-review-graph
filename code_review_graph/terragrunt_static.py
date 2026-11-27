"""Static evaluation of the expressions a Terragrunt reference is written as.

``terraform.source``, ``include.path``, ``dependency.config_path`` and ``dependencies.paths``
are usually not literals: ``find_in_parent_folders()``, ``"${get_parent_terragrunt_dir()}/m"``,
``"${include.root.locals.base}//x"``. This module reduces such an expression to a string by
reading the files, never by running anything. The caller turns that string into a graph
target; the string itself is used once and never stored.

Safety rules, all enforced here:

* Nothing is executed. ``get_env``, ``run_cmd``, ``sops_*`` and every function not listed in
  :data:`_PATH_FUNCTIONS` stop the evaluation. No environment variable is read, no process is
  started and nothing touches the network.
* Only files inside the repository are looked at: the search for a parent file and every
  included file must resolve (symbolic links included) below the repository root and must not
  be an excluded file.
* Work is bounded: expression size, nesting depth, evaluated nodes, files read through
  ``include`` and bytes per file. Past a bound, or on a cycle, the result is dynamic.
* Only a string comes out. Values (``locals``) are evaluated for the one reference being
  resolved and are not kept. Lists and whole numbers exist only between the pure functions
  that take them apart (``split``, ``slice``, ``join``, ``length``, an index); an expression
  whose result is not a string is dynamic.
* ``inputs`` is read only through :meth:`TerragruntEvaluator.input_value` and
  :meth:`TerragruntEvaluator.input_entries`, and only for a key the caller names. Every other
  key of ``inputs`` is skipped without its value being looked at. The caller keeps an
  allowlist of keys (see ``infra_references.TERRAGRUNT_INPUT_KEYS``) and reduces what comes
  back to naming data before anything is stored.

A failed evaluation carries one reason from :data:`DYNAMIC_REASONS` and no text.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

#: Why a reference stays dynamic. ``env``: reads an environment variable (``get_env``).
#: ``command``: runs a command (``run_cmd``). ``variable``: depends on a value that is not
#: known from the files (``var.*``, ``each.*``, ``dependency`` outputs, a conditional).
#: ``unsupported_function``: any other function or construct. ``depth_limit``: a bound was
#: reached, or the expression is cyclic. ``not_found``: a file, include or local it needs is
#: missing, outside the repository or excluded.
DYNAMIC_REASONS = (
    "env", "command", "variable", "unsupported_function", "depth_limit", "not_found",
)

MAX_EXPRESSION_BYTES = 4_096
MAX_EVAL_DEPTH = 40
MAX_EVAL_NODES = 400
MAX_INCLUDED_FILES = 3
MAX_FILE_BYTES = 1_048_576
MAX_VALUE_CHARS = 4_096
MAX_PARENT_LEVELS = 64
MAX_LIST_ITEMS = 256
MAX_INPUT_ENTRIES = 500

#: Stands for an interpolation that could not be reduced, in :meth:`input_entries` values.
#: The caller must refuse a value whose name part holds one.
HOLE = "\x00"

_VARIABLE_ROOTS = frozenset({
    "var", "each", "count", "values", "dependency", "dependencies", "module", "data",
    "feature", "terraform", "path", "self",
})


class StaticDynamicError(Exception):
    """The expression cannot be reduced statically. ``reason`` is in :data:`DYNAMIC_REASONS`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace")


def _children(node: Any, *types: str) -> list[Any]:
    return [c for c in node.children if c.type in types]


def _attribute_expression(body: Any, name: str) -> Optional[Any]:
    """The expression node of attribute *name* directly in *body*, else ``None``."""
    for attr in body.children:
        if attr.type != "attribute":
            continue
        key = next((c for c in attr.children if c.type == "identifier"), None)
        expr = next((c for c in attr.children if c.type == "expression"), None)
        if key is not None and expr is not None and _text(key) == name:
            return expr
    return None


def _top_level_blocks(root: Any, block_type: str) -> list[tuple[str, Any]]:
    """``(label, body)`` of every top-level block of *block_type*, in file order."""
    body = next((c for c in root.children if c.type == "body"), None)
    found: list[tuple[str, Any]] = []
    if body is None:
        return found
    for block in body.children:
        if block.type != "block" or not block.children:
            continue
        if block.children[0].type != "identifier" or _text(block.children[0]) != block_type:
            continue
        inner = next((c for c in block.children if c.type == "body"), None)
        if inner is None:
            continue
        label = ""
        for child in block.children:
            if child.type == "string_lit":
                label = "".join(_text(t) for t in child.children if t.type == "template_literal")
                break
        found.append((label, inner))
    return found


@dataclass
class _Frame:
    """One file whose locals can be read: the file being parsed, or a file it includes."""

    path: Path
    root: Any
    entry: bool
    locals_map: Optional[dict[str, Optional[Any]]] = None
    inputs_map: Optional[dict[str, Any]] = None


class TerragruntEvaluator:
    """Reduce expressions of one Terragrunt file to strings. Pure: reads files, runs nothing.

    *repo_root* is the resolved repository root. *file_path* is the file being parsed and
    *root_node* its tree. *parse* turns the bytes of an included file into a tree root (or
    ``None``). *is_excluded* says whether a resolved file must be left out of the graph.
    """

    def __init__(
        self,
        repo_root: Path,
        file_path: Path,
        root_node: Any,
        parse: Callable[[bytes], Optional[Any]],
        is_excluded: Callable[[Path], bool],
    ) -> None:
        self._root = repo_root
        self._parse = parse
        self._is_excluded = is_excluded
        absolute = file_path if file_path.is_absolute() else repo_root / file_path
        self._entry_path = Path(os.path.realpath(absolute))
        self._entry = _Frame(self._entry_path, root_node, True)
        self._entry_is_unit = self._entry_path.name == "terragrunt.hcl"
        self._nodes = 0
        self._files = 0
        self._active: set[tuple[str, str]] = set()
        self._include_frames: Optional[list[tuple[_Frame, bool]]] = None
        self._include_reason: Optional[str] = None

    # -- public ---------------------------------------------------------------------------

    def evaluate(self, expr: Any) -> tuple[Optional[str], Optional[str]]:
        """``(value, None)`` when *expr* reduces to a string, else ``(None, reason)``."""
        self._nodes = 0
        self._files = 0
        self._active = set()
        try:
            if not self._within(self._entry_path):
                raise StaticDynamicError("not_found")
            if expr.end_byte - expr.start_byte > MAX_EXPRESSION_BYTES:
                raise StaticDynamicError("depth_limit")
            return self._as_string(self._eval(expr, self._entry, 0)), None
        except StaticDynamicError as stop:
            return None, stop.reason
        except RecursionError:
            return None, "depth_limit"

    def input_keys(self) -> frozenset[str]:
        """The keys of the ``inputs`` of this unit and of the files it includes. No value."""
        keys: set[str] = set()
        for frame in self._input_frames():
            keys.update(self._inputs(frame))
        return frozenset(keys)

    def is_deployable(self) -> bool:
        """Whether this file includes another or has a ``terraform`` block: a unit, not a base."""
        return bool(
            _top_level_blocks(self._entry.root, "include")
            or _top_level_blocks(self._entry.root, "terraform")
        )

    def input_value(self, key: str) -> tuple[Optional[str], Optional[str], bool]:
        """``(value, reason, found)`` of ``inputs.<key>`` of this unit.

        The unit's own ``inputs`` is looked at first, then the ``inputs`` of each file it
        includes from the last ``include`` to the first, as Terragrunt merges them (the unit
        wins, then a later include wins over an earlier one; a ``no_merge`` include is not
        read). Only the element named *key* is
        evaluated; no other element's value is visited. *found* is false when no file
        defines the key, and *reason* is then ``None`` unless the includes could not be read.
        """
        for frame in self._input_frames():
            expr = self._inputs(frame).get(key)
            if expr is None:
                continue
            value, reason = self._evaluate_in(expr, frame)
            return value, reason, True
        return None, self._include_reason, False

    def input_entries(self, key: str) -> tuple[list[tuple[str, Optional[str], int]], Optional[str]]:
        """``([(entry key, value, line)], reason)`` of the map ``inputs.<key>`` of this unit.

        The map is an object written out (or a ``merge(...)`` of such objects). An entry of
        the unit replaces the entry of an included file with the same key; an included file
        adds entries only when the unit does not define the map, or the include asks for a
        ``deep`` merge. A value that does not reduce to a string is ``None``, except that a
        quoted template keeps its literal text with :data:`HOLE` for each interpolation that
        could not be reduced. *reason* is set when the includes could not all be read.
        """
        merged: dict[str, tuple[Optional[str], int]] = {}
        defined = False
        for frame, deep in self._input_frames_with_merge():
            expr = self._inputs(frame).get(key)
            if expr is None:
                continue
            if defined and not deep:
                continue
            defined = True
            for name, value_expr in self._object_entries(expr):
                if name in merged or len(merged) >= MAX_INPUT_ENTRIES:
                    continue
                value, _reason = self._evaluate_in(value_expr, frame, holes=True)
                merged[name] = (value, value_expr.start_point[0] + 1)
        return [(k, v[0], v[1]) for k, v in merged.items()], self._include_reason

    def input_objects(
        self, key: str, fields: frozenset[str],
    ) -> tuple[list[dict[str, Optional[str]]], Optional[str]]:
        """``([{field: value}], reason)`` for the list of objects ``inputs.<key>``.

        The list is written out in the unit (or in the first included file that defines it).
        Only the entries named in *fields* are evaluated; every other entry of an object (a
        policy, an ``input`` document, an environment) is never visited. A value that does not
        reduce to a string is ``None``.
        """
        for frame in self._input_frames():
            expr = self._inputs(frame).get(key)
            if expr is None:
                continue
            out: list[dict[str, Optional[str]]] = []
            for item in self._list_items(expr)[:MAX_LIST_ITEMS]:
                row: dict[str, Optional[str]] = {}
                for name, value_expr in self._object_entries(item):
                    if name in fields and name not in row:
                        flag = self._flag(value_expr)
                        row[name] = flag if flag is not None else (
                            self._evaluate_in(value_expr, frame)[0]
                        )
                out.append(row)
            return out, self._include_reason
        return [], self._include_reason

    @staticmethod
    def _flag(expr: Any) -> Optional[str]:
        """``"true"`` or ``"false"`` for an expression that is exactly that literal."""
        text = _text(expr).strip()
        return text if text in ("true", "false") else None

    def input_flag(self, key: str) -> Optional[str]:
        """``"true"``/``"false"`` when ``inputs.<key>`` is exactly that literal, else ``None``."""
        for frame in self._input_frames():
            expr = self._inputs(frame).get(key)
            if expr is not None:
                return self._flag(expr)
        return None

    def input_strings(self, key: str) -> tuple[list[Optional[str]], Optional[str]]:
        """``([value], reason)`` for the list of strings ``inputs.<key>``."""
        for frame in self._input_frames():
            expr = self._inputs(frame).get(key)
            if expr is None:
                continue
            items = self._list_items(expr)[:MAX_LIST_ITEMS]
            return [self._evaluate_in(i, frame)[0] for i in items], self._include_reason
        return [], self._include_reason

    @staticmethod
    def _list_items(expr: Any) -> list[Any]:
        """The element expressions of a list written out (``[a, b]``); nothing otherwise."""
        kids = [c for c in expr.named_children if c.type != "comment"]
        if len(kids) != 1 or kids[0].type != "collection_value":
            return []
        tup = next((c for c in kids[0].children if c.type == "tuple"), None)
        return [c for c in tup.children if c.type == "expression"] if tup is not None else []

    def _evaluate_in(
        self, expr: Any, frame: _Frame, holes: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        self._nodes = 0
        self._files = 0
        self._active = set()
        try:
            if expr.end_byte - expr.start_byte > MAX_EXPRESSION_BYTES:
                raise StaticDynamicError("depth_limit")
            if holes:
                template = self._quoted_template(expr)
                if template is not None:
                    return self._template(template, frame, 1, holes=True), None
            return self._as_string(self._eval(expr, frame, 0)), None
        except StaticDynamicError as stop:
            return None, stop.reason
        except RecursionError:
            return None, "depth_limit"

    @staticmethod
    def _quoted_template(expr: Any) -> Optional[Any]:
        kids = [c for c in expr.named_children if c.type != "comment"]
        if len(kids) != 1 or kids[0].type != "template_expr" or not kids[0].children:
            return None
        tpl = kids[0].children[0]
        return tpl if tpl.type == "quoted_template" else None

    def _input_frames(self) -> list[_Frame]:
        return [frame for frame, _deep in self._input_frames_with_merge()]

    def _input_frames_with_merge(self) -> list[tuple[_Frame, bool]]:
        """The unit and the files merged into it, highest precedence first, with deep or not."""
        if self._include_frames is None:
            included: list[tuple[_Frame, bool]] = []
            self._include_reason = None
            blocks = _top_level_blocks(self._entry.root, "include")
            if len(blocks) > MAX_INCLUDED_FILES:
                # More files than one unit may read: nothing is guessed from a part of them.
                self._include_reason = "depth_limit"
                blocks = []
            for label, body in blocks:
                self._nodes = 0
                self._active = set()
                try:
                    path = self._include_file(label or None, 0)
                    strategy = _attribute_expression(body, "merge_strategy")
                    mode = _text(strategy).strip() if strategy is not None else ""
                    frame = self._read_frame(path)
                    if mode != '"no_merge"':
                        included.append((frame, mode == '"deep"'))
                except StaticDynamicError as stop:
                    self._include_reason = stop.reason
                except RecursionError:
                    self._include_reason = "depth_limit"
            # Highest precedence first: the unit, then the includes from the last to the first
            # (a later ``include`` wins). ``no_merge`` merges nothing, so that file is absent.
            self._include_frames = [(self._entry, True), *reversed(included)]
        return self._include_frames

    def _read_frame(self, path: Path) -> _Frame:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise StaticDynamicError("depth_limit")
            data = path.read_bytes()
        except OSError:
            raise StaticDynamicError("not_found") from None
        tree = self._parse(data)
        if tree is None:
            raise StaticDynamicError("not_found")
        return _Frame(path, tree, False)

    def _inputs(self, frame: _Frame) -> dict[str, Any]:
        """Key -> expression of the top-level ``inputs`` object of *frame*. No value is read."""
        if frame.inputs_map is None:
            table: dict[str, Any] = {}
            body = next((c for c in frame.root.children if c.type == "body"), None)
            expr = _attribute_expression(body, "inputs") if body is not None else None
            if expr is not None:
                for name, value_expr in self._object_entries(expr):
                    table.setdefault(name, value_expr)
            frame.inputs_map = table
        return frame.inputs_map

    def _object_entries(self, expr: Any) -> list[tuple[str, Any]]:
        """``(key, value expression)`` of an object written out, or of ``merge(objects)``.

        Later arguments of ``merge`` win, as in Terraform. An argument that is not an object
        written out contributes nothing.
        """
        kids = [c for c in expr.named_children if c.type != "comment"]
        if len(kids) != 1:
            return []
        node = kids[0]
        if node.type == "function_call":
            ident = next((c for c in node.children if c.type == "identifier"), None)
            args = next((c for c in node.children if c.type == "function_arguments"), None)
            if ident is None or args is None or _text(ident) != "merge":
                return []
            found: dict[str, Any] = {}
            for arg in [c for c in args.children if c.type == "expression"][:MAX_LIST_ITEMS]:
                for name, value_expr in self._object_entries(arg):
                    found[name] = value_expr
            return list(found.items())
        if node.type != "collection_value":
            return []
        obj = next((c for c in node.children if c.type == "object"), None)
        if obj is None:
            return []
        out: list[tuple[str, Any]] = []
        for elem in obj.children:
            if elem.type != "object_elem":
                continue
            exprs = [c for c in elem.children if c.type == "expression"]
            if len(exprs) != 2:
                continue
            name = self._object_key(exprs[0])
            if name is not None:
                out.append((name, exprs[1]))
        return out

    @staticmethod
    def _object_key(expr: Any) -> Optional[str]:
        kids = [c for c in expr.named_children if c.type != "comment"]
        if len(kids) != 1:
            return None
        node = kids[0]
        if node.type == "variable_expr":
            return _text(node)
        if node.type == "literal_value" and node.children:
            lit = node.children[0]
            if lit.type == "string_lit":
                return "".join(_text(t) for t in lit.children if t.type == "template_literal")
        return None

    @staticmethod
    def _as_string(value: Any) -> str:
        if not isinstance(value, str):
            raise StaticDynamicError("unsupported_function")
        return value

    @property
    def entry_dir(self) -> Path:
        """The resolved directory of the file being parsed."""
        return self._entry_path.parent

    # -- evaluation -----------------------------------------------------------------------

    def _eval(self, node: Any, frame: _Frame, depth: int) -> Any:
        """The value of *node*: a string, or a list of strings or a whole number in passing."""
        self._nodes += 1
        if depth > MAX_EVAL_DEPTH or self._nodes > MAX_EVAL_NODES:
            raise StaticDynamicError("depth_limit")
        kind = node.type
        if kind == "expression":
            kids = [c for c in node.named_children if c.type != "comment"]
            if len(kids) == 1:
                return self._eval(kids[0], frame, depth + 1)
            if not kids:
                raise StaticDynamicError("unsupported_function")
            # ``base[index]``: the indexes come last and each takes one item of a list.
            cut = len(kids)
            while cut > 1 and kids[cut - 1].type == "index":
                cut -= 1
            if kids[0].type == "variable_expr":
                value = self._chain(kids[0], kids[1:cut], frame, depth + 1)
            elif cut == 1:
                value = self._eval(kids[0], frame, depth + 1)
            else:
                raise StaticDynamicError("unsupported_function")
            for index in kids[cut:]:
                value = self._index(value, index, frame, depth + 1)
            return value
        if kind == "literal_value":
            lit = node.children[0] if node.children else None
            if lit is not None and lit.type == "string_lit":
                return self._string(lit)
            if lit is not None and lit.type == "numeric_lit" and _text(lit).isdigit():
                return int(_text(lit))
            raise StaticDynamicError("unsupported_function")
        if kind == "operation":
            inner = node.named_children[0] if node.named_children else None
            if inner is None or inner.type != "binary_operation" or len(inner.children) != 3:
                raise StaticDynamicError("unsupported_function")
            left = self._eval(inner.children[0], frame, depth + 1)
            right = self._eval(inner.children[2], frame, depth + 1)
            sign = inner.children[1].type
            if (
                sign not in ("+", "-")
                or isinstance(left, bool) or isinstance(right, bool)
                or not isinstance(left, int) or not isinstance(right, int)
            ):
                raise StaticDynamicError("unsupported_function")
            return left + right if sign == "+" else left - right
        if kind == "template_expr":
            tpl = node.children[0] if node.children else None
            if tpl is not None and tpl.type == "quoted_template":
                return self._template(tpl, frame, depth + 1)
            raise StaticDynamicError("unsupported_function")
        if kind == "function_call":
            return self._call(node, frame, depth + 1)
        if kind in ("variable_expr", "conditional"):
            raise StaticDynamicError("variable")
        raise StaticDynamicError("unsupported_function")

    def _index(self, value: Any, index: Any, frame: _Frame, depth: int) -> Any:
        inner = next((c for c in index.children if c.type == "new_index"), None)
        expr = (
            next((c for c in inner.children if c.type == "expression"), None)
            if inner is not None else None
        )
        if expr is None or not isinstance(value, list):
            raise StaticDynamicError("unsupported_function")
        position = self._eval(expr, frame, depth + 1)
        if isinstance(position, bool) or not isinstance(position, int):
            raise StaticDynamicError("unsupported_function")
        if not 0 <= position < len(value):
            raise StaticDynamicError("not_found")
        return value[position]

    @staticmethod
    def _literal_text(text: str) -> str:
        # ``$${`` and ``%%{`` are escapes, a backslash is an escape in some positions.
        # None of them occurs in a path worth resolving, so none is interpreted.
        if "\\" in text or "$$" in text or "%%" in text:
            raise StaticDynamicError("unsupported_function")
        return text

    def _string(self, node: Any) -> str:
        return "".join(
            self._literal_text(_text(t)) for t in node.children if t.type == "template_literal"
        )

    def _template(self, node: Any, frame: _Frame, depth: int, holes: bool = False) -> str:
        out: list[str] = []
        size = 0
        for part in node.children:
            if part.type == "template_literal":
                piece = self._literal_text(_text(part))
            elif part.type == "template_interpolation":
                inner = next((c for c in part.children if c.type == "expression"), None)
                if inner is None:
                    raise StaticDynamicError("unsupported_function")
                if holes:
                    try:
                        piece = self._piece(self._eval(inner, frame, depth + 1))
                    except StaticDynamicError:
                        piece = HOLE
                else:
                    piece = self._piece(self._eval(inner, frame, depth + 1))
            elif part.type == "template_directive":
                raise StaticDynamicError("unsupported_function")
            else:
                continue
            size += len(piece)
            if size > MAX_VALUE_CHARS:
                raise StaticDynamicError("depth_limit")
            out.append(piece)
        return "".join(out)

    @staticmethod
    def _piece(value: Any) -> str:
        """What an interpolated value adds to a string: itself, or a whole number's digits."""
        if isinstance(value, str):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        raise StaticDynamicError("unsupported_function")

    def _chain(self, head: Any, rest: list[Any], frame: _Frame, depth: int) -> Any:
        root = _text(head)
        names: list[str] = []
        for accessor in rest:
            if accessor.type != "get_attr" or not accessor.named_children:
                raise StaticDynamicError("unsupported_function")
            names.append(_text(accessor.named_children[-1]))
        if root == "local":
            if len(names) != 1:
                raise StaticDynamicError("unsupported_function")
            return self._local(frame, names[0], depth)
        if root == "include":
            if len(names) != 3 or names[1] != "locals":
                raise StaticDynamicError("unsupported_function")
            return self._include_local(names[0], names[2], depth)
        if root in _VARIABLE_ROOTS:
            raise StaticDynamicError("variable")
        raise StaticDynamicError("unsupported_function")

    # -- locals and includes --------------------------------------------------------------

    def _locals(self, frame: _Frame) -> dict[str, Optional[Any]]:
        if frame.locals_map is None:
            table: dict[str, Optional[Any]] = {}
            for _label, body in _top_level_blocks(frame.root, "locals"):
                for attr in body.children:
                    if attr.type != "attribute":
                        continue
                    key = next((c for c in attr.children if c.type == "identifier"), None)
                    expr = next((c for c in attr.children if c.type == "expression"), None)
                    if key is None or expr is None:
                        continue
                    name = _text(key)
                    # A name defined twice is an error in Terragrunt; it is not guessed.
                    table[name] = None if name in table else expr
            frame.locals_map = table
        return frame.locals_map

    def _local(self, frame: _Frame, name: str, depth: int) -> Any:
        expr = self._locals(frame).get(name)
        if expr is None:
            raise StaticDynamicError("not_found")
        key = (str(frame.path), "local." + name)
        if key in self._active:
            raise StaticDynamicError("depth_limit")
        self._active.add(key)
        try:
            return self._eval(expr, frame, depth + 1)
        finally:
            self._active.discard(key)

    def _include_block(self, name: Optional[str]) -> tuple[str, Any]:
        blocks = _top_level_blocks(self._entry.root, "include")
        if name is None:
            chosen = next((b for b in blocks if b[0] == ""), None)
            if chosen is None and len(blocks) == 1:
                chosen = blocks[0]
            if chosen is None:
                raise StaticDynamicError("unsupported_function")
            return chosen
        chosen = next((b for b in blocks if b[0] == name), None)
        if chosen is None:
            raise StaticDynamicError("not_found")
        return chosen

    def _include_file(self, name: Optional[str], depth: int) -> Path:
        label, body = self._include_block(name)
        expr = _attribute_expression(body, "path")
        if expr is None:
            raise StaticDynamicError("not_found")
        key = (str(self._entry_path), "include." + label)
        if key in self._active:
            raise StaticDynamicError("depth_limit")
        self._active.add(key)
        try:
            value = self._as_string(self._eval(expr, self._entry, depth + 1))
        finally:
            self._active.discard(key)
        return self._existing_file(value, self._entry_path.parent)

    def _include_local(self, name: str, local_name: str, depth: int) -> Any:
        label, body = self._include_block(name)
        expose = _attribute_expression(body, "expose")
        if expose is None or _text(expose).strip() != "true":
            raise StaticDynamicError("not_found")
        path = self._include_file(label, depth + 1)
        self._files += 1
        if self._files > MAX_INCLUDED_FILES:
            raise StaticDynamicError("depth_limit")
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise StaticDynamicError("depth_limit")
            data = path.read_bytes()
        except OSError:
            raise StaticDynamicError("not_found") from None
        tree = self._parse(data)
        if tree is None:
            raise StaticDynamicError("not_found")
        return self._local(_Frame(path, tree, False), local_name, depth + 1)

    # -- paths ----------------------------------------------------------------------------

    def _within(self, path: Path) -> bool:
        try:
            path.relative_to(self._root)
        except ValueError:
            return False
        return True

    def _existing_file(self, value: str, base: Path) -> Path:
        """The resolved in-repository, non-excluded file *value* names, else not found."""
        if not value or "\x00" in value:
            raise StaticDynamicError("not_found")
        candidate = Path(value) if os.path.isabs(value) else base / value
        try:
            resolved = Path(os.path.realpath(candidate))
            if not self._within(resolved) or not resolved.is_file():
                raise StaticDynamicError("not_found")
        except (OSError, ValueError):
            raise StaticDynamicError("not_found") from None
        if self._is_excluded(resolved):
            raise StaticDynamicError("not_found")
        return resolved

    def _find_in_parent(self, fname: str) -> str:
        pure = posixpath.normpath(fname.replace("\\", "/"))
        if (
            not fname
            or posixpath.isabs(pure)
            or pure == ".."
            or pure.startswith("../")
            or "\x00" in fname
        ):
            raise StaticDynamicError("unsupported_function")
        current = self._entry_path.parent
        for _ in range(MAX_PARENT_LEVELS):
            if current == self._root or not self._within(current):
                raise StaticDynamicError("not_found")
            current = current.parent
            try:
                return self._existing_file(pure, current).as_posix()
            except StaticDynamicError as stop:
                if stop.reason != "not_found":
                    raise
            if current == self._root:
                break
        raise StaticDynamicError("not_found")

    def _need_unit(self) -> None:
        # ``get_terragrunt_dir()`` and friends name the unit being run. A file that is not
        # itself a unit (``terragrunt.hcl``) has no such directory without its includer.
        if not self._entry_is_unit:
            raise StaticDynamicError("unsupported_function")

    def _need_entry(self, frame: _Frame) -> None:
        if not frame.entry:
            raise StaticDynamicError("unsupported_function")

    # -- functions ------------------------------------------------------------------------

    def _call(self, node: Any, frame: _Frame, depth: int) -> Any:
        ident = next((c for c in node.children if c.type == "identifier"), None)
        if ident is None:
            raise StaticDynamicError("unsupported_function")
        name = _text(ident)
        # Nothing about these is evaluated, so no argument is looked at.
        if name == "get_env":
            raise StaticDynamicError("env")
        if name == "run_cmd":
            raise StaticDynamicError("command")
        if name not in _PATH_FUNCTIONS and name not in _VALUE_FUNCTIONS:
            raise StaticDynamicError("unsupported_function")
        arg_node = next((c for c in node.children if c.type == "function_arguments"), None)
        arg_nodes: list[Any] = []
        if arg_node is not None:
            if any(c.type == "..." for c in arg_node.children):
                raise StaticDynamicError("unsupported_function")
            arg_nodes = [c for c in arg_node.children if c.type == "expression"]
        args = [self._eval(a, frame, depth + 1) for a in arg_nodes]
        if name in _VALUE_FUNCTIONS:
            return _VALUE_FUNCTIONS[name](args)
        if not all(isinstance(a, str) for a in args):
            raise StaticDynamicError("unsupported_function")
        return _PATH_FUNCTIONS[name](self, frame, args, depth + 1)

    def _fn_find_in_parent_folders(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_entry(frame)
        if len(args) > 1:
            raise StaticDynamicError("unsupported_function")
        return self._find_in_parent(args[0] if args else "terragrunt.hcl")

    def _fn_unit_dir(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_unit()
        if args:
            raise StaticDynamicError("unsupported_function")
        return self._entry_path.parent.as_posix()

    def _fn_parent_dir(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_unit()
        self._need_entry(frame)
        if len(args) > 1:
            raise StaticDynamicError("unsupported_function")
        return self._include_file(args[0] if args else None, depth).parent.as_posix()

    def _fn_repo_root(self, frame: _Frame, args: list[str], depth: int) -> str:
        if args:
            raise StaticDynamicError("unsupported_function")
        return self._root.as_posix()

    def _fn_path_to_repo_root(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_unit()
        self._need_entry(frame)
        if args:
            raise StaticDynamicError("unsupported_function")
        return Path(os.path.relpath(self._root, self._entry_path.parent)).as_posix()

    def _fn_relative_to_include(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_unit()
        if not frame.entry:
            # Inside an included file the function names the unit that includes it.
            if args:
                raise StaticDynamicError("unsupported_function")
            return Path(os.path.relpath(self._entry_path.parent, frame.path.parent)).as_posix()
        if len(args) > 1:
            raise StaticDynamicError("unsupported_function")
        parent = self._include_file(args[0] if args else None, depth).parent
        return Path(os.path.relpath(self._entry_path.parent, parent)).as_posix()

    def _fn_relative_from_include(self, frame: _Frame, args: list[str], depth: int) -> str:
        self._need_unit()
        self._need_entry(frame)
        if len(args) > 1:
            raise StaticDynamicError("unsupported_function")
        parent = self._include_file(args[0] if args else None, depth).parent
        return Path(os.path.relpath(parent, self._entry_path.parent)).as_posix()

    def _fn_dirname(self, frame: _Frame, args: list[str], depth: int) -> str:
        if len(args) != 1:
            raise StaticDynamicError("unsupported_function")
        return posixpath.dirname(args[0].replace("\\", "/")) or "."


def _vf_split(args: list[Any]) -> list[str]:
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise StaticDynamicError("unsupported_function")
    if not args[0]:
        raise StaticDynamicError("unsupported_function")
    parts = args[1].split(args[0])
    if len(parts) > MAX_LIST_ITEMS:
        raise StaticDynamicError("depth_limit")
    return parts


def _vf_join(args: list[Any]) -> str:
    if (
        len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], list)
        or not all(isinstance(item, str) for item in args[1])
    ):
        raise StaticDynamicError("unsupported_function")
    joined = args[0].join(args[1])
    if len(joined) > MAX_VALUE_CHARS:
        raise StaticDynamicError("depth_limit")
    return joined


def _vf_slice(args: list[Any]) -> list[str]:
    if (
        len(args) != 3 or not isinstance(args[0], list)
        or any(isinstance(a, bool) or not isinstance(a, int) for a in args[1:])
    ):
        raise StaticDynamicError("unsupported_function")
    start, end = args[1], args[2]
    if not 0 <= start <= end <= len(args[0]):
        raise StaticDynamicError("not_found")
    return args[0][start:end]


def _vf_length(args: list[Any]) -> int:
    if len(args) != 1 or not isinstance(args[0], (list, str)):
        raise StaticDynamicError("unsupported_function")
    return len(args[0])


def _vf_basename(args: list[Any]) -> str:
    if len(args) != 1 or not isinstance(args[0], str):
        raise StaticDynamicError("unsupported_function")
    return posixpath.basename(args[0].replace("\\", "/").rstrip("/"))


#: Pure functions of their arguments: no file, no environment, nothing run.
_VALUE_FUNCTIONS: dict[str, Callable[[list[Any]], Any]] = {
    "split": _vf_split,
    "join": _vf_join,
    "slice": _vf_slice,
    "length": _vf_length,
    "basename": _vf_basename,
}

#: Functions whose value follows from the file path, the repository root and the files.
_PATH_FUNCTIONS: dict[str, Callable[..., str]] = {
    "find_in_parent_folders": TerragruntEvaluator._fn_find_in_parent_folders,
    "get_terragrunt_dir": TerragruntEvaluator._fn_unit_dir,
    "get_original_terragrunt_dir": TerragruntEvaluator._fn_unit_dir,
    "get_parent_terragrunt_dir": TerragruntEvaluator._fn_parent_dir,
    "get_repo_root": TerragruntEvaluator._fn_repo_root,
    "get_path_to_repo_root": TerragruntEvaluator._fn_path_to_repo_root,
    "path_relative_to_include": TerragruntEvaluator._fn_relative_to_include,
    "path_relative_from_include": TerragruntEvaluator._fn_relative_from_include,
    "dirname": TerragruntEvaluator._fn_dirname,
}
