"""Claim-discipline lint over every renderer path.

Every Markdown emitter must avoid causal language. This file walks each
public renderer in :mod:`codevigil.report.renderer` and asserts that
none of the strings in
:data:`~codevigil.report.renderer.BANNED_CAUSAL_WORDS` appear in its
output. New renderer paths must be added here so causal language cannot
sneak in via a new section.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from codevigil.analysis.cohort import CohortCell, CohortSlice
from codevigil.analysis.store import SessionReport, build_report
from codevigil.report.renderer import (
    BANNED_CAUSAL_WORDS,
    render_compare_periods_report,
    render_correlations_section,
    render_group_by_csv,
    render_group_by_json,
    render_group_by_report,
    render_multi_period,
)


def _assert_clean(text: str) -> None:
    lowered = text.lower()
    for word in BANNED_CAUSAL_WORDS:
        assert word not in lowered, f"banned causal word {word!r} in:\n{text[:500]}"


@pytest.fixture
def reports() -> list[SessionReport]:
    out: list[SessionReport] = []
    for i in range(20):
        ts = datetime(2026, 4, 1 + (i % 14), 9, 0, tzinfo=UTC)
        out.append(
            build_report(
                session_id=f"s{i}",
                project_hash="p",
                project_name=None,
                model=None,
                permission_mode=None,
                started_at=ts,
                ended_at=ts,
                event_count=10,
                parse_confidence=1.0,
                metrics={
                    "read_edit_ratio": 0.5 + (i % 5) * 0.1,
                    "thinking_visible_chars_median": 200.0 + i * 50,
                    "blind_edit_rate": 0.1 + (i % 3) * 0.05,
                    "user_turns": float(i + 1),
                },
            )
        )
    return out


def test_group_by_clean(reports: list[SessionReport]) -> None:
    _assert_clean(render_group_by_report(reports, dimension="week"))


def test_compare_periods_clean(reports: list[SessionReport]) -> None:
    _assert_clean(
        render_compare_periods_report(
            reports,
            period_a_since=date(2026, 4, 1),
            period_a_until=date(2026, 4, 7),
            period_b_since=date(2026, 4, 8),
            period_b_until=date(2026, 4, 14),
        )
    )


def test_multi_period_clean(reports: list[SessionReport]) -> None:
    _assert_clean(render_multi_period({"7d": reports, "30d": reports}))


def test_correlations_section_clean(reports: list[SessionReport]) -> None:
    # Need at least 30 reports to clear the MIN_PAIRS gate.
    rich = reports * 2
    _assert_clean(render_correlations_section(rich))


def test_csv_clean() -> None:
    cohort = CohortSlice(
        dimension="week",
        cells=[
            CohortCell(
                dimension_value="2026-W14",
                metric_name="read_edit_ratio",
                mean=0.42,
                stdev=0.12,
                n=37,
                min_value=0.05,
                max_value=0.91,
            )
        ],
        session_count=37,
        excluded_null_count=0,
    )
    _assert_clean(render_group_by_csv(cohort))


def test_json_clean() -> None:
    cohort = CohortSlice(
        dimension="week",
        cells=[
            CohortCell(
                dimension_value="2026-W14",
                metric_name="thinking_visible_chars_median",
                mean=320.0,
                stdev=110.0,
                n=37,
                min_value=80.0,
                max_value=900.0,
            )
        ],
        session_count=37,
        excluded_null_count=0,
    )
    _assert_clean(render_group_by_json(cohort))


def test_banned_set_includes_phase4_additions() -> None:
    for word in ("because of", "due to", "results in", "responsible for"):
        assert word in BANNED_CAUSAL_WORDS
