"""Unit tests for codevigil.analysis.cohort.

Covers the reducer (reduce_by) against synthetic fixtures, the filter helper
(filter_by_period), null-dimension exclusion, and edge cases (empty input,
unknown dimension, single-session group).

The cohort reducer is a critical-path component per test-standards.md — it
requires >= 95% coverage.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from codevigil.analysis.cohort import (
    VALID_DIMENSIONS,
    CohortSlice,
    filter_by_period,
    reduce_by,
)
from codevigil.analysis.store import SessionReport, build_report

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_EPOCH = datetime(2026, 4, 14, 9, 0, 0, tzinfo=UTC)


def _report(
    session_id: str = "s1",
    *,
    started_at: datetime | None = None,
    metrics: dict[str, float] | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    project_hash: str = "projA",
) -> SessionReport:
    t0 = started_at or _EPOCH
    return build_report(
        session_id=session_id,
        project_hash=project_hash,
        project_name=None,
        model=model,
        permission_mode=permission_mode,
        started_at=t0,
        ended_at=t0 + timedelta(minutes=30),
        event_count=10,
        parse_confidence=0.95,
        metrics=metrics or {"read_edit_ratio": 4.0, "reasoning_loop": 10.0},
    )


def _reports_n(
    n: int,
    *,
    metric_value: float = 1.0,
    project_hash: str = "projA",
    model: str | None = None,
    permission_mode: str | None = None,
    day_offset: int = 0,
) -> list[SessionReport]:
    """Generate n reports spread 1 hour apart starting from _EPOCH + day_offset days."""
    base = _EPOCH + timedelta(days=day_offset)
    return [
        _report(
            f"s-{i}",
            started_at=base + timedelta(hours=i),
            metrics={"m": metric_value + i * 0.1},
            project_hash=project_hash,
            model=model,
            permission_mode=permission_mode,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# VALID_DIMENSIONS
# ---------------------------------------------------------------------------


def test_valid_dimensions_contains_all_five() -> None:
    assert {"day", "week", "project", "model", "permission_mode"} == VALID_DIMENSIONS


# ---------------------------------------------------------------------------
# reduce_by — unknown dimension
# ---------------------------------------------------------------------------


def test_reduce_by_unknown_dimension_raises() -> None:
    with pytest.raises(ValueError, match="unsupported group-by dimension"):
        reduce_by([], "bad_dim")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reduce_by — empty input
# ---------------------------------------------------------------------------


def test_reduce_by_empty_returns_empty_slice() -> None:
    result = reduce_by([], "day")
    assert isinstance(result, CohortSlice)
    assert result.cells == []
    assert result.session_count == 0
    assert result.excluded_null_count == 0


# ---------------------------------------------------------------------------
# reduce_by — day
# ---------------------------------------------------------------------------


def test_reduce_by_day_single_session() -> None:
    reports = [_report("only", metrics={"m": 3.0})]
    result = reduce_by(reports, "day")
    assert result.session_count == 1
    assert len(result.cells) == 1
    cell = result.cells[0]
    assert cell.dimension_value == _EPOCH.date().isoformat()
    assert cell.metric_name == "m"
    assert cell.mean == pytest.approx(3.0)
    assert cell.n == 1
    assert cell.stdev == pytest.approx(0.0)


def test_reduce_by_day_multiple_sessions_same_day() -> None:
    r1 = _report("a", metrics={"x": 2.0})
    r2 = _report("b", metrics={"x": 4.0})
    result = reduce_by([r1, r2], "day")
    assert result.session_count == 2
    assert len(result.cells) == 1
    cell = result.cells[0]
    assert cell.n == 2
    assert cell.mean == pytest.approx(3.0)


def test_reduce_by_day_two_days_separate_cells() -> None:
    day1 = _EPOCH
    day2 = _EPOCH + timedelta(days=1)
    r1 = _report("a", started_at=day1, metrics={"m": 1.0})
    r2 = _report("b", started_at=day2, metrics={"m": 2.0})
    result = reduce_by([r1, r2], "day")
    assert len(result.cells) == 2
    assert {c.dimension_value for c in result.cells} == {
        day1.date().isoformat(),
        day2.date().isoformat(),
    }


def test_reduce_by_day_min_max_correct() -> None:
    r1 = _report("a", metrics={"m": 1.0})
    r2 = _report("b", metrics={"m": 5.0})
    result = reduce_by([r1, r2], "day")
    cell = result.cells[0]
    assert cell.min_value == pytest.approx(1.0)
    assert cell.max_value == pytest.approx(5.0)


def test_reduce_by_day_stdev_nonzero_for_multiple() -> None:
    reports = _reports_n(5, metric_value=0.0)
    result = reduce_by(reports, "day")
    # Values differ (0.0, 0.1, 0.2, 0.3, 0.4), stdev > 0
    assert result.cells[0].stdev > 0.0


# ---------------------------------------------------------------------------
# reduce_by — week
# ---------------------------------------------------------------------------


def test_reduce_by_week_groups_same_week() -> None:
    # 2026-04-14 is a Tuesday; 2026-04-15 is Wednesday — same ISO week
    t1 = datetime(2026, 4, 14, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    r1 = _report("a", started_at=t1, metrics={"m": 10.0})
    r2 = _report("b", started_at=t2, metrics={"m": 20.0})
    result = reduce_by([r1, r2], "week")
    assert len(result.cells) == 1
    assert result.cells[0].n == 2


def test_reduce_by_week_separates_different_weeks() -> None:
    # Two weeks apart
    t1 = datetime(2026, 4, 6, 9, 0, tzinfo=UTC)  # Week 15
    t2 = datetime(2026, 4, 13, 9, 0, tzinfo=UTC)  # Week 16
    r1 = _report("a", started_at=t1, metrics={"m": 1.0})
    r2 = _report("b", started_at=t2, metrics={"m": 2.0})
    result = reduce_by([r1, r2], "week")
    assert len(result.cells) == 2
    week_labels = {c.dimension_value for c in result.cells}
    assert "2026-W15" in week_labels
    assert "2026-W16" in week_labels


# ---------------------------------------------------------------------------
# reduce_by — project
# ---------------------------------------------------------------------------


def test_reduce_by_project_two_projects() -> None:
    r1 = _report("a", project_hash="p1", metrics={"m": 1.0})
    r2 = _report("b", project_hash="p2", metrics={"m": 2.0})
    result = reduce_by([r1, r2], "project")
    assert {c.dimension_value for c in result.cells} == {"p1", "p2"}


def test_reduce_by_project_empty_hash_excluded() -> None:
    # project_hash="" — _key_project returns None (falsy), excluded
    r = build_report(
        session_id="empty-proj",
        project_hash="",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=_EPOCH,
        ended_at=_EPOCH + timedelta(minutes=10),
        event_count=5,
        parse_confidence=0.9,
        metrics={"m": 1.0},
    )
    result = reduce_by([r], "project")
    assert result.cells == []
    assert result.excluded_null_count == 1


# ---------------------------------------------------------------------------
# reduce_by — model
# ---------------------------------------------------------------------------


def test_reduce_by_model_excludes_null_model() -> None:
    r_none = _report("no-model", model=None, metrics={"m": 1.0})
    r_gpt = _report("with-model", model="gpt-5", metrics={"m": 2.0})
    result = reduce_by([r_none, r_gpt], "model")
    assert result.excluded_null_count == 1
    assert len(result.cells) == 1
    assert result.cells[0].dimension_value == "gpt-5"


def test_reduce_by_model_all_null_returns_empty() -> None:
    reports = [_report(f"s{i}", model=None) for i in range(3)]
    result = reduce_by(reports, "model")
    assert result.cells == []
    assert result.excluded_null_count == 3


def test_reduce_by_model_multiple_models() -> None:
    r1 = _report("a", model="gpt-5", metrics={"m": 1.0})
    r2 = _report("b", model="gpt-5-mini", metrics={"m": 3.0})
    result = reduce_by([r1, r2], "model")
    assert len(result.cells) == 2
    dim_values = {c.dimension_value for c in result.cells}
    assert dim_values == {"gpt-5", "gpt-5-mini"}


# ---------------------------------------------------------------------------
# reduce_by — permission_mode
# ---------------------------------------------------------------------------


def test_reduce_by_permission_mode_excludes_null() -> None:
    r_none = _report("a", permission_mode=None, metrics={"m": 1.0})
    r_pm = _report("b", permission_mode="default", metrics={"m": 2.0})
    result = reduce_by([r_none, r_pm], "permission_mode")
    assert result.excluded_null_count == 1
    assert len(result.cells) == 1
    assert result.cells[0].dimension_value == "default"


# ---------------------------------------------------------------------------
# reduce_by — multiple metrics
# ---------------------------------------------------------------------------


def test_reduce_by_day_multiple_metrics_produce_separate_cells() -> None:
    r = _report("a", metrics={"read_edit_ratio": 5.0, "reasoning_loop": 15.0})
    result = reduce_by([r], "day")
    metric_names = {c.metric_name for c in result.cells}
    assert metric_names == {"read_edit_ratio", "reasoning_loop"}


def test_reduce_by_day_cells_sorted() -> None:
    """Cells must be sorted by (dimension_value, metric_name)."""
    day1 = _EPOCH
    day2 = _EPOCH + timedelta(days=1)
    reports = [
        _report("a", started_at=day2, metrics={"z": 1.0, "a": 2.0}),
        _report("b", started_at=day1, metrics={"z": 3.0, "a": 4.0}),
    ]
    result = reduce_by(reports, "day")
    keys = [(c.dimension_value, c.metric_name) for c in result.cells]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# reduce_by — session_count accuracy
# ---------------------------------------------------------------------------


def test_reduce_by_session_count_includes_null_excluded() -> None:
    r_null = _report("no-model", model=None)
    r_ok = _report("with-model", model="gpt-5")
    result = reduce_by([r_null, r_ok], "model")
    assert result.session_count == 2
    assert result.excluded_null_count == 1


# ---------------------------------------------------------------------------
# reduce_by — session with no metrics
# ---------------------------------------------------------------------------


def test_reduce_by_session_with_no_metrics_produces_no_cells() -> None:
    r = build_report(
        session_id="empty-metrics",
        project_hash="p",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=_EPOCH,
        ended_at=_EPOCH + timedelta(minutes=10),
        event_count=5,
        parse_confidence=0.9,
        metrics={},
    )
    result = reduce_by([r], "day")
    assert result.session_count == 1
    assert result.cells == []


# ---------------------------------------------------------------------------
# filter_by_period
# ---------------------------------------------------------------------------


def test_filter_by_period_no_bounds_returns_all() -> None:
    reports = _reports_n(5)
    assert filter_by_period(reports) == reports


def test_filter_by_period_since_inclusive() -> None:
    t_before = _EPOCH - timedelta(days=1)
    r_before = _report("old", started_at=t_before)
    r_on = _report("on", started_at=_EPOCH)
    r_after = _report("after", started_at=_EPOCH + timedelta(days=1))
    result = filter_by_period([r_before, r_on, r_after], since=_EPOCH.date())
    assert {r.session_id for r in result} == {"on", "after"}


def test_filter_by_period_until_inclusive() -> None:
    r_early = _report("early", started_at=_EPOCH - timedelta(days=1))
    r_on = _report("on", started_at=_EPOCH)
    r_late = _report("late", started_at=_EPOCH + timedelta(days=1))
    result = filter_by_period([r_early, r_on, r_late], until=_EPOCH.date())
    assert {r.session_id for r in result} == {"early", "on"}


def test_filter_by_period_since_and_until() -> None:
    dates = [_EPOCH + timedelta(days=i) for i in range(7)]
    reports = [_report(f"s{i}", started_at=d) for i, d in enumerate(dates)]
    since = dates[2].date()
    until = dates[4].date()
    result = filter_by_period(reports, since=since, until=until)
    assert len(result) == 3
    for r in result:
        d = r.started_at.date()
        assert since <= d <= until


def test_filter_by_period_empty_input() -> None:
    result = filter_by_period([], since=date(2026, 1, 1))
    assert result == []


# ---------------------------------------------------------------------------
# Synthetic large fixture — reducer stability
# ---------------------------------------------------------------------------


def test_reduce_by_week_100_sessions_stable() -> None:
    """Reducer must not crash or produce NaN on 100 synthetic sessions."""
    import random

    rng = random.Random(42)
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Monday
    reports = [
        _report(
            f"bulk-{i}",
            started_at=base + timedelta(days=rng.randint(0, 13)),
            metrics={
                "read_edit_ratio": rng.uniform(1.0, 10.0),
                "reasoning_loop": rng.uniform(0.0, 30.0),
            },
        )
        for i in range(100)
    ]
    result = reduce_by(reports, "week")
    assert result.session_count == 100
    for cell in result.cells:
        assert cell.n >= 1
        assert isinstance(cell.mean, float)
        import math

        assert math.isfinite(cell.mean)
        assert math.isfinite(cell.stdev)
