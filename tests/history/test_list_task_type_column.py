"""Tests for task_type column visibility in ``history list``.

The task_type column is HIDDEN — not just empty — when no session in the
result set has a session_task_type value. It appears only when at least one
session carries a non-None task type.

Rich truncates column content when the terminal is narrow. Tests that check
for the column header or cell values use short substrings that survive the
default 80-column truncation (Rich renders "task_…" for "task_type …").
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.list_cmd import _build_table


def _make_reports(
    store: SessionStore,
    session_id: str,
    *,
    session_task_type: str | None = None,
) -> None:
    report = build_report(
        session_id=session_id,
        project_hash="proj-hash",
        project_name=None,
        model="gpt-4.1",
        permission_mode="default",
        started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        event_count=10,
        parse_confidence=0.99,
        metrics={},
        session_task_type=session_task_type,
    )
    store.write(report)


def _render_table(reports: list, *, classifier_experimental: bool = True) -> str:
    """Render a table to a wide StringIO to avoid truncation."""
    import rich.console

    tbl = _build_table(reports, classifier_experimental=classifier_experimental)
    buf = io.StringIO()
    console = rich.console.Console(file=buf, highlight=False, width=200)
    console.print(tbl)
    return buf.getvalue()


class TestTaskTypeColumnHidden:
    def test_column_absent_when_no_session_has_task_type(self, tmp_path: Path) -> None:
        """task_type column header is absent when all sessions lack a task type."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-notask1", session_task_type=None)
        _make_reports(store, "agent-notask2", session_task_type=None)
        reports = store.list_reports()
        text = _render_table(reports)
        assert "task_type" not in text

    def test_column_absent_for_empty_store(self, tmp_path: Path) -> None:
        """task_type column header is absent for an empty result set."""
        text = _render_table([])
        assert "task_type" not in text

    def test_column_present_when_at_least_one_session_has_task_type(self, tmp_path: Path) -> None:
        """task_type column appears when any session carries a non-None task type."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-withtask1", session_task_type="exploration")
        _make_reports(store, "agent-notask3", session_task_type=None)
        reports = store.list_reports()
        text = _render_table(reports)
        assert "task_type" in text

    def test_column_shows_label_value(self, tmp_path: Path) -> None:
        """task_type column cells display the session_task_type label."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-label1", session_task_type="debug_loop")
        reports = store.list_reports()
        text = _render_table(reports)
        assert "debug_loop" in text

    def test_column_shows_dash_for_sessions_without_task_type(self, tmp_path: Path) -> None:
        """Sessions without a task type show '—' in the task_type column."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-withtask2", session_task_type="planning")
        _make_reports(store, "agent-notask4", session_task_type=None)
        reports = store.list_reports()
        text = _render_table(reports)
        assert "planning" in text
        # The em-dash sentinel is present for the session without a type.
        assert "—" in text

    def test_experimental_badge_in_header_when_experimental_true(self, tmp_path: Path) -> None:
        """Column header carries [experimental] badge when classifier_experimental=True."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-badge1", session_task_type="mutation_heavy")
        reports = store.list_reports()
        text = _render_table(reports, classifier_experimental=True)
        assert "[experimental]" in text

    def test_no_badge_when_experimental_false(self, tmp_path: Path) -> None:
        """No [experimental] badge when classifier_experimental=False."""
        store = SessionStore(base_dir=tmp_path)
        _make_reports(store, "agent-nobadge1", session_task_type="planning")
        reports = store.list_reports()
        text = _render_table(reports, classifier_experimental=False)
        assert "[experimental]" not in text
        assert "task_type" in text
