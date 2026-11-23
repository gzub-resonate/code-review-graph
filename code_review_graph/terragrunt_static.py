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
* Only a string comes out. Values (``locals``, ``inputs``) are evaluated for the one reference
  being resolved and are not kept.

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
MAX_EVAL_DEPTH = 16
MAX_EVAL_NODES = 400
MAX_INCLUDED_FILES = 3
MAX_FILE_BYTES = 1_048_576
MAX_VALUE_CHARS = 4_096
MAX_PARENT_LEVELS = 64

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
            return self._eval(expr, self._entry, 0), None
        except StaticDynamicError as stop:
            return None, stop.reason
        except RecursionError:
            return None, "depth_limit"

    @property
    def entry_dir(self) -> Path:
        """The resolved directory of the file being parsed."""
        return self._entry_path.parent

    # -- evaluation -----------------------------------------------------------------------

    def _eval(self, node: Any, frame: _Frame, depth: int) -> str:
        self._nodes += 1
        if depth > MAX_EVAL_DEPTH or self._nodes > MAX_EVAL_NODES:
            raise StaticDynamicError("depth_limit")
        kind = node.type
        if kind == "expression":
            kids = [c for c in node.named_children if c.type != "comment"]
            if len(kids) == 1:
                return self._eval(kids[0], frame, depth + 1)
            if kids and kids[0].type == "variable_expr":
                return self._chain(kids[0], kids[1:], frame, depth + 1)
            raise StaticDynamicError("unsupported_function")
        if kind == "literal_value":
            lit = node.children[0] if node.children else None
            if lit is not None and lit.type == "string_lit":
                return self._string(lit)
            raise StaticDynamicError("unsupported_function")
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

    def _template(self, node: Any, frame: _Frame, depth: int) -> str:
        out: list[str] = []
        size = 0
        for part in node.children:
            if part.type == "template_literal":
                piece = self._literal_text(_text(part))
            elif part.type == "template_interpolation":
                inner = next((c for c in part.children if c.type == "expression"), None)
                if inner is None:
                    raise StaticDynamicError("unsupported_function")
                piece = self._eval(inner, frame, depth + 1)
            elif part.type == "template_directive":
                raise StaticDynamicError("unsupported_function")
            else:
                continue
            size += len(piece)
            if size > MAX_VALUE_CHARS:
                raise StaticDynamicError("depth_limit")
            out.append(piece)
        return "".join(out)

    def _chain(self, head: Any, rest: list[Any], frame: _Frame, depth: int) -> str:
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

    def _local(self, frame: _Frame, name: str, depth: int) -> str:
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
            value = self._eval(expr, self._entry, depth + 1)
        finally:
            self._active.discard(key)
        return self._existing_file(value, self._entry_path.parent)

    def _include_local(self, name: str, local_name: str, depth: int) -> str:
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

    def _call(self, node: Any, frame: _Frame, depth: int) -> str:
        ident = next((c for c in node.children if c.type == "identifier"), None)
        if ident is None:
            raise StaticDynamicError("unsupported_function")
        name = _text(ident)
        # Nothing about these is evaluated, so no argument is looked at.
        if name == "get_env":
            raise StaticDynamicError("env")
        if name == "run_cmd":
            raise StaticDynamicError("command")
        if name not in _PATH_FUNCTIONS:
            raise StaticDynamicError("unsupported_function")
        arg_node = next((c for c in node.children if c.type == "function_arguments"), None)
        arg_nodes: list[Any] = []
        if arg_node is not None:
            if any(c.type == "..." for c in arg_node.children):
                raise StaticDynamicError("unsupported_function")
            arg_nodes = [c for c in arg_node.children if c.type == "expression"]
        args = [self._eval(a, frame, depth + 1) for a in arg_nodes]
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
        self._need_entry(frame)
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
