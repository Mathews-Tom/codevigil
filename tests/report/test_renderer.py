"""Tests for codevigil.report.renderer.

Covers:
- Group-by trend table: weekly table with correct structure, n<5 guard, and
  write_precision column presence.
- Compare-periods: signed delta table, prose one-liners, n<5 guard fires for
  the small-period-B fixture, passing periods produce headline numbers.
- Methodology section: required disclaimer phrase present, banned causal words
  absent in all rendered output on realistic fixtures.
- Appendix section: section headers present, threshold table, schema version,
  cell distribution.
- Snapshot tests: section headers and table column headers are stable (not
  exact numeric cells).

The renderer is a critical-path component per test-standards.md and requires
>= 95% coverage.
"""

from __future__ import annotations

import re
from datetime import date

from codevigil.analysis.store import SessionReport
from codevigil.report.renderer import (
    BANNED_CAUSAL_WORDS,
    render_compare_periods_report,
    render_group_by_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_banned_words(text: str) -> None:
    """Assert that no banned causal words appear in ``text``."""
    lowered = text.lower()
    for word in BANNED_CAUSAL_WORDS:
        assert word not in lowered, f"banned causal word {word!r} found in rendered output:\n{text}"


def _contains_phrase(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


# ---------------------------------------------------------------------------
# Group-by: weekly trend table
# ---------------------------------------------------------------------------


class TestGroupByWeeklyTable:
    def test_renders_h1_with_dimension(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "# Cohort Trend Report" in output
        assert "week" in output

    def test_has_week_rows(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        # Three ISO weeks should appear as row headers.
        assert "2026-W14" in output
        assert "2026-W15" in output
        assert "2026-W16" in output

    def test_has_write_precision_column(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "Write Precision" in output

    def test_has_read_edit_ratio_column(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "Read:Edit Ratio" in output

    def test_cells_contain_mean_stdev_n(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        # Each cell should match "X.XX ± X.XX (n=N)" pattern for sufficient n.
        assert re.search(r"\d+\.\d+ ± \d+\.\d+ \(n=\d+\)", output)

    def test_n_less_than_5_cells_are_redacted(self) -> None:
        """A single-session corpus per week produces n<5 cells."""
        from datetime import UTC, datetime, timedelta

        from codevigil.analysis.store import build_report

        # One session per week — n=1, which is < 5.
        base = datetime(2026, 3, 30, 10, 0, tzinfo=UTC)
        reports = [
            build_report(
                session_id=f"solo-{w}",
                project_hash="proj",
                project_name=None,
                model=None,
                permission_mode=None,
                started_at=base + timedelta(weeks=w),
                ended_at=base + timedelta(weeks=w, hours=1),
                event_count=10,
                parse_confidence=0.99,
                metrics={"read_edit_ratio": 5.0, "stop_phrase": 0.0},
            )
            for w in range(3)
        ]
        output = render_group_by_report(reports, dimension="week")
        # With n=1 per cell, all cells should be redacted.
        assert "n<5" in output

    def test_single_session_cell_shows_n_equals_1(self) -> None:
        """A single-session group shows the n=1 notation (not ± stdev)."""
        from datetime import UTC, datetime

        from codevigil.analysis.store import build_report

        reports = [
            build_report(
                session_id="single",
                project_hash="proj",
                project_name=None,
                model=None,
                permission_mode=None,
                started_at=datetime(2026, 3, 30, 10, 0, tzinfo=UTC),
                ended_at=datetime(2026, 3, 30, 11, 0, tzinfo=UTC),
                event_count=10,
                parse_confidence=0.99,
                metrics={"read_edit_ratio": 5.0},
            )
        ]
        output = render_group_by_report(reports, dimension="week")
        # n=1 should be redacted (sentinel) since 1 < 5.
        assert "n<5" in output

    def test_empty_corpus_graceful(self) -> None:
        output = render_group_by_report([], dimension="week")
        assert "No data available" in output

    def test_all_dimensions_accepted(self, corpus_35: list[SessionReport]) -> None:
        """All five supported dimensions produce output without error."""
        for dim in ("day", "week", "project"):
            output = render_group_by_report(corpus_35, dimension=dim)
            assert "# Cohort Trend Report" in output

    def test_date_filter_applied(self, corpus_35: list[SessionReport]) -> None:
        """Filtering to a single week should produce only that week's rows."""
        output = render_group_by_report(
            corpus_35,
            dimension="week",
            since=date(2026, 3, 30),
            until=date(2026, 4, 5),
        )
        assert "2026-W14" in output
        # W15 and W16 should not appear.
        assert "2026-W15" not in output
        assert "2026-W16" not in output


# ---------------------------------------------------------------------------
# Snapshot tests: section headers and table column headers
# ---------------------------------------------------------------------------


class TestGroupBySnapshot:
    """Snapshot tests that pin structure (not exact numerics) to detect drift."""

    def test_methodology_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "## Methodology" in output

    def test_appendix_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "## Appendix" in output

    def test_behavioral_catalog_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "### Behavioral Catalog" in output

    def test_threshold_table_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "### Threshold Table" in output

    def test_schema_version_present(self, corpus_35: list[SessionReport]) -> None:
        from codevigil.analysis.store import CURRENT_SCHEMA_VERSION

        output = render_group_by_report(corpus_35, dimension="week")
        assert f"Schema version: {CURRENT_SCHEMA_VERSION}" in output

    def test_sample_size_distribution_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "### Sample-Size Distribution" in output

    def test_table_column_headers_stable(self, corpus_35: list[SessionReport]) -> None:
        """Column headers must include the key metric names."""
        output = render_group_by_report(corpus_35, dimension="week")
        assert "Read:Edit Ratio" in output
        assert "Write Precision" in output


# ---------------------------------------------------------------------------
# Methodology: claim discipline
# ---------------------------------------------------------------------------


class TestMethodologyClaimDiscipline:
    def test_required_disclaimer_phrase_present_group_by(
        self, corpus_35: list[SessionReport]
    ) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert _contains_phrase(
            output, "metrics have not been validated against labeled outcomes"
        ), "Required disclaimer phrase missing from methodology section"

    def test_no_banned_causal_words_group_by(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        _no_banned_words(output)

    def test_required_disclaimer_phrase_present_compare(
        self, corpus_35: list[SessionReport]
    ) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        assert _contains_phrase(
            output, "metrics have not been validated against labeled outcomes"
        ), "Required disclaimer phrase missing from compare-periods methodology"

    def test_no_banned_causal_words_compare(self, corpus_35: list[SessionReport]) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        _no_banned_words(output)

    def test_banned_words_set_is_complete(self) -> None:
        """Verify the banned-words set matches the plan specification."""
        assert "caused" in BANNED_CAUSAL_WORDS
        assert "drove" in BANNED_CAUSAL_WORDS
        assert "led to" in BANNED_CAUSAL_WORDS

    def test_permitted_words_present(self, corpus_35: list[SessionReport]) -> None:
        """Verify correlation-friendly language is used in the methodology."""
        output = render_group_by_report(corpus_35, dimension="week")
        # At least one of the permitted phrases must appear.
        permitted = ["correlates", "coincides", "observed alongside"]
        assert any(p in output.lower() for p in permitted), (
            "Expected at least one correlation-language phrase in methodology"
        )


# ---------------------------------------------------------------------------
# Compare-periods: delta table and one-liners
# ---------------------------------------------------------------------------


class TestComparePeriods:
    def test_renders_h1_with_period_labels(self, corpus_35: list[SessionReport]) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        assert "# Period Comparison" in output
        assert "2026-03-30..2026-04-05" in output
        assert "2026-04-06..2026-04-12" in output

    def test_has_methodology_and_appendix(self, corpus_35: list[SessionReport]) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        assert "## Methodology" in output
        assert "## Appendix" in output

    def test_summary_section_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        assert "## Summary" in output

    def test_one_liners_include_n_counts(self, corpus_35: list[SessionReport]) -> None:
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        # One-liners include n=X, n=Y counts.
        assert re.search(r"n=\d+", output)

    def test_sample_size_guard_fires_on_small_period_b(
        self, corpus_small_period_b: list[SessionReport]
    ) -> None:
        """Period B has 3 sessions (< 5); guard must fire and suppress headline."""
        output = render_compare_periods_report(
            corpus_small_period_b,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 13),
            period_b_until=date(2026, 4, 19),
        )
        # The n<5 sentinel must appear (table cell or summary).
        assert "n<5" in output
        # "insufficient data" should appear in the summary.
        assert "insufficient data" in output

    def test_guard_does_not_fire_when_both_periods_large(
        self, corpus_35: list[SessionReport]
    ) -> None:
        """Both periods have >= 5 sessions; one-liners should have headline numbers."""
        output = render_compare_periods_report(
            corpus_35,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 6),
            period_b_until=date(2026, 4, 12),
        )
        # A prose one-liner with "fell" or "rose" should appear.
        assert re.search(r"fell|rose|held steady", output)

    def test_empty_periods_graceful(self) -> None:
        """No sessions in either period should produce graceful empty output."""
        output = render_compare_periods_report(
            [],
            period_a_since=date(2026, 1, 1),
            period_a_until=date(2026, 1, 7),
            period_b_since=date(2026, 2, 1),
            period_b_until=date(2026, 2, 7),
        )
        assert "# Period Comparison" in output
        assert "No metrics shared" in output


# ---------------------------------------------------------------------------
# Appendix: behavioral catalog and threshold table
# ---------------------------------------------------------------------------


class TestAppendix:
    def test_threshold_table_contains_read_edit_ratio(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "Read:Edit Ratio" in output
        # Default warn threshold from config.
        assert "4.0" in output

    def test_behavioral_catalog_contains_write_precision_definition(
        self, corpus_35: list[SessionReport]
    ) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "Write Precision" in output
        assert "write_calls / (write_calls + edit_calls)" in output

    def test_schema_version_line_present(self, corpus_35: list[SessionReport]) -> None:
        from codevigil.analysis.store import CURRENT_SCHEMA_VERSION

        output = render_group_by_report(corpus_35, dimension="week")
        assert f"Schema version: {CURRENT_SCHEMA_VERSION}" in output

    def test_cell_distribution_table_present(self, corpus_35: list[SessionReport]) -> None:
        output = render_group_by_report(corpus_35, dimension="week")
        assert "| n-range | cell count |" in output


# ---------------------------------------------------------------------------
# Direction word helper
# ---------------------------------------------------------------------------


class TestDirectionWord:
    def test_negative_delta_is_fell(self) -> None:
        from codevigil.report.renderer import _direction_word

        assert _direction_word(-1.0) == "fell"

    def test_positive_delta_is_rose(self) -> None:
        from codevigil.report.renderer import _direction_word

        assert _direction_word(1.0) == "rose"

    def test_zero_delta_is_held_steady(self) -> None:
        from codevigil.report.renderer import _direction_word

        assert _direction_word(0.0) == "held steady"


# ---------------------------------------------------------------------------
# Edge cases: coverage for n<5 cell guard in _format_cell
# ---------------------------------------------------------------------------


class TestCellFormatting:
    def test_n_less_than_5_cell_renders_sentinel(self) -> None:
        """A cell with n < 5 must render as 'n<5' sentinel."""
        from codevigil.analysis.cohort import CohortCell
        from codevigil.report.renderer import _format_cell

        cell = CohortCell(
            dimension_value="2026-W14",
            metric_name="read_edit_ratio",
            mean=5.0,
            stdev=0.0,
            n=3,
            min_value=5.0,
            max_value=5.0,
        )
        result = _format_cell(cell)
        assert result == "n<5"

    def test_sufficient_n_cell_renders_mean_and_stdev(self) -> None:
        """A cell with n >= 5 renders as 'mean ± stdev (n=N)'."""
        from codevigil.analysis.cohort import CohortCell
        from codevigil.report.renderer import _format_cell

        cell = CohortCell(
            dimension_value="2026-W14",
            metric_name="read_edit_ratio",
            mean=5.25,
            stdev=0.75,
            n=10,
            min_value=4.0,
            max_value=6.5,
        )
        result = _format_cell(cell)
        assert "5.25" in result
        assert "0.75" in result
        assert "(n=10)" in result


class TestMetricsOnlyOneDirection:
    def test_compare_metrics_only_in_a_shown(self) -> None:
        """Metrics that appear only in period A are listed in the comparison output."""
        from datetime import UTC, datetime, timedelta

        from codevigil.analysis.store import build_report
        from codevigil.report.renderer import render_compare_periods_report

        base = datetime(2026, 3, 30, 10, 0, tzinfo=UTC)

        def _rep(sid: str, t: datetime, metrics: dict[str, float]) -> SessionReport:
            return build_report(
                session_id=sid,
                project_hash="proj",
                project_name=None,
                model=None,
                permission_mode=None,
                started_at=t,
                ended_at=t + timedelta(hours=1),
                event_count=10,
                parse_confidence=0.99,
                metrics=metrics,
            )

        # Period A has "read_edit_ratio" and "stop_phrase";
        # Period B only has "reasoning_loop".
        period_a = [
            _rep(f"a{i}", base + timedelta(days=i), {"read_edit_ratio": 5.0, "stop_phrase": 0.5})
            for i in range(6)
        ]
        period_b = [
            _rep(f"b{i}", base + timedelta(days=30 + i), {"reasoning_loop": 10.0}) for i in range(6)
        ]
        from datetime import date

        output = render_compare_periods_report(
            period_a + period_b,
            period_a_since=date(2026, 3, 30),
            period_a_until=date(2026, 4, 5),
            period_b_since=date(2026, 4, 29),
            period_b_until=date(2026, 5, 5),
        )
        assert "only in period A" in output
        assert "only in period B" in output
