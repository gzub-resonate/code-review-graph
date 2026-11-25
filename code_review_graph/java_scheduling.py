"""Timing of a Spring ``@Scheduled`` annotation, read as written.

The parser keeps every annotation argument as source text. This module adds the structured
view a reader wants: which timing attributes the annotation sets, and for a property
placeholder (``"${jobs.delay:5000}"``) the key and the default written in the placeholder.
A property value is never looked up: the placeholder text and its inline default are all
that is stored.
"""

from __future__ import annotations

import re
from typing import Any

#: Attributes that describe when a schedule fires, in the order they are reported.
TIMING_KEYS: tuple[str, ...] = (
    "cron", "zone", "fixedDelay", "fixedDelayString", "fixedRate", "fixedRateString",
    "initialDelay", "initialDelayString", "timeUnit", "scheduler",
)

_PLACEHOLDER = re.compile(r"\$\{\s*([^:}\s][^:}]*?)\s*(?::([^}]*))?\}")


def parse_placeholder(text: str) -> dict[str, str] | None:
    """``{"key": ..., "default": ...}`` for the first ``${key:default}`` in *text*, else ``None``.

    ``default`` is present only when the placeholder spells one.
    """
    match = _PLACEHOLDER.search(text)
    if match is None:
        return None
    found = {"key": match.group(1)}
    if match.group(2) is not None:
        found["default"] = match.group(2)
    return found


def schedule_timing(attributes: dict[str, str]) -> dict[str, Any]:
    """The timing attributes of one ``@Scheduled`` as written, plus placeholder details.

    Returns ``{"attributes": {name: text}, "placeholders": {name: {"key", "default"?}}}``;
    ``placeholders`` is left out when no value holds one.
    """
    written = {key: attributes[key] for key in TIMING_KEYS if key in attributes}
    placeholders = {
        key: found for key, value in written.items()
        if (found := parse_placeholder(value)) is not None
    }
    timing: dict[str, Any] = {"attributes": written}
    if placeholders:
        timing["placeholders"] = placeholders
    return timing
