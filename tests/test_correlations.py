"""Tests for the experimental correlation helper."""

from __future__ import annotations

from datetime import UTC, datetime

from codevigil.analysis.correlations import compute_correlations
from codevigil.analysis.store import SessionReport, build_report


def _make(metrics: dict[str, float], idx: int) -> SessionReport:
    ts = datetime(2026, 4, 1, 9, idx % 60, tzinfo=UTC)
    return build_report(
        session_id=f"s{idx}",
        project_hash="p",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=ts,
        ended_at=ts,
        event_count=1,
        parse_confidence=1.0,
        metrics=metrics,
    )


def test_perfect_positive_correlation() -> None:
    reports = [_make({"a": float(i), "b": 2.0 * i}, i) for i in range(40)]
    out = compute_correlations(reports)
    assert len(out) == 1
    mc = out[0]
    assert (mc.metric_a, mc.metric_b) == ("a", "b")
    assert abs(mc.r - 1.0) < 1e-9
    assert mc.n == 40


def test_perfect_negative_correlation() -> None:
    reports = [_make({"a": float(i), "b": -float(i)}, i) for i in range(40)]
    out = compute_correlations(reports)
    assert abs(out[0].r + 1.0) < 1e-9


def test_constant_column_omitted() -> None:
    reports = [_make({"a": float(i), "b": 5.0}, i) for i in range(40)]
    assert compute_correlations(reports) == []


def test_min_pairs_floor_drops_short_columns() -> None:
    reports = [_make({"a": float(i), "b": float(i)}, i) for i in range(10)]
    assert compute_correlations(reports) == []


def test_partial_overlap_only_counts_joint_observations() -> None:
    reports: list[SessionReport] = []
    for i in range(40):
        metrics: dict[str, float] = {"a": float(i)}
        if i % 2 == 0:
            metrics["b"] = float(i)
        reports.append(_make(metrics, i))
    out = compute_correlations(reports)
    # 20 even-indexed sessions have both a and b — below the default 30 floor.
    assert out == []


def test_results_sorted_by_abs_r_descending() -> None:
    reports: list[SessionReport] = []
    for i in range(40):
        reports.append(
            _make(
                {
                    "a": float(i),
                    "b": float(i) * 2.0,  # perfectly correlated with a
                    "c": float(i % 3),  # weakly correlated
                },
                i,
            )
        )
    out = compute_correlations(reports)
    assert len(out) == 3
    # First entry is the perfect a/b pair.
    assert (out[0].metric_a, out[0].metric_b) == ("a", "b")
    assert abs(out[0].r) >= abs(out[1].r) >= abs(out[2].r)
