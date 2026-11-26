"""One same-file hop from a Java expression to a configuration getter or ``@Value`` field.

An SDK or messaging call usually names its resource through a configuration binding, but not
always directly: ``.queueUrl(getQueueUrl())`` calls an own-class method, ``var bucket =
props.getBucketName()`` goes through a local, ``sqsHelper.getQueueUrl(props.getQueueName())``
through a wrapper, and ``props.getDynamo().getTableName()`` through a nested properties object.
The two Java extractors (``messaging_edges`` and ``aws_sdk_calls``) know how to read the direct
form; :func:`follow` walks up to :data:`MAX_HOPS` such hops and hands each reached expression
back to the extractor's own direct reader, so the binding rules stay in one place.

Everything stays inside the file. A hop that cannot be made with certainty yields nothing:
a local that is assigned twice, declared twice, shadowed by a parameter or declared after
its use; a method that is overloaded, has parameters, or has anything but one ``return``; a
wrapper whose name does not say it looks a resource up. Method parameters are never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any, Optional, TypeVar

__all__ = ["MAX_HOPS", "follow", "getter_chain", "local_alias", "BEAN_GETTER"]

T = TypeVar("T")

#: Hops followed from one expression: ``getQueueUrl()`` -> ``helper.getQueueUrl(x)`` ->
#: ``props.getQueueName()`` is two.
MAX_HOPS = 3
#: Getters in one ``a.getB().getC()`` chain.
_MAX_CHAIN = 4

BEAN_GETTER = re.compile(r"(?:get|is)([A-Z]\w*)")
#: A call that turns a configured name into the resource reference the SDK wants. Anything
#: else (``format``, ``valueOf``, ``Paths.get``) may change what the argument means.
_WRAPPER_NAME = re.compile(
    r"\w*(?:queue|topic|bucket|table|stream|url|arn|function|machine|secret)\w*", re.I,
)
_CLASS_NODES = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
})
_METHOD_NODES = frozenset({"method_declaration", "constructor_declaration"})
_CLASS_BODIES = frozenset({"class_body", "interface_body", "enum_body", "enum_body_declarations"})


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace")


def _walk(node: Any) -> Iterator[Any]:
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _arguments(invocation: Any) -> list[Any]:
    arguments = invocation.child_by_field_name("arguments")
    if arguments is None:
        return []
    return [c for c in arguments.named_children if c.type not in ("comment",)]


def _ancestor(node: Any, types: frozenset[str]) -> Optional[Any]:
    parent = node.parent
    while parent is not None and parent.type not in types:
        parent = parent.parent
    return parent


def _is_own_receiver(invocation: Any) -> bool:
    """``f()`` or ``this.f()``: a call on the class that holds the call site."""
    owner = invocation.child_by_field_name("object")
    return owner is None or owner.type == "this"


# -- The hops -----------------------------------------------------------------------------------


def _binds(node: Any, name: str) -> bool:
    """Whether *node* introduces a variable called *name* other than a plain declarator."""
    kind = node.type
    if kind in ("formal_parameter", "catch_formal_parameter"):
        field = node.child_by_field_name("name")
        return field is not None and _text(field) == name
    if kind == "spread_parameter":
        return any(
            c.type == "variable_declarator" and _text(c.child_by_field_name("name")) == name
            for c in node.children
        )
    if kind == "inferred_parameters":
        return any(c.type == "identifier" and _text(c) == name for c in node.children)
    if kind == "enhanced_for_statement":
        field = node.child_by_field_name("name")
        return field is not None and _text(field) == name
    if kind == "assignment_expression":
        left = node.child_by_field_name("left")
        return left is not None and left.type == "identifier" and _text(left) == name
    if kind in ("update_expression",):
        return any(c.type == "identifier" and _text(c) == name for c in node.named_children)
    return False


def local_alias(identifier: Any) -> Optional[Any]:
    """The initialiser of the one local variable that *identifier* names, or ``None``.

    The variable must be declared once in the method, before the use, with an initialiser,
    and never assigned again or shadowed by a parameter, lambda or loop variable.
    """
    name = _text(identifier)
    method = _ancestor(identifier, _METHOD_NODES)
    if method is None:
        return None
    init = None
    declared = 0
    for node in _walk(method):
        if node.type == "variable_declarator":
            ident = node.child_by_field_name("name")
            if ident is not None and _text(ident) == name:
                parent = node.parent
                declared += 1
                if parent is None or parent.type != "local_variable_declaration":
                    return None
                init = node.child_by_field_name("value")
        elif _binds(node, name):
            return None
    if declared != 1 or init is None or init.start_byte >= identifier.start_byte:
        return None
    return init


def _own_method_return(invocation: Any) -> Optional[Any]:
    """The expression of ``return <expr>;`` in the one no-argument method the call names."""
    if _arguments(invocation) or not _is_own_receiver(invocation):
        return None
    name = invocation.child_by_field_name("name")
    cls = _ancestor(invocation, _CLASS_NODES)
    if name is None or cls is None:
        return None
    body = cls.child_by_field_name("body")
    if body is None:
        return None
    wanted = _text(name)
    found = [
        m for m in body.named_children
        if m.type == "method_declaration"
        and _text(m.child_by_field_name("name")) == wanted
    ]
    if len(found) != 1:
        return None
    method = found[0]
    params = method.child_by_field_name("parameters")
    block = method.child_by_field_name("body")
    if block is None or (params is not None and params.named_child_count):
        return None
    statements = [c for c in block.named_children if c.type not in ("comment",)]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return None
    values = [c for c in statements[0].named_children if c.type != "comment"]
    return values[0] if len(values) == 1 else None


def _wrapper_argument(invocation: Any) -> Optional[Any]:
    """The one argument of ``helper.getQueueUrl(<expr>)``-style resource lookups."""
    name = invocation.child_by_field_name("name")
    args = _arguments(invocation)
    if name is None or len(args) != 1 or not _WRAPPER_NAME.fullmatch(_text(name)):
        return None
    return args[0]


def _next_expression(node: Any) -> Optional[Any]:
    """The expression one hop away from *node*, or ``None``."""
    kind = node.type
    if kind == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        return inner[0] if len(inner) == 1 else None
    if kind == "identifier":
        return local_alias(node)
    if kind == "method_invocation":
        return _own_method_return(node) or _wrapper_argument(node)
    return None


def follow(node: Any, direct: Callable[[Any], Optional[T]]) -> Optional[T]:
    """The first answer of *direct* on an expression reached by hopping from *node*.

    *node* itself is not tried; the caller has already read it. Each step moves through a
    local alias, an own-class method that only returns an expression, or a resource-lookup
    wrapper, at most :data:`MAX_HOPS` times.
    """
    current = node
    for _ in range(MAX_HOPS):
        current = _next_expression(current)
        if current is None:
            return None
        got = direct(current)
        if got is not None:
            return got
    return None


# -- Getter chains --------------------------------------------------------------------------------


def getter_chain(node: Any) -> Optional[tuple[Any, list[str]]]:
    """``(receiver node, [property, ...])`` of ``a.getB().getC()``; ``None`` if not a chain.

    The receiver is whatever the innermost getter is called on. Properties come outermost
    last: ``a.getDynamo().getTableName()`` is ``(a, ["dynamo", "tableName"])``. A single
    getter ``a.getX()`` is a chain of one.
    """
    props: list[str] = []
    current = node
    while current is not None and current.type == "method_invocation":
        if _arguments(current):
            return None
        name = current.child_by_field_name("name")
        owner = current.child_by_field_name("object")
        if name is None or owner is None:
            return None
        match = BEAN_GETTER.fullmatch(_text(name))
        if match is None:
            return None
        prop = match.group(1)
        props.append(prop[0].lower() + prop[1:])
        current = owner
        if len(props) > _MAX_CHAIN:
            return None
        if current.type != "method_invocation":
            props.reverse()
            return current, props
    return None
