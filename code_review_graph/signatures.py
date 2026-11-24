"""The one-line ``signature`` shown for a function, test or class node."""

from __future__ import annotations

#: Languages that write a function as ``def name(...)``. A node with no language keeps
#: that form, as every signature did before the language was recorded.
_DEF_LANGUAGES = frozenset({"", "python", "ruby"})


def format_signature(
    name: str,
    kind: str,
    params: str | None,
    return_type: str | None,
    language: str | None = None,
) -> str:
    """Return the signature text for a node.

    The parser stores ``params`` as written in the source, so most languages already
    carry their parentheses (``(page, opts)``) and Python's do not (``self, x``); wrap
    only the second form. Only Python-like languages get the ``def`` prefix.
    """
    if kind == "Class":
        return f"class {name}"
    if kind not in ("Function", "Test"):
        return name
    text = params or ""
    if not (text.startswith("(") and text.endswith(")")):
        text = f"({text})"
    prefix = "def " if (language or "") in _DEF_LANGUAGES else ""
    sig = f"{prefix}{name}{text}"
    if return_type:
        sig += f" -> {return_type}"
    return sig
