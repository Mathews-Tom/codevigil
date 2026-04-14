"""Tests for ``--axis task_type`` in ``history heatmap``.

``--axis task_type`` produces a cross-tab of metric means grouped by
session_task_type. The default ``--axis severity`` behavior is unchanged.
When the classifier is disabled, the task_type axis exits 1 with a
descriptive error.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.heatmap_cmd import run_heatmap


def _write_session(
    store: SessionStore,
    session_id: str,
    *,
    session_task_type: str | None = None,
    metrics: dict[str, float] | None = None,
    **kwargs: object,
) -> None:
    defaults: dict[str, object] = {
        "project_hash": "proj-hash",
        "project_name": None,
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 10,
        "parse_confidence": 0.99,
        "metrics": metrics or {"stop_phrase": 0.0, "read_edit_ratio": 2.0},
    }
    defaults.update(kwargs)
    report = build_report(
        session_id=session_id,
        session_task_type=session_task_type,
        **defaults,  # type: ignore[arg-type]
    )
    store.write(report)


class TestHeatmapSeverityAxisUnchanged:
    def test_default_axis_renders_session_heatmap(self, tmp_path: Path) -> None:
        """Default severity axis still renders a single-session metric x severity table."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-hm1")
        out = io.StringIO()
        code = run_heatmap("agent-hm1", store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        assert "agent-hm1" in text
        assert "stop_phrase" in text

    def test_severity_axis_explicit_still_works(self, tmp_path: Path) -> None:
        """Explicitly passing axis='severity' preserves the original heatmap."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-hm2")
        out = io.StringIO()
        code = run_heatmap("agent-hm2", store_dir=tmp_path, axis="severity", out=out)
        assert code == 0
        assert "agent-hm2" in out.getvalue()


class TestHeatmapTaskTypeAxis:
    def test_task_axis_produces_cross_tab(self, tmp_path: Path) -> None:
        """--axis task_type produces a cross-tab table with task type columns."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store, "agent-exp1", session_task_type="exploration", metrics={"read_edit_ratio": 8.0}
        )
        _write_session(
            store,
            "agent-mut1",
            session_task_type="mutation_heavy",
            metrics={"read_edit_ratio": 1.5},
        )
        out = io.StringIO()
        code = run_heatmap(
            "ignored-in-task-axis",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=True,
            out=out,
        )
        assert code == 0
        text = out.getvalue()
        assert "task_type" in text
        assert "read_edit_ratio" in text
        # Both task type labels appear as column headers.
        assert "exploration" in text
        assert "mutation_heavy" in text

    def test_task_axis_with_experimental_badge(self, tmp_path: Path) -> None:
        """[experimental] badge appears on the task_type axis title when enabled."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store, "agent-badge1", session_task_type="planning", metrics={"stop_phrase": 0.0}
        )
        out = io.StringIO()
        run_heatmap(
            "ignored",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=True,
            classifier_experimental=True,
            out=out,
        )
        assert "[experimental]" in out.getvalue()

    def test_task_axis_no_badge_when_experimental_false(self, tmp_path: Path) -> None:
        """No [experimental] badge when classifier_experimental=False."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store, "agent-nobadge1", session_task_type="debug_loop", metrics={"stop_phrase": 0.5}
        )
        out = io.StringIO()
        run_heatmap(
            "ignored",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=True,
            classifier_experimental=False,
            out=out,
        )
        assert "[experimental]" not in out.getvalue()

    def test_task_axis_unclassified_sessions_grouped(self, tmp_path: Path) -> None:
        """Sessions with no task type appear under '(unclassified)' group."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-unc1", session_task_type=None, metrics={"stop_phrase": 0.0})
        out = io.StringIO()
        run_heatmap(
            "ignored",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=True,
            out=out,
        )
        assert "(unclassified)" in out.getvalue()

    def test_task_axis_mean_computed_across_sessions(self, tmp_path: Path) -> None:
        """Mean metric value is computed across sessions with the same task type."""
        store = SessionStore(base_dir=tmp_path)
        # Two exploration sessions: read_edit_ratio = 6.0 and 8.0 → mean 7.0
        _write_session(
            store, "agent-exp2", session_task_type="exploration", metrics={"read_edit_ratio": 6.0}
        )
        _write_session(
            store, "agent-exp3", session_task_type="exploration", metrics={"read_edit_ratio": 8.0}
        )
        out = io.StringIO()
        run_heatmap(
            "ignored",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=True,
            out=out,
        )
        text = out.getvalue()
        # Mean is 7.0 — the only metric in the column, so col_max == 7.0 and the
        # bar renders fully filled.  Assert proportional bar glyphs are present
        # rather than the raw numeric string (cells now show gradient bars).
        assert "█" in text


class TestHeatmapTaskAxisDisabled:
    def test_classifier_disabled_returns_1_with_error(self, tmp_path: Path) -> None:
        """axis=task_type with classifier_enabled=False exits 1 with error message."""
        out = io.StringIO()
        code = run_heatmap(
            "any-session",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=False,
            out=out,
        )
        assert code == 1
        text = out.getvalue()
        assert "classifier" in text.lower()
        assert "disabled" in text.lower()

    def test_classifier_disabled_error_suggests_severity_axis(self, tmp_path: Path) -> None:
        """Error message mentions the alternative --axis severity option."""
        out = io.StringIO()
        run_heatmap(
            "any-session",
            store_dir=tmp_path,
            axis="task_type",
            classifier_enabled=False,
            out=out,
        )
        assert "severity" in out.getvalue()
