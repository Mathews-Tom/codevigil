"""Unit tests for codevigil.analysis.compare.

Covers period-over-period comparison on synthetic session fixtures, including
Welch's t-test correctness, edge cases (n=0, n=1, zero variance, empty
metrics), and the 100-session stability test required by the phase spec.

The comparator is a critical-path component per test-standards.md.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from codevigil.analysis.compare import (
    SIGNIFICANCE_ALPHA,
    ComparisonResult,
    _beta_cf,
    _collect_metric_values,
    _compare_metric,
    _regularised_incomplete_beta,
    _t_sf,
    _welch_t_test,
    compare_periods,
)
from codevigil.analysis.store import SessionReport, build_report

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _report(
    session_id: str,
    *,
    started_at: datetime | None = None,
    metrics: dict[str, float] | None = None,
) -> SessionReport:
    t0 = started_at or _EPOCH
    return build_report(
        session_id=session_id,
        project_hash="proj",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=t0,
        ended_at=t0 + timedelta(minutes=30),
        event_count=10,
        parse_confidence=0.95,
        metrics=metrics or {},
    )


def _period(n: int, *, metric_value: float, metric_name: str = "m") -> list[SessionReport]:
    return [_report(f"s{i}", metrics={metric_name: metric_value + i * 0.01}) for i in range(n)]


# ---------------------------------------------------------------------------
# _collect_metric_values
# ---------------------------------------------------------------------------


def test_collect_metric_values_empty() -> None:
    assert _collect_metric_values([]) == {}


def test_collect_metric_values_single_report() -> None:
    r = _report("x", metrics={"a": 1.0, "b": 2.0})
    result = _collect_metric_values([r])
    assert result == {"a": [1.0], "b": [2.0]}


def test_collect_metric_values_multiple_reports() -> None:
    r1 = _report("x", metrics={"m": 1.0})
    r2 = _report("y", metrics={"m": 3.0})
    result = _collect_metric_values([r1, r2])
    assert result == {"m": [1.0, 3.0]}


def test_collect_metric_values_union_of_metrics() -> None:
    r1 = _report("x", metrics={"a": 1.0})
    r2 = _report("y", metrics={"b": 2.0})
    result = _collect_metric_values([r1, r2])
    assert set(result.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# compare_periods — structural tests
# ---------------------------------------------------------------------------


def test_compare_periods_empty_both() -> None:
    result = compare_periods([], [])
    assert isinstance(result, ComparisonResult)
    assert result.metrics == []
    assert result.n_sessions_a == 0
    assert result.n_sessions_b == 0


def test_compare_periods_empty_a() -> None:
    b = _period(5, metric_value=2.0)
    result = compare_periods([], b)
    assert result.n_sessions_a == 0
    assert result.n_sessions_b == 5
    assert result.metrics_only_in_b == ["m"]


def test_compare_periods_empty_b() -> None:
    a = _period(5, metric_value=1.0)
    result = compare_periods(a, [])
    assert result.metrics_only_in_a == ["m"]


def test_compare_periods_shared_metrics() -> None:
    a = _period(10, metric_value=1.0, metric_name="x")
    b = _period(10, metric_value=2.0, metric_name="x")
    result = compare_periods(a, b)
    assert len(result.metrics) == 1
    assert result.metrics[0].metric_name == "x"


def test_compare_periods_metrics_only_in_a() -> None:
    a = [_report("s", metrics={"only_a": 1.0})]
    b = [_report("t", metrics={"only_b": 2.0})]
    result = compare_periods(a, b)
    assert result.metrics_only_in_a == ["only_a"]
    assert result.metrics_only_in_b == ["only_b"]
    assert result.metrics == []


def test_compare_periods_delta_direction() -> None:
    # Period B has higher values → delta > 0
    a = _period(10, metric_value=1.0)
    b = _period(10, metric_value=3.0)
    result = compare_periods(a, b)
    assert result.metrics[0].delta > 0


def test_compare_periods_delta_pct_zero_mean_a() -> None:
    # mean_a=0 → delta_pct must be None (avoid division by zero)
    a = [_report("x", metrics={"m": 0.0}) for _ in range(10)]
    b = [_report("y", metrics={"m": 1.0}) for _ in range(10)]
    result = compare_periods(a, b)
    assert result.metrics[0].delta_pct is None


def test_compare_periods_delta_pct_nonzero_mean_a() -> None:
    a = [_report("x", metrics={"m": 2.0}) for _ in range(10)]
    b = [_report("y", metrics={"m": 4.0}) for _ in range(10)]
    result = compare_periods(a, b)
    assert result.metrics[0].delta_pct == pytest.approx(100.0)


def test_compare_periods_session_counts() -> None:
    a = _period(7, metric_value=1.0)
    b = _period(13, metric_value=2.0)
    result = compare_periods(a, b)
    assert result.n_sessions_a == 7
    assert result.n_sessions_b == 13


# ---------------------------------------------------------------------------
# compare_periods — significance
# ---------------------------------------------------------------------------


def test_compare_periods_identical_values_not_significant() -> None:
    # Identical means → not significant
    a = [_report(f"a{i}", metrics={"m": 5.0}) for i in range(20)]
    b = [_report(f"b{i}", metrics={"m": 5.0}) for i in range(20)]
    result = compare_periods(a, b)
    assert not result.metrics[0].significant


def test_compare_periods_clearly_different_significant() -> None:
    # Very different means, large n → should be significant
    a = [_report(f"a{i}", metrics={"m": 1.0}) for i in range(30)]
    b = [_report(f"b{i}", metrics={"m": 100.0}) for i in range(30)]
    result = compare_periods(a, b)
    mc = result.metrics[0]
    assert mc.significant
    assert mc.p_value is not None
    assert mc.p_value < SIGNIFICANCE_ALPHA


def test_compare_periods_n_less_than_2_no_t_test() -> None:
    # n=1 in either period → test skipped, significant=False, p_value=None
    a = [_report("only", metrics={"m": 1.0})]
    b = _period(10, metric_value=100.0)
    result = compare_periods(a, b)
    mc = result.metrics[0]
    assert mc.t_statistic is None
    assert mc.p_value is None
    assert not mc.significant


def test_compare_periods_n_zero_no_t_test() -> None:
    # n=0 in period A (metric only in B)
    a = [_report("x", metrics={"other": 1.0})]
    b = [_report("y", metrics={"m": 2.0})]
    result = compare_periods(a, b)
    # "m" is only in B; no comparison for it
    assert result.metrics == []


# ---------------------------------------------------------------------------
# _welch_t_test edge cases
# ---------------------------------------------------------------------------


def test_welch_t_test_zero_variance_same_means() -> None:
    t, p = _welch_t_test(3.0, 3.0, 0.0, 0.0, 10, 10)
    assert t == pytest.approx(0.0)
    assert p == pytest.approx(1.0)


def test_welch_t_test_zero_variance_different_means() -> None:
    t, p = _welch_t_test(1.0, 2.0, 0.0, 0.0, 10, 10)
    assert math.isinf(t)
    assert p == pytest.approx(0.0)


def test_welch_t_test_finite_result() -> None:
    t, p = _welch_t_test(1.0, 2.0, 1.0, 1.0, 10, 10)
    assert math.isfinite(t)
    assert 0.0 <= p <= 1.0


def test_welch_t_test_symmetric_pvalue() -> None:
    # Swapping A and B should give the same p-value (two-tailed)
    t1, p1 = _welch_t_test(1.0, 2.0, 1.0, 1.0, 15, 15)
    t2, p2 = _welch_t_test(2.0, 1.0, 1.0, 1.0, 15, 15)
    assert p1 == pytest.approx(p2)
    assert t1 == pytest.approx(-t2)


# ---------------------------------------------------------------------------
# _t_sf
# ---------------------------------------------------------------------------


def test_t_sf_zero_returns_half() -> None:
    # P(T > 0) = 0.5 for any symmetric t distribution
    assert _t_sf(0.0, df=10.0) == pytest.approx(0.5, abs=1e-6)


def test_t_sf_large_t_returns_near_zero() -> None:
    # For large t, tail probability approaches 0
    p = _t_sf(100.0, df=5.0)
    assert p < 1e-6


def test_t_sf_infinite_t_returns_zero() -> None:
    assert _t_sf(float("inf"), df=10.0) == pytest.approx(0.0)


def test_t_sf_negative_infinite_returns_zero() -> None:
    # We pass abs(t) to _t_sf; but test defensive path
    assert _t_sf(float("-inf"), df=10.0) == pytest.approx(0.0)


def test_t_sf_known_value() -> None:
    # For t=2.0, df=30, the two-tailed p-value is approximately 0.0546
    # so the one-tailed survival is ~0.0273
    p = _t_sf(2.0, df=30.0)
    assert p == pytest.approx(0.0273, abs=0.005)


# ---------------------------------------------------------------------------
# _regularised_incomplete_beta
# ---------------------------------------------------------------------------


def test_regularised_incomplete_beta_at_zero() -> None:
    assert _regularised_incomplete_beta(0.0, 2.0, 3.0) == pytest.approx(0.0)


def test_regularised_incomplete_beta_at_one() -> None:
    assert _regularised_incomplete_beta(1.0, 2.0, 3.0) == pytest.approx(1.0)


def test_regularised_incomplete_beta_known_value() -> None:
    # I(0.5; 2, 2) = 0.5 by symmetry of symmetric Beta distribution
    assert _regularised_incomplete_beta(0.5, 2.0, 2.0) == pytest.approx(0.5, abs=1e-6)


def test_regularised_incomplete_beta_invalid_x_raises() -> None:
    with pytest.raises(ValueError):
        _regularised_incomplete_beta(-0.1, 1.0, 1.0)


def test_regularised_incomplete_beta_x_above_one_raises() -> None:
    with pytest.raises(ValueError):
        _regularised_incomplete_beta(1.1, 1.0, 1.0)


# ---------------------------------------------------------------------------
# 100-session stability test
# ---------------------------------------------------------------------------


def test_compare_periods_100_sessions_numerically_stable() -> None:
    """compare_periods on 100 synthetic sessions must not produce NaN or inf."""
    rng = random.Random(0xDEAD)
    # Period A: 50 sessions with read_edit_ratio ~5 and reasoning_loop ~10
    a = [
        _report(
            f"a{i}",
            metrics={
                "read_edit_ratio": rng.gauss(5.0, 1.0),
                "reasoning_loop": rng.gauss(10.0, 3.0),
            },
        )
        for i in range(50)
    ]
    # Period B: 50 sessions with shifted distributions
    b = [
        _report(
            f"b{i}",
            metrics={
                "read_edit_ratio": rng.gauss(3.0, 1.2),
                "reasoning_loop": rng.gauss(15.0, 4.0),
            },
        )
        for i in range(50)
    ]
    result = compare_periods(a, b)
    assert result.n_sessions_a == 50
    assert result.n_sessions_b == 50
    assert len(result.metrics) == 2
    for mc in result.metrics:
        assert math.isfinite(mc.delta)
        assert mc.t_statistic is not None
        assert math.isfinite(mc.t_statistic)
        assert mc.p_value is not None
        assert 0.0 <= mc.p_value <= 1.0
        # read_edit_ratio decreased: A mean ~5, B mean ~3 → delta < 0
        if mc.metric_name == "read_edit_ratio":
            assert mc.delta < 0
        # reasoning_loop increased: A mean ~10, B mean ~15 → delta > 0
        if mc.metric_name == "reasoning_loop":
            assert mc.delta > 0


# ---------------------------------------------------------------------------
# _compare_metric edge cases
# ---------------------------------------------------------------------------


def test_compare_metric_n1_each() -> None:
    mc = _compare_metric("m", [1.0], [2.0])
    assert mc.t_statistic is None
    assert mc.p_value is None
    assert not mc.significant
    assert mc.delta == pytest.approx(1.0)


def test_compare_metric_n2_each_computes_t() -> None:
    mc = _compare_metric("m", [1.0, 1.2], [5.0, 5.2])
    assert mc.t_statistic is not None
    assert mc.p_value is not None


def test_compare_metric_delta_pct_computed() -> None:
    mc = _compare_metric("m", [2.0, 2.0], [4.0, 4.0])
    assert mc.delta_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# _beta_cf — internal stability
# ---------------------------------------------------------------------------


def test_beta_cf_returns_positive() -> None:
    # cf is a convergent series and must be positive
    v = _beta_cf(0.3, 2.0, 3.0)
    assert v > 0


def test_beta_cf_at_boundary_x_near_zero() -> None:
    v = _beta_cf(1e-10, 1.0, 1.0)
    assert math.isfinite(v)
