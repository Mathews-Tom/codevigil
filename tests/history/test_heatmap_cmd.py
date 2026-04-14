"""Tests for codevigil.history.heatmap_cmd."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.heatmap_cmd import run_heatmap


def _write_session(store: SessionStore, session_id: str, **kwargs: object) -> None:
    defaults: dict[str, object] = {
        "project_hash": "proj-hash",
        "project_name": None,
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 10,
        "parse_confidence": 0.99,
        "metrics": {"stop_phrase": 0.0, "read_edit_ratio": 2.0},
    }
    defaults.update(kwargs)
    report = build_report(session_id=session_id, **defaults)  # type: ignore[arg-type]
    store.write(report)


class TestHeatmapSessionNotFound:
    def test_exits_1_when_session_missing(self, tmp_path: Path) -> None:
        out = io.StringIO()
        err = io.StringIO()
        code = run_heatmap("no-such-session", store_dir=tmp_path, out=out, err=err)
        assert code == 1
        assert "not found" in out.getvalue()


class TestHeatmapPresent:
    def test_exits_0_with_real_session(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-heat1")
        out = io.StringIO()
        err = io.StringIO()
        code = run_heatmap("agent-heat1", store_dir=tmp_path, out=out, err=err)
        assert code == 0

    def test_output_contains_metric_name(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-heat2", metrics={"stop_phrase": 0.0})
        out = io.StringIO()
        err = io.StringIO()
        run_heatmap("agent-heat2", store_dir=tmp_path, out=out, err=err)
        assert "stop_phrase" in out.getvalue()

    def test_cells_render_gradient_bars(self, tmp_path: Path) -> None:
        """Cells show Unicode block glyphs, not raw numeric strings."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(
            store,
            "agent-heat3",
            metrics={"stop_phrase": 0.0, "read_edit_ratio": 4.0},
        )
        out = io.StringIO()
        run_heatmap("agent-heat3", store_dir=tmp_path, out=out)
        text = out.getvalue()
        # At least one gradient glyph must appear in the rendered output.
        assert "█" in text
        # Numeric metric values must not appear as raw decimal strings.
        assert "4.0000" not in text
        assert "0.0000" not in text
