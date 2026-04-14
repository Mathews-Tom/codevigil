"""Period comparison over adjacent days with a straddling session.

Phase 2 acceptance test: when a session straddles a date boundary (e.g.
2026-01-01 23:50 → 2026-01-02 00:10), splitting it via event-level filtering
and passing each half to compare_periods must produce symmetric event counts.

Symmetry means: events_in_jan1_half + events_in_jan2_half == total_events.
Neither half should contain events from the other day.

Uses the midnight-straddle fixture from
tests/fixtures/midnight_straddle/straddle.jsonl directly through
load_reports_from_jsonl to exercise the full event-pipeline filter path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from codevigil.analysis.compare import compare_periods
from codevigil.report.loader import load_reports_from_jsonl

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "midnight_straddle" / "straddle.jsonl"

# Window boundaries (naive datetimes as produced by the CLI's _parse_date_filter).
_JAN1_START = datetime(2026, 1, 1, 0, 0, 0)
_JAN1_END = datetime(2026, 1, 1, 23, 59, 59)
_JAN2_START = datetime(2026, 1, 2, 0, 0, 0)
_JAN2_END = datetime(2026, 1, 2, 23, 59, 59)


def test_adjacent_day_split_is_symmetric() -> None:
    """Events in the two halves of a straddling session sum to the total.

    Loads the straddle fixture three ways:
    1. No filter — all events (total).
    2. --to 2026-01-01 — only pre-midnight events (jan1 half).
    3. --from 2026-01-02 — only post-midnight events (jan2 half).

    The jan1 + jan2 event counts must equal the total, demonstrating that the
    event-level filter partitions the session exactly at the midnight boundary
    with no double-counting and no loss.
    """
    total_reports = load_reports_from_jsonl([_FIXTURE_PATH])
    assert len(total_reports) == 1
    total_events = total_reports[0].event_count

    jan1_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        to_timestamp=_JAN1_END,
    )
    assert len(jan1_reports) == 1, "pre-midnight filter should yield exactly one report"
    jan1_events = jan1_reports[0].event_count

    jan2_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        from_timestamp=_JAN2_START,
    )
    assert len(jan2_reports) == 1, "post-midnight filter should yield exactly one report"
    jan2_events = jan2_reports[0].event_count

    assert jan1_events + jan2_events == total_events, (
        f"jan1 ({jan1_events}) + jan2 ({jan2_events}) = {jan1_events + jan2_events} "
        f"!= total ({total_events}); event-level filter is not partitioning cleanly"
    )


def test_compare_periods_straddling_session_produces_nonzero_counts() -> None:
    """compare_periods on the two halves produces non-zero session counts.

    Regression: if the filter incorrectly returned empty reports, compare_periods
    would see n_sessions_a=0 or n_sessions_b=0. This asserts both sides have
    at least one session after entry-level filtering.
    """
    jan1_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        to_timestamp=_JAN1_END,
    )
    jan2_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        from_timestamp=_JAN2_START,
    )

    result = compare_periods(jan1_reports, jan2_reports)
    assert result.n_sessions_a > 0, "jan1 period must have at least one session"
    assert result.n_sessions_b > 0, "jan2 period must have at least one session"


def test_started_at_boundary_consistency() -> None:
    """started_at of each half must fall within its respective day.

    Validates that the clamped started_at is actually clamped to the in-window
    range and not left at the raw session start.
    """
    jan1_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        to_timestamp=_JAN1_END,
    )
    jan2_reports = load_reports_from_jsonl(
        [_FIXTURE_PATH],
        from_timestamp=_JAN2_START,
    )

    assert len(jan1_reports) == 1
    assert len(jan2_reports) == 1

    jan1_started = jan1_reports[0].started_at.replace(tzinfo=None)
    jan2_started = jan2_reports[0].started_at.replace(tzinfo=None)

    assert jan1_started < _JAN2_START, (
        f"jan1 started_at {jan1_started} is on or after jan2 start {_JAN2_START}"
    )
    assert jan2_started >= _JAN2_START, (
        f"jan2 started_at {jan2_started} is before jan2 start {_JAN2_START}"
    )
