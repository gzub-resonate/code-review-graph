"""The time budget of a cross-repo call, checkable from inside one repo's work.

``CRG_CROSS_REPO_BUDGET_SECONDS`` used to be read only before a repo was started, so one big
repo (an index build over every graph, a long join) could run well past it. A
:class:`Deadline` is cheap to ask, takes the clock as an argument so a test can drive it, and
:meth:`Deadline.check` raises :class:`BudgetSpent`, which the caller turns into "this repo is
skipped, repeat the call": what was read stays in the per-repo caches, so the repeat goes on
from there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional


class BudgetSpent(Exception):  # noqa: N818 - a signal, not an error
    """The call's time budget ran out in the middle of a step."""


class Deadline:
    """A point in time on *clock*, or no limit (``budget`` of ``0`` or less)."""

    __slots__ = ("_clock", "at")

    def __init__(self, budget: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.at: Optional[float] = clock() + budget if budget > 0 else None

    def spent(self) -> bool:
        return self.at is not None and self._clock() >= self.at

    def remaining(self) -> Optional[float]:
        """Seconds left (never below ``0``), or ``None`` when there is no limit."""
        return None if self.at is None else max(0.0, self.at - self._clock())

    def check(self) -> None:
        if self.spent():
            raise BudgetSpent
