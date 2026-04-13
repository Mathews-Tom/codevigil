"""Shared fixtures for codevigil.report tests.

Provides a synthetic corpus of 35 SessionReport objects spanning three ISO
weeks and two projects. The corpus is designed to:

- Include cells with n >= 5 (to pass the sample-size guard) in the weekly
  trend table.
- Include at least one group where n < 5 (period B in compare-periods) so
  the guard is exercised.
- Have realistic but synthetic metric values for write_precision, read_edit_ratio,
  stop_phrase, and reasoning_loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codevigil.analysis.store import SessionReport, build_report


def _make_report(
    session_id: str,
    *,
    started_at: datetime,
    metrics: dict[str, float],
    project_hash: str = "proj-alpha",
) -> SessionReport:
    return build_report(
        session_id=session_id,
        project_hash=project_hash,
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=45),
        event_count=60,
        parse_confidence=0.98,
        metrics=metrics,
    )


# Week anchors: Monday of each ISO week in our fixture.
# 2026-W14: Mon 2026-03-30 .. Sun 2026-04-05
# 2026-W15: Mon 2026-04-06 .. Sun 2026-04-12
# 2026-W16: Mon 2026-04-13 .. Sun 2026-04-19
_W14_MON = datetime(2026, 3, 30, 10, 0, 0, tzinfo=UTC)
_W15_MON = datetime(2026, 4, 6, 10, 0, 0, tzinfo=UTC)
_W16_MON = datetime(2026, 4, 13, 10, 0, 0, tzinfo=UTC)


def _make_week_sessions(
    week_anchor: datetime,
    *,
    n: int,
    base_id: str,
    read_edit: float,
    stop_phrase: float,
    reasoning_loop: float,
    write_precision: float,
    project_hash: str = "proj-alpha",
) -> list[SessionReport]:
    """Generate ``n`` sessions within the same ISO week."""
    reports = []
    for i in range(n):
        ts = week_anchor + timedelta(hours=i * 6)
        reports.append(
            _make_report(
                f"{base_id}-{i:02d}",
                started_at=ts,
                metrics={
                    "read_edit_ratio": read_edit + i * 0.05,
                    "stop_phrase": stop_phrase,
                    "reasoning_loop": reasoning_loop + i * 0.2,
                    "write_precision": write_precision,
                },
                project_hash=project_hash,
            )
        )
    return reports


@pytest.fixture
def corpus_35() -> list[SessionReport]:
    """35 synthetic session reports spanning three ISO weeks.

    W14: 10 sessions — high read:edit, low write_precision (surgical edits)
    W15: 15 sessions — mixed metrics
    W16: 10 sessions — lower read:edit, higher write_precision (more writes)
    """
    w14 = _make_week_sessions(
        _W14_MON,
        n=10,
        base_id="w14",
        read_edit=6.5,
        stop_phrase=0.5,
        reasoning_loop=8.0,
        write_precision=0.2,
    )
    w15 = _make_week_sessions(
        _W15_MON,
        n=15,
        base_id="w15",
        read_edit=4.8,
        stop_phrase=1.0,
        reasoning_loop=10.5,
        write_precision=0.4,
    )
    w16 = _make_week_sessions(
        _W16_MON,
        n=10,
        base_id="w16",
        read_edit=3.1,
        stop_phrase=1.5,
        reasoning_loop=14.0,
        write_precision=0.65,
    )
    return w14 + w15 + w16


@pytest.fixture
def corpus_small_period_b() -> list[SessionReport]:
    """Corpus where period B has only 3 sessions (below the n<5 guard).

    Period A: W14 (10 sessions) — passes guard
    Period B: 3 sessions in W16 — fails guard (n=3 < 5)
    """
    period_a = _make_week_sessions(
        _W14_MON,
        n=10,
        base_id="small-a",
        read_edit=6.6,
        stop_phrase=0.3,
        reasoning_loop=7.5,
        write_precision=0.15,
    )
    period_b = _make_week_sessions(
        _W16_MON,
        n=3,
        base_id="small-b",
        read_edit=2.0,
        stop_phrase=2.5,
        reasoning_loop=18.0,
        write_precision=0.8,
    )
    return period_a + period_b
