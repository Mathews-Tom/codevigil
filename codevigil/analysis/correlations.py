"""Experimental Pearson correlation helper for session-report metrics.

Computes pairwise Pearson correlation between metric columns extracted
from a list of :class:`~codevigil.analysis.store.SessionReport` objects.
This is gated as **experimental** because:

1. Per-session metric values are heteroscedastic and not normally
   distributed; Pearson assumes both. Use as exploratory signal only.
2. A high correlation across sessions is *not* causal evidence — the
   project's claim discipline (see ``docs/cohort_report_enhancement_plan.md``
   §4) explicitly bans causal interpretation.
3. The minimum pair count guard (``MIN_PAIRS = 30``) drops correlations
   computed from samples too small to be informative; below the floor
   the entry is omitted from output rather than reported as ``None``.

Output is a list of :class:`MetricCorrelation` records sorted by
``abs(r)`` descending so the strongest signals surface first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

from codevigil.analysis.store import SessionReport

MIN_PAIRS: int = 30


@dataclass(frozen=True, slots=True)
class MetricCorrelation:
    """Pearson correlation between two metric columns.

    Attributes:
        metric_a: First metric name (alphabetically lower).
        metric_b: Second metric name.
        r: Pearson correlation coefficient in [-1.0, 1.0].
        n: Number of sessions where both metrics were present.
    """

    metric_a: str
    metric_b: str
    r: float
    n: int


def compute_correlations(
    reports: list[SessionReport],
    *,
    min_pairs: int = MIN_PAIRS,
) -> list[MetricCorrelation]:
    """Return pairwise Pearson correlations across all metric columns.

    Sessions missing a metric are excluded from that pair's calculation,
    not from the entire run — different pairs may have different ``n``.
    Pairs with fewer than ``min_pairs`` joint observations are omitted.
    Pairs with zero variance in either column are also omitted (Pearson
    is undefined when one variable is constant).
    """
    if not reports:
        return []

    columns: dict[str, list[tuple[int, float]]] = {}
    for idx, report in enumerate(reports):
        for metric_name, value in report.metrics.items():
            columns.setdefault(metric_name, []).append((idx, float(value)))

    metric_names = sorted(columns.keys())
    out: list[MetricCorrelation] = []
    for a, b in combinations(metric_names, 2):
        joint = _join_columns(columns[a], columns[b])
        if len(joint) < min_pairs:
            continue
        r = _pearson(joint)
        if r is None:
            continue
        out.append(MetricCorrelation(metric_a=a, metric_b=b, r=r, n=len(joint)))

    out.sort(key=lambda mc: abs(mc.r), reverse=True)
    return out


def _join_columns(
    col_a: list[tuple[int, float]],
    col_b: list[tuple[int, float]],
) -> list[tuple[float, float]]:
    map_b = dict(col_b)
    return [(va, map_b[i]) for i, va in col_a if i in map_b]


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    syy = sum((y - mean_y) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


__all__ = ["MIN_PAIRS", "MetricCorrelation", "compute_correlations"]
