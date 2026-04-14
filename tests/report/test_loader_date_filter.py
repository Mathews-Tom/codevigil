"""Tests for per-entry date filtering in codevigil.report.loader.

Covers Phase 2: event-level timestamp filtering via from_timestamp/to_timestamp
parameters on load_reports_from_jsonl. Uses the midnight-straddle fixture from
tests/fixtures/midnight_straddle/straddle.jsonl (events span 2026-01-01 23:50
through 2026-01-02 00:10 UTC).

The straddle fixture has 22 lines total: 1 session_start, 20 message events
(user/assistant interleaved, timestamps 23:50 through 00:09), and 1 session_stop.
The parser yields event objects only for message events, not for system lines.

Midnight boundary: 2026-01-01T23:59:00 is the last pre-midnight event, and
2026-01-02T00:00:00 is the first post-midnight event (a user message at 00:00).

This test file:
- Tests --from 2026-01-02 yields only post-midnight events with clamped started_at.
- Tests --to 2026-01-01 yields only pre-midnight events.
- Tests a window containing zero in-window events produces no report.
- Tests a window fully covering the session produces the same event count as
  no-filter, confirming the filter is transparent when it matches everything.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from codevigil.report.loader import load_reports_from_jsonl

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "midnight_straddle" / "straddle.jsonl"

# The straddle fixture spans 2026-01-01 23:50 → 2026-01-02 00:09 UTC.
# Boundaries used across tests (naive datetimes — the CLI produces these from
# date-only strings via _parse_date_filter).
_MIDNIGHT = datetime(2026, 1, 2, 0, 0, 0)  # start of 2026-01-02 (inclusive)
_END_OF_JAN1 = datetime(2026, 1, 1, 23, 59, 59)  # last second of 2026-01-01
_END_OF_JAN2 = datetime(2026, 1, 2, 23, 59, 59)  # last second of 2026-01-02


class TestMidnightStraddleFromFilter:
    """--from 2026-01-02 keeps only post-midnight events."""

    def test_yields_one_report(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=_MIDNIGHT,
        )
        assert len(reports) == 1

    def test_started_at_clamped_to_first_in_window_event(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=_MIDNIGHT,
        )
        assert len(reports) == 1
        # The first post-midnight event is at 2026-01-02T00:00:00 (naive comparison).
        started = reports[0].started_at.replace(tzinfo=None)
        assert started >= _MIDNIGHT, f"started_at {started} is before midnight bound {_MIDNIGHT}"

    def test_event_count_is_post_midnight_only(self) -> None:
        # No-filter count for comparison.
        unfiltered = load_reports_from_jsonl([_FIXTURE_PATH])
        assert len(unfiltered) == 1
        total_events = unfiltered[0].event_count

        filtered = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=_MIDNIGHT,
        )
        assert len(filtered) == 1
        post_midnight_events = filtered[0].event_count

        # Filtered count must be strictly less than total — there are pre-midnight
        # events in the fixture.
        assert post_midnight_events < total_events
        # And strictly positive — there are post-midnight events too.
        assert post_midnight_events > 0

    def test_ended_at_clamped_to_last_in_window_event(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=_MIDNIGHT,
        )
        assert len(reports) == 1
        # ended_at must also be on or after midnight (the last event is 00:09).
        ended = reports[0].ended_at.replace(tzinfo=None)
        assert ended >= _MIDNIGHT


class TestMidnightStraddleToFilter:
    """--to 2026-01-01 keeps only pre-midnight events."""

    def test_yields_one_report(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            to_timestamp=_END_OF_JAN1,
        )
        assert len(reports) == 1

    def test_event_count_is_pre_midnight_only(self) -> None:
        unfiltered = load_reports_from_jsonl([_FIXTURE_PATH])
        assert len(unfiltered) == 1
        total_events = unfiltered[0].event_count

        filtered = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            to_timestamp=_END_OF_JAN1,
        )
        assert len(filtered) == 1
        pre_midnight_events = filtered[0].event_count

        assert pre_midnight_events < total_events
        assert pre_midnight_events > 0

    def test_ended_at_clamped_to_last_in_window_event(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            to_timestamp=_END_OF_JAN1,
        )
        assert len(reports) == 1
        ended = reports[0].ended_at.replace(tzinfo=None)
        # ended_at must be at or before the last second of 2026-01-01.
        assert ended <= _END_OF_JAN1

    def test_started_at_matches_first_event_timestamp(self) -> None:
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            to_timestamp=_END_OF_JAN1,
        )
        assert len(reports) == 1
        # The first event in the fixture is at 2026-01-01 23:50:00.
        started = reports[0].started_at.replace(tzinfo=None)
        assert started == datetime(2026, 1, 1, 23, 50, 0)


class TestZeroEventWindow:
    """A window that contains zero events produces no report."""

    def test_future_window_yields_no_reports(self) -> None:
        # Fixture ends at 2026-01-02 00:10; use a window far in the future.
        from_ts = datetime(2030, 1, 1, 0, 0, 0)
        to_ts = datetime(2030, 12, 31, 23, 59, 59)
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=from_ts,
            to_timestamp=to_ts,
        )
        assert reports == []

    def test_past_window_yields_no_reports(self) -> None:
        # Fixture starts at 2026-01-01 23:50; use a window entirely before that.
        to_ts = datetime(2025, 12, 31, 23, 59, 59)
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            to_timestamp=to_ts,
        )
        assert reports == []

    def test_narrow_window_after_all_events_yields_no_reports(self) -> None:
        # The fixture's last event (system stop) is at 00:10. Use a window
        # that starts after the session has fully ended.
        from_ts = datetime(2026, 1, 2, 0, 11, 0)
        to_ts = datetime(2026, 1, 2, 0, 20, 0)
        reports = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=from_ts,
            to_timestamp=to_ts,
        )
        assert reports == []


class TestFullCoverageWindow:
    """A window fully covering the session produces the same output as no filter."""

    def test_event_count_matches_unfiltered(self) -> None:
        unfiltered = load_reports_from_jsonl([_FIXTURE_PATH])
        assert len(unfiltered) == 1

        # Window that comfortably covers the entire fixture.
        from_ts = datetime(2025, 1, 1, 0, 0, 0)
        to_ts = datetime(2027, 1, 1, 23, 59, 59)
        filtered = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=from_ts,
            to_timestamp=to_ts,
        )
        assert len(filtered) == 1
        assert filtered[0].event_count == unfiltered[0].event_count

    def test_session_id_matches_unfiltered(self) -> None:
        unfiltered = load_reports_from_jsonl([_FIXTURE_PATH])
        filtered = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=datetime(2025, 1, 1, 0, 0, 0),
            to_timestamp=datetime(2027, 1, 1, 23, 59, 59),
        )
        assert filtered[0].session_id == unfiltered[0].session_id

    def test_metrics_match_unfiltered(self) -> None:
        unfiltered = load_reports_from_jsonl([_FIXTURE_PATH])
        filtered = load_reports_from_jsonl(
            [_FIXTURE_PATH],
            from_timestamp=datetime(2025, 1, 1, 0, 0, 0),
            to_timestamp=datetime(2027, 1, 1, 23, 59, 59),
        )
        assert filtered[0].metrics == pytest.approx(unfiltered[0].metrics, rel=1e-9)
