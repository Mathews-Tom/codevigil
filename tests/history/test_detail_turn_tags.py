"""Tests for per-turn task type headings in ``history detail``.

When a session carries ``turn_task_types``, the detail view renders a
``Turn Task Types`` panel with one heading per turn annotated with its task
type label and an ``[experimental]`` badge. When no turn data is available
(None or empty), this panel is absent.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.detail_cmd import run_detail


def _write_session(
    store: SessionStore,
    session_id: str,
    *,
    turn_task_types: tuple[str, ...] | None = None,
    session_task_type: str | None = None,
    **kwargs: object,
) -> None:
    defaults: dict[str, object] = {
        "project_hash": "proj-hash",
        "project_name": "test-project",
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 20,
        "parse_confidence": 0.98,
        "metrics": {},
    }
    defaults.update(kwargs)
    report = build_report(
        session_id=session_id,
        turn_task_types=turn_task_types,
        session_task_type=session_task_type,
        **defaults,  # type: ignore[arg-type]
    )
    store.write(report)


class TestDetailTurnTags:
    def test_turn_task_panel_absent_when_no_turn_data(self, tmp_path: Path) -> None:
        """Turn Task Types panel is absent when turn_task_types is None."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-noturn1", turn_task_types=None)
        out = io.StringIO()
        run_detail("agent-noturn1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "Turn Task Types" not in text

    def test_turn_task_panel_absent_for_empty_turn_types(self, tmp_path: Path) -> None:
        """Turn Task Types panel is absent when turn_task_types is an empty tuple."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-empty1", turn_task_types=())
        out = io.StringIO()
        run_detail("agent-empty1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "Turn Task Types" not in text

    def test_turn_task_panel_present_with_turn_data(self, tmp_path: Path) -> None:
        """Turn Task Types panel appears when turn_task_types has values."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-turns1", turn_task_types=("exploration", "mutation_heavy"))
        out = io.StringIO()
        run_detail("agent-turns1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "Turn Task Types" in text

    def test_turn_task_labels_shown_per_turn(self, tmp_path: Path) -> None:
        """Each turn's task type label appears in the output."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store, "agent-turns2", turn_task_types=("exploration", "debug_loop", "planning")
        )
        out = io.StringIO()
        run_detail("agent-turns2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "exploration" in text
        assert "debug_loop" in text
        assert "planning" in text

    def test_turn_task_labels_indexed(self, tmp_path: Path) -> None:
        """Turn headings include a 1-based index."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-turns3", turn_task_types=("exploration", "mutation_heavy"))
        out = io.StringIO()
        run_detail("agent-turns3", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "Turn 1" in text
        assert "Turn 2" in text

    def test_experimental_badge_on_turn_labels(self, tmp_path: Path) -> None:
        """[experimental] badge appears adjacent to turn task labels."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-turns4", turn_task_types=("exploration",))
        out = io.StringIO()
        run_detail("agent-turns4", store_dir=tmp_path, classifier_experimental=True, out=out)
        text = out.getvalue()
        assert "[experimental]" in text

    def test_no_badge_when_experimental_false(self, tmp_path: Path) -> None:
        """No [experimental] badge when classifier_experimental=False."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-turns5", turn_task_types=("planning",))
        out = io.StringIO()
        run_detail("agent-turns5", store_dir=tmp_path, classifier_experimental=False, out=out)
        text = out.getvalue()
        assert "[experimental]" not in text
        assert "planning" in text

    def test_session_task_type_in_header_when_set(self, tmp_path: Path) -> None:
        """Session-level task type appears in the header when present."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store,
            "agent-sesstype1",
            session_task_type="debug_loop",
            turn_task_types=None,
        )
        out = io.StringIO()
        run_detail("agent-sesstype1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "task_type" in text
        assert "debug_loop" in text

    def test_session_task_type_absent_from_header_when_none(self, tmp_path: Path) -> None:
        """task_type line is absent from the header when session_task_type is None."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store,
            "agent-notype1",
            session_task_type=None,
            turn_task_types=None,
        )
        out = io.StringIO()
        run_detail("agent-notype1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        # "task_type:" with colon should not be in header lines when None.
        assert "task_type:" not in text


class TestDetailDisabledDegradation:
    def test_no_turn_panel_when_classifier_data_absent(self, tmp_path: Path) -> None:
        """No Turn Task Types panel when turn data is None (classifier was disabled)."""
        store = SessionStore(base_dir=tmp_path)
        # A session written with classifier.enabled=False has turn_task_types=None.
        _write_session(
            store,
            "agent-disabled1",
            session_task_type=None,
            turn_task_types=None,
        )
        out = io.StringIO()
        run_detail("agent-disabled1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "Turn Task Types" not in text
        assert "[experimental]" not in text
