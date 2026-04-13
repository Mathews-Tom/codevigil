"""Tests for codevigil.history.filters.

Covers filter logic (critical path: 95% coverage required), severity
classification, and all utility formatters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codevigil.analysis.store import SessionReport, build_report
from codevigil.history.filters import (
    apply_filters,
    classify_metric_severity,
    format_duration,
    format_started_at,
    parse_date_arg,
    severity_of_report,
    short_id,
    top_metrics_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=30)


def _make_report(
    session_id: str = "agent-abc123",
    *,
    started_at: datetime | None = None,
    metrics: dict[str, float] | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    project_name: str | None = None,
    project_hash: str = "proj-hash",
) -> SessionReport:
    return build_report(
        session_id=session_id,
        project_hash=project_hash,
        project_name=project_name,
        model=model,
        permission_mode=permission_mode,
        started_at=started_at or _T0,
        ended_at=(started_at or _T0) + timedelta(minutes=30),
        event_count=10,
        parse_confidence=0.99,
        metrics=metrics or {},
    )


# ---------------------------------------------------------------------------
# classify_metric_severity
# ---------------------------------------------------------------------------


class TestClassifyMetricSeverity:
    def test_unknown_metric_returns_ok(self) -> None:
        assert classify_metric_severity("unknown_metric", 999.0) == "ok"

    def test_read_edit_ratio_high_value_returns_ok(self) -> None:
        # read_edit_ratio is inverted: higher is better
        assert classify_metric_severity("read_edit_ratio", 6.0) == "ok"

    def test_read_edit_ratio_below_warn_returns_warn(self) -> None:
        # 3.0 < 4.0 (warn threshold) but >= 2.0 (crit threshold) -> warn
        assert classify_metric_severity("read_edit_ratio", 3.0) == "warn"

    def test_read_edit_ratio_at_warn_boundary_returns_ok(self) -> None:
        # 4.0 == warn threshold -> not below warn -> ok
        assert classify_metric_severity("read_edit_ratio", 4.0) == "ok"

    def test_read_edit_ratio_below_crit_returns_crit(self) -> None:
        # 1.5 < 2.0 (crit threshold) -> crit
        assert classify_metric_severity("read_edit_ratio", 1.5) == "crit"

    def test_stop_phrase_zero_returns_ok(self) -> None:
        # 0.0 < 1.0 (warn) -> ok
        assert classify_metric_severity("stop_phrase", 0.0) == "ok"

    def test_stop_phrase_at_warn_returns_warn(self) -> None:
        # 1.0 >= 1.0 (warn) but < 3.0 (crit) -> warn
        assert classify_metric_severity("stop_phrase", 1.0) == "warn"

    def test_stop_phrase_at_crit_returns_crit(self) -> None:
        # 3.0 >= 3.0 (crit) -> crit
        assert classify_metric_severity("stop_phrase", 3.0) == "crit"

    def test_parse_health_high_value_returns_ok(self) -> None:
        # parse_health is inverted: lower is worse
        assert classify_metric_severity("parse_health", 0.99) == "ok"

    def test_parse_health_below_threshold_returns_crit(self) -> None:
        # parse_health has warn==crit==0.9; any value < 0.9 is crit
        assert classify_metric_severity("parse_health", 0.80) == "crit"

    def test_parse_health_below_crit_returns_crit(self) -> None:
        assert classify_metric_severity("parse_health", 0.60) == "crit"

    def test_custom_thresholds_override_defaults(self) -> None:
        thresholds = {"custom_metric": (1.0, 2.0)}
        assert classify_metric_severity("custom_metric", 1.5, thresholds=thresholds) == "warn"
        assert classify_metric_severity("custom_metric", 2.0, thresholds=thresholds) == "crit"
        assert classify_metric_severity("custom_metric", 0.5, thresholds=thresholds) == "ok"


# ---------------------------------------------------------------------------
# severity_of_report
# ---------------------------------------------------------------------------


class TestSeverityOfReport:
    def test_empty_metrics_returns_ok(self) -> None:
        r = _make_report(metrics={})
        assert severity_of_report(r) == "ok"

    def test_all_ok_returns_ok(self) -> None:
        r = _make_report(metrics={"stop_phrase": 0.0})
        assert severity_of_report(r) == "ok"

    def test_one_warn_metric_returns_warn(self) -> None:
        # stop_phrase=1.0 >= 1.0 (warn) but < 3.0 (crit) -> warn
        r = _make_report(metrics={"stop_phrase": 1.0})
        assert severity_of_report(r) == "warn"

    def test_crit_metric_returns_crit(self) -> None:
        # stop_phrase=3.0 >= 3.0 (crit) -> crit
        r = _make_report(metrics={"stop_phrase": 3.0})
        assert severity_of_report(r) == "crit"

    def test_crit_takes_priority_over_warn(self) -> None:
        # stop_phrase=3.0 -> crit, read_edit_ratio=3.0 -> warn (3.0 < 4.0 warn thresh, >= 2.0 crit)
        r = _make_report(metrics={"stop_phrase": 3.0, "read_edit_ratio": 3.0})
        assert severity_of_report(r) == "crit"

    def test_warn_without_crit_returns_warn(self) -> None:
        # stop_phrase=1.5 >= 1.0 (warn) but < 3.0 (crit) -> warn
        # read_edit_ratio=5.0 -> ok (above both thresholds on inverted scale)
        r = _make_report(metrics={"stop_phrase": 1.5, "read_edit_ratio": 5.0})
        assert severity_of_report(r) == "warn"


# ---------------------------------------------------------------------------
# apply_filters — critical path
# ---------------------------------------------------------------------------


class TestApplyFilters:
    def _reports(self) -> list[SessionReport]:
        return [
            _make_report(
                "agent-001",
                model="gpt-4.1",
                permission_mode="default",
                project_name="proj-a",
                project_hash="hash-a",
                metrics={"stop_phrase": 0.0},  # ok
            ),
            _make_report(
                "agent-002",
                model="gpt-4.1-mini",
                permission_mode="bypassPermissions",
                project_name="proj-b",
                project_hash="hash-b",
                metrics={"stop_phrase": 1.0},  # warn (>= 1.0 but < 3.0)
                started_at=_T0 + timedelta(days=3),
            ),
            _make_report(
                "agent-003",
                model="gpt-4.1",
                permission_mode="default",
                project_name=None,
                project_hash="hash-c",
                metrics={"stop_phrase": 3.0},  # crit (>= 3.0)
                started_at=_T0 + timedelta(days=6),
            ),
        ]

    def test_no_filters_returns_all(self) -> None:
        reports = self._reports()
        result = apply_filters(reports)
        assert len(result) == 3

    def test_filter_by_project_name_matches(self) -> None:
        result = apply_filters(self._reports(), project="proj-a")
        assert len(result) == 1
        assert result[0].session_id == "agent-001"

    def test_filter_by_project_hash_matches(self) -> None:
        result = apply_filters(self._reports(), project="hash-b")
        assert len(result) == 1
        assert result[0].session_id == "agent-002"

    def test_filter_by_project_nonexistent_returns_empty(self) -> None:
        result = apply_filters(self._reports(), project="no-such-project")
        assert result == []

    def test_filter_by_since_excludes_earlier(self) -> None:

        since = (_T0 + timedelta(days=3)).date()
        result = apply_filters(self._reports(), since=since)
        assert len(result) == 2
        ids = {r.session_id for r in result}
        assert "agent-001" not in ids

    def test_filter_by_until_excludes_later(self) -> None:

        until = (_T0 + timedelta(days=3)).date()
        result = apply_filters(self._reports(), until=until)
        assert len(result) == 2
        ids = {r.session_id for r in result}
        assert "agent-003" not in ids

    def test_filter_by_since_and_until_narrows_range(self) -> None:

        since = (_T0 + timedelta(days=1)).date()
        until = (_T0 + timedelta(days=5)).date()
        result = apply_filters(self._reports(), since=since, until=until)
        assert len(result) == 1
        assert result[0].session_id == "agent-002"

    def test_filter_by_severity_ok(self) -> None:
        result = apply_filters(self._reports(), severity="ok")
        assert len(result) == 1
        assert result[0].session_id == "agent-001"

    def test_filter_by_severity_warn(self) -> None:
        result = apply_filters(self._reports(), severity="warn")
        assert len(result) == 1
        assert result[0].session_id == "agent-002"

    def test_filter_by_severity_crit(self) -> None:
        result = apply_filters(self._reports(), severity="crit")
        assert len(result) == 1
        assert result[0].session_id == "agent-003"

    def test_filter_by_model(self) -> None:
        result = apply_filters(self._reports(), model="gpt-4.1")
        assert len(result) == 2
        assert all(r.model == "gpt-4.1" for r in result)

    def test_filter_by_model_no_match_returns_empty(self) -> None:
        result = apply_filters(self._reports(), model="no-such-model")
        assert result == []

    def test_filter_by_permission_mode(self) -> None:
        result = apply_filters(self._reports(), permission_mode="default")
        assert len(result) == 2

    def test_combined_filters_are_anded(self) -> None:
        result = apply_filters(
            self._reports(),
            model="gpt-4.1",
            permission_mode="default",
            severity="ok",
        )
        assert len(result) == 1
        assert result[0].session_id == "agent-001"


# ---------------------------------------------------------------------------
# Utility formatters
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_valid_date_parses_correctly(self) -> None:
        from datetime import date

        d = parse_date_arg("2026-04-14")
        assert d == date(2026, 4, 14)

    def test_invalid_date_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid date"):
            parse_date_arg("not-a-date")

    def test_partial_date_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_date_arg("2026-04")


class TestShortId:
    def test_strips_agent_prefix(self) -> None:
        assert short_id("agent-abc123def456") == "abc123def456"

    def test_truncates_to_12_chars(self) -> None:
        result = short_id("agent-" + "a" * 20)
        assert len(result) == 12

    def test_no_agent_prefix_returns_truncated(self) -> None:
        result = short_id("abcdefghijklmnop")
        assert result == "abcdefghijkl"

    def test_short_id_stays_full(self) -> None:
        assert short_id("agent-abc") == "abc"


class TestFormatDuration:
    def test_zero_seconds(self) -> None:
        assert format_duration(0.0) == "0s"

    def test_under_one_minute(self) -> None:
        assert format_duration(45.9) == "45s"

    def test_one_minute_thirty_seconds(self) -> None:
        assert format_duration(90.0) == "1m30s"

    def test_one_hour(self) -> None:
        assert format_duration(3600.0) == "1h00m"

    def test_one_hour_two_minutes(self) -> None:
        assert format_duration(3720.0) == "1h02m"


class TestTopMetricsSummary:
    def test_empty_metrics_returns_dash(self) -> None:
        assert top_metrics_summary({}) == "—"

    def test_single_metric(self) -> None:
        result = top_metrics_summary({"stop_phrase": 5.0})
        assert result == "stop_phrase=5.00"

    def test_top_2_selected_by_absolute_value(self) -> None:
        metrics = {"a": 1.0, "b": 10.0, "c": 3.0}
        result = top_metrics_summary(metrics, n=2)
        # b=10.0 and c=3.0 should be the top-2
        assert "b=10.00" in result
        assert "c=3.00" in result
        assert "a=1.00" not in result

    def test_negative_value_uses_absolute(self) -> None:
        metrics = {"a": -10.0, "b": 2.0}
        result = top_metrics_summary(metrics, n=1)
        assert "a=-10.00" in result


class TestFormatStartedAt:
    def test_formats_to_minute_precision(self) -> None:
        dt = datetime(2026, 4, 14, 10, 5, 30, tzinfo=UTC)
        result = format_started_at(dt)
        assert result == "2026-04-14 10:05"
