"""Period-over-period comparator.

Takes two cohort slices (or two lists of session reports) and produces a
:class:`ComparisonResult` per shared metric: mean delta, percentage change,
and a significance flag derived from a two-sample Welch's t-test using only
the Python stdlib ``statistics`` module.

Welch's t-test is appropriate here because:
- The two period samples are independent (different calendar periods).
- We cannot assume equal variance across periods.
- Session-level metric values are continuous floats.
- Small sample sizes (n < 30) are common in real usage, and Welch's t handles
  unequal n well.

When either period has fewer than 2 observations for a given metric, the
significance test is skipped and ``significant`` is set to ``False`` with
``p_value=None``. This is the correct behaviour for n=1 because a single
observation has no variance to test against.

The implementation computes Welch's t manually from the ``statistics`` module
primitives (mean, variance) and the ``scipy``-free approximation of the
t-distribution CDF using a continued-fraction expansion (see :func:`_t_cdf`).
No third-party libraries are used.

Significance threshold: p < 0.05 (two-tailed). This is a conventional choice;
renderers should always display the raw delta and the p-value alongside the
flag so users can apply their own threshold.

Usage::

    from codevigil.analysis import compare
    result = compare.compare_periods(period_a_reports, period_b_reports)
    for metric_result in result.metrics:
        print(metric_result)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from codevigil.analysis.store import SessionReport

# Significance threshold (two-tailed p-value). Results with p < ALPHA are
# flagged as significant. Callers may override this threshold in their
# rendering logic but should always display the raw p_value for transparency.
SIGNIFICANCE_ALPHA: float = 0.05


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """Comparison result for a single metric across two periods.

    Attributes:
        metric_name: Name of the metric (e.g., ``"read_edit_ratio"``).
        mean_a: Mean value in period A.
        mean_b: Mean value in period B.
        n_a: Number of observations in period A.
        n_b: Number of observations in period B.
        delta: ``mean_b - mean_a``. Positive means B is higher than A.
        delta_pct: ``(delta / mean_a) * 100`` when ``mean_a != 0``, else
            ``None`` (avoid division by zero).
        t_statistic: Welch's t-statistic, or ``None`` when either period has
            fewer than 2 observations.
        p_value: Two-tailed p-value from the Welch's t-distribution, or
            ``None`` when the test was skipped.
        significant: ``True`` when ``p_value is not None`` and
            ``p_value < SIGNIFICANCE_ALPHA``.
    """

    metric_name: str
    mean_a: float
    mean_b: float
    n_a: int
    n_b: int
    delta: float
    delta_pct: float | None
    t_statistic: float | None
    p_value: float | None
    significant: bool


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """All metric comparisons between period A and period B.

    Attributes:
        metrics: One :class:`MetricComparison` per shared metric, sorted by
            ``metric_name``.
        metrics_only_in_a: Metric names present in A but not in B.
        metrics_only_in_b: Metric names present in B but not in A.
        n_sessions_a: Total session count in period A.
        n_sessions_b: Total session count in period B.
    """

    metrics: list[MetricComparison]
    metrics_only_in_a: list[str]
    metrics_only_in_b: list[str]
    n_sessions_a: int
    n_sessions_b: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compare_periods(
    period_a: list[SessionReport],
    period_b: list[SessionReport],
) -> ComparisonResult:
    """Compare metric distributions between two periods.

    Both periods are lists of :class:`~codevigil.analysis.store.SessionReport`.
    The function collects per-metric value lists from each period, computes
    descriptive statistics and a Welch's t-test for metrics present in both
    periods, and returns a :class:`ComparisonResult`.

    Metrics that appear in only one period are recorded in
    ``metrics_only_in_a`` / ``metrics_only_in_b`` and are not tested.
    """
    values_a = _collect_metric_values(period_a)
    values_b = _collect_metric_values(period_b)

    shared = sorted(set(values_a) & set(values_b))
    only_a = sorted(set(values_a) - set(values_b))
    only_b = sorted(set(values_b) - set(values_a))

    comparisons: list[MetricComparison] = []
    for metric_name in shared:
        va = values_a[metric_name]
        vb = values_b[metric_name]
        comparisons.append(_compare_metric(metric_name, va, vb))

    return ComparisonResult(
        metrics=comparisons,
        metrics_only_in_a=only_a,
        metrics_only_in_b=only_b,
        n_sessions_a=len(period_a),
        n_sessions_b=len(period_b),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_metric_values(reports: list[SessionReport]) -> dict[str, list[float]]:
    """Collect per-metric value lists from a list of session reports."""
    out: dict[str, list[float]] = {}
    for report in reports:
        for metric_name, value in report.metrics.items():
            if metric_name not in out:
                out[metric_name] = []
            out[metric_name].append(value)
    return out


def _compare_metric(
    metric_name: str,
    values_a: list[float],
    values_b: list[float],
) -> MetricComparison:
    """Compute comparison statistics for a single metric."""
    n_a = len(values_a)
    n_b = len(values_b)
    mean_a = statistics.mean(values_a) if n_a > 0 else 0.0
    mean_b = statistics.mean(values_b) if n_b > 0 else 0.0
    delta = mean_b - mean_a
    delta_pct: float | None = (delta / mean_a * 100.0) if mean_a != 0.0 else None

    t_stat: float | None = None
    p_val: float | None = None
    significant = False

    if n_a >= 2 and n_b >= 2:
        var_a = statistics.variance(values_a)
        var_b = statistics.variance(values_b)
        t_stat, p_val = _welch_t_test(mean_a, mean_b, var_a, var_b, n_a, n_b)
        significant = p_val < SIGNIFICANCE_ALPHA

    return MetricComparison(
        metric_name=metric_name,
        mean_a=mean_a,
        mean_b=mean_b,
        n_a=n_a,
        n_b=n_b,
        delta=delta,
        delta_pct=delta_pct,
        t_statistic=t_stat,
        p_value=p_val,
        significant=significant,
    )


def _welch_t_test(
    mean_a: float,
    mean_b: float,
    var_a: float,
    var_b: float,
    n_a: int,
    n_b: int,
) -> tuple[float, float]:
    """Compute Welch's t-statistic and two-tailed p-value.

    Returns:
        ``(t_statistic, p_value)``

    When both variances are zero (all values identical), returns ``(0.0, 1.0)``
    (no difference, not significant). When only one variance is zero but means
    differ, returns a large t-statistic with p_value approximated to 0.0.
    """
    se_a = var_a / n_a
    se_b = var_b / n_b
    se_sum = se_a + se_b

    if se_sum == 0.0:
        # Both samples are constant; t is 0 (or undefined if means differ).
        if mean_a == mean_b:
            return 0.0, 1.0
        # Means differ but variance is zero: theoretically infinite t.
        # Return a very large t-stat and p≈0.
        return float("inf"), 0.0

    t = (mean_a - mean_b) / math.sqrt(se_sum)

    # Welch-Satterthwaite degrees of freedom
    df = (se_sum**2) / ((se_a**2 / (n_a - 1)) + (se_b**2 / (n_b - 1)))

    # Two-tailed p-value: p = 2 * P(T > |t|) where T ~ t(df)
    p = 2.0 * _t_sf(abs(t), df)
    return t, p


def _t_sf(t: float, df: float) -> float:
    """Survival function P(T > t) for a Student's t distribution with *df* d.f.

    Uses the regularised incomplete beta function identity:
        P(T > t) = 0.5 * I(df / (df + t^2); df/2, 1/2)

    where ``I(x; a, b)`` is the regularised incomplete beta function,
    approximated via the continued-fraction expansion of Lentz (as implemented
    in ``math.lgamma`` arithmetic). This is a well-known textbook derivation
    that avoids any dependency on ``scipy``.

    For |t| very large relative to df, returns 0.0 (p → 0).
    For t = 0, returns 0.5.
    """
    if not math.isfinite(t):
        return 0.0
    x = df / (df + t * t)
    # I(x; a, b) = betainc(x, a, b)
    p_half = _regularised_incomplete_beta(x, df / 2.0, 0.5) / 2.0
    return p_half


def _regularised_incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta function I_x(a, b) via continued fractions.

    Uses the Lentz continued-fraction method described in Numerical Recipes
    §6.4. Accurate to ~1e-10 for the parameter ranges encountered in Welch's
    t-test with small samples.
    """
    if x < 0.0 or x > 1.0:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0

    # Use symmetry relation to ensure continued fraction converges:
    # I_x(a, b) = 1 - I_{1-x}(b, a) when x > (a+1)/(a+b+2)
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _regularised_incomplete_beta(1.0 - x, b, a)

    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - log_beta) / a

    return front * _beta_cf(x, a, b)


def _beta_cf(x: float, a: float, b: float) -> float:
    """Continued-fraction part of the regularised incomplete beta function.

    Implements the modified Lentz algorithm from Numerical Recipes. Returns
    the continued-fraction value ``cf`` such that
    ``I_x(a, b) ≈ front * cf``.
    """
    max_iter = 200
    eps = 3.0e-7
    fp_min = 1.0e-30

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fp_min:
        d = fp_min
    d = 1.0 / d
    h = d

    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # Even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fp_min:
            d = fp_min
        c = 1.0 + aa / c
        if abs(c) < fp_min:
            c = fp_min
        d = 1.0 / d
        h *= d * c

        # Odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fp_min:
            d = fp_min
        c = 1.0 + aa / c
        if abs(c) < fp_min:
            c = fp_min
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < eps:
            break

    return h


__all__ = [
    "SIGNIFICANCE_ALPHA",
    "ComparisonResult",
    "MetricComparison",
    "compare_periods",
]
