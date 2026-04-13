"""Cohort reducer: group session reports by a closed set of dimensions.

The group-by dimension set is deliberately closed (see design decision D4 in
the plan). The five supported dimensions are:

- ``day`` -- ISO date string (``YYYY-MM-DD``)
- ``week`` -- ISO week string (``YYYY-Www``, Monday-anchored per ISO 8601)
- ``project`` -- ``project_hash`` value from the session report
- ``model`` -- ``model`` value from the session report; sessions with
  ``model=None`` are **excluded** from this dimension's output (not imputed)
- ``permission_mode`` -- ``permission_mode`` value from the session report;
  sessions with ``permission_mode=None`` are **excluded** (same policy)

To add a new dimension, add it to :data:`VALID_DIMENSIONS` and add a matching
``_key_<dim>`` function. Do not build a generic "roll up anything" framework --
new dimensions are added only when a future phase explicitly needs them.

Aggregates produced per cell
-----------------------------
For each cohort cell (dimension value x metric name) the reducer computes:

- ``mean`` -- arithmetic mean of the metric values in the cell
- ``stdev`` -- population stdev (0.0 for n=1; uses ``statistics.pstdev``)
- ``n`` -- observation count
- ``min`` / ``max`` -- range

The ``GuardedCell`` / ``CellTooSmall`` contract from :mod:`guards` is enforced
here: callers receive :class:`CohortCell` objects with ``n`` populated so they
can call :func:`~guards.guard_cell` before rendering. The reducer does **not**
call guard_cell itself -- that responsibility belongs to the rendering layer and
the compare path, which may choose different display strategies.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from codevigil.analysis.store import SessionReport

# Closed set of supported group-by dimensions. Resist the urge to extend this
# with a generic registry; add entries only when a future phase needs them.
GroupByDimension = Literal["day", "week", "project", "model", "permission_mode"]

VALID_DIMENSIONS: frozenset[str] = frozenset({"day", "week", "project", "model", "permission_mode"})


@dataclass(frozen=True, slots=True)
class CohortCell:
    """Aggregated statistics for one (dimension_value, metric_name) pair.

    ``n`` is the number of session reports that contributed. Callers must
    route ``n`` through :func:`~guards.guard_cell` before rendering ``mean``
    as a headline number.
    """

    dimension_value: str
    metric_name: str
    mean: float
    stdev: float
    n: int
    min_value: float
    max_value: float


@dataclass(frozen=True, slots=True)
class CohortSlice:
    """All cells produced by a single group-by pass.

    ``dimension`` is the dimension used for grouping.
    ``cells`` is a list of :class:`CohortCell` objects sorted by
    ``(dimension_value, metric_name)``.
    ``session_count`` is the total number of reports that fed the reducer
    (some may have been excluded for null dimension values).
    """

    dimension: str
    cells: list[CohortCell]
    session_count: int
    excluded_null_count: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reduce_by(
    reports: list[SessionReport],
    dimension: GroupByDimension,
) -> CohortSlice:
    """Reduce *reports* into cohort aggregates grouped by *dimension*.

    Sessions with a null value for the requested dimension (``model=None``,
    ``permission_mode=None``) are silently excluded from the output and counted
    in ``CohortSlice.excluded_null_count``. Sessions whose metric dict is empty
    contribute to ``session_count`` but produce no cells.

    Raises:
        ValueError: if *dimension* is not in :data:`VALID_DIMENSIONS`.
    """
    if dimension not in VALID_DIMENSIONS:
        raise ValueError(
            f"unsupported group-by dimension {dimension!r}; supported: {sorted(VALID_DIMENSIONS)}"
        )

    key_fn = _KEY_FNS[dimension]
    # buckets[dim_value][metric_name] = list of float values
    buckets: dict[str, dict[str, list[float]]] = {}
    excluded = 0

    for report in reports:
        dim_value = key_fn(report)
        if dim_value is None:
            excluded += 1
            continue
        if dim_value not in buckets:
            buckets[dim_value] = {}
        for metric_name, metric_value in report.metrics.items():
            if metric_name not in buckets[dim_value]:
                buckets[dim_value][metric_name] = []
            buckets[dim_value][metric_name].append(metric_value)

    cells: list[CohortCell] = []
    for dim_value in sorted(buckets):
        for metric_name in sorted(buckets[dim_value]):
            values = buckets[dim_value][metric_name]
            n = len(values)
            mean = statistics.mean(values)
            stdev = statistics.pstdev(values)
            cells.append(
                CohortCell(
                    dimension_value=dim_value,
                    metric_name=metric_name,
                    mean=mean,
                    stdev=stdev,
                    n=n,
                    min_value=min(values),
                    max_value=max(values),
                )
            )

    return CohortSlice(
        dimension=dimension,
        cells=cells,
        session_count=len(reports),
        excluded_null_count=excluded,
    )


def filter_by_period(
    reports: list[SessionReport],
    *,
    since: date | None = None,
    until: date | None = None,
) -> list[SessionReport]:
    """Return the subset of *reports* whose ``started_at`` falls in the period.

    ``since`` and ``until`` are inclusive. Both are optional; omitting both
    returns all reports. Timezone information is stripped from ``started_at``
    for comparison to avoid ``datetime`` vs naive-``date`` errors -- only the
    calendar date is used for filtering.
    """
    result: list[SessionReport] = []
    for r in reports:
        d = r.started_at.date()
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# Dimension key functions
# ---------------------------------------------------------------------------


def _key_day(report: SessionReport) -> str | None:
    return report.started_at.date().isoformat()


def _key_week(report: SessionReport) -> str | None:
    iso = report.started_at.date().isocalendar()
    # isocalendar() returns IsoCalendarDate(year, week, weekday)
    return f"{iso.year}-W{iso.week:02d}"


def _key_project(report: SessionReport) -> str | None:
    return report.project_hash or None


def _key_model(report: SessionReport) -> str | None:
    return report.model  # None excluded by caller


def _key_permission_mode(report: SessionReport) -> str | None:
    return report.permission_mode  # None excluded by caller


_KeyFn = Callable[[SessionReport], str | None]
_KEY_FNS: dict[str, _KeyFn] = {
    "day": _key_day,
    "week": _key_week,
    "project": _key_project,
    "model": _key_model,
    "permission_mode": _key_permission_mode,
}


__all__ = [
    "VALID_DIMENSIONS",
    "CohortCell",
    "CohortSlice",
    "GroupByDimension",
    "filter_by_period",
    "reduce_by",
]
