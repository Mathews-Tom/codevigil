"""Sample-size and observation-span guards.

These guards implement the claim-discipline rule from the design: a cohort cell
with fewer than ``MIN_CELL_N`` observations must never be rendered as a headline
number. Instead it is rendered as the sentinel string ``"n<5"`` (or the
configured minimum). Callers that receive ``CellTooSmall`` instead of a float
must propagate the sentinel rather than substituting a computed value.

Span guards enforce a minimum observation window. Any period shorter than the
configured ``min_observation_days`` is dropped from cohort output with a logged
reason rather than producing a misleading one-day aggregate.

Neither guard modifies data. They classify inputs and raise or return sentinel
values that the caller is responsible for propagating.

Responsibilities of callers:
- :mod:`cohort` — call :func:`guard_cell` on each aggregated cell before
  returning it to the renderer or compare path.
- :mod:`compare` — call :func:`guard_cell` on the inputs before computing
  deltas; also call :func:`guard_span` to reject undersized periods.
- Renderers (Phase 4+) — substitute ``"n<5"`` for ``CellTooSmall`` in all
  headline-number positions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)

# Minimum number of observations before a cell is allowed to render as a
# headline number. Hard-coded to 5 as required by Phase 3 spec. Future phases
# that need a different floor should route through this constant, not embed
# a literal in their own rendering code.
MIN_CELL_N: int = 5

# Default minimum observation span in days. Any period that covers fewer days
# than this is treated as too short to draw conclusions from. Configurable via
# the ``[storage] min_observation_days`` config key.
DEFAULT_MIN_OBSERVATION_DAYS: int = 1


class CellTooSmall(Exception):
    """Raised when a cohort cell has too few observations to display.

    Attributes:
        n: The actual number of observations in the cell.
        min_n: The minimum required (:data:`MIN_CELL_N`).
        sentinel: The string that renderers should display in place of the
            numeric value, e.g. ``"n<5"``.
    """

    def __init__(self, n: int, *, min_n: int = MIN_CELL_N) -> None:
        self.n: int = n
        self.min_n: int = min_n
        self.sentinel: str = f"n<{min_n}"
        msg = f"cell has only {n} observation(s); minimum is {min_n} -> show {self.sentinel!r}"
        super().__init__(msg)


class SpanTooShort(Exception):
    """Raised when a period is shorter than the minimum observation window.

    Attributes:
        actual_days: The number of days in the period.
        min_days: The minimum required.
        reason: Human-readable explanation suitable for a log message.
    """

    def __init__(self, actual_days: float, *, min_days: int, label: str = "") -> None:
        self.actual_days: float = actual_days
        self.min_days: int = min_days
        context = f" ({label})" if label else ""
        self.reason: str = (
            f"period{context} spans {actual_days:.1f} day(s); minimum is {min_days} — dropping"
        )
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class GuardedCell:
    """Result of a successful :func:`guard_cell` check.

    When the guard passes, callers use ``value`` and ``n`` directly. When it
    raises :exc:`CellTooSmall`, callers use :attr:`CellTooSmall.sentinel`.
    """

    value: float
    n: int


def guard_cell(value: float, n: int, *, min_n: int = MIN_CELL_N) -> GuardedCell:
    """Assert that a cohort cell has enough observations to display.

    Raises:
        CellTooSmall: when ``n < min_n``.

    Returns:
        :class:`GuardedCell` with the validated ``value`` and ``n``.
    """
    if n < min_n:
        raise CellTooSmall(n, min_n=min_n)
    return GuardedCell(value=value, n=n)


def guard_span(
    actual_days: float,
    *,
    min_days: int = DEFAULT_MIN_OBSERVATION_DAYS,
    label: str = "",
) -> None:
    """Assert that an observation period is long enough to include.

    Logs a WARNING with the reason and raises :exc:`SpanTooShort` so the
    caller can skip the period rather than silently producing a misleading
    aggregate.

    Raises:
        SpanTooShort: when ``actual_days < min_days``.
    """
    if actual_days < min_days:
        exc = SpanTooShort(actual_days, min_days=min_days, label=label)
        _LOG.warning("dropping period: %s", exc.reason)
        raise exc


def cell_sentinel(min_n: int = MIN_CELL_N) -> str:
    """Return the display sentinel string for a too-small cell.

    Convenience for renderers that need the sentinel without raising.
    """
    return f"n<{min_n}"


__all__ = [
    "DEFAULT_MIN_OBSERVATION_DAYS",
    "MIN_CELL_N",
    "CellTooSmall",
    "GuardedCell",
    "SpanTooShort",
    "cell_sentinel",
    "guard_cell",
    "guard_span",
]
