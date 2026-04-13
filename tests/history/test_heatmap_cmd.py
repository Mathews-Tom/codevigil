"""Tests for codevigil.history.heatmap_cmd.

The heatmap is gated behind the rich extra. Tests cover:
1. Rich-absent path: exits 2 with install hint on stderr.
2. Rich-present path: exits 0 and renders without crash.
3. Session-not-found path: exits 1.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


class TestHeatmapRichAbsent:
    """Path: rich not installed (monkeypatched to None)."""

    def test_exits_2_when_rich_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.heatmap_cmd as heatmap_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(heatmap_module, "RICH", None)

        out = io.StringIO()
        err = io.StringIO()
        code = run_heatmap("any-session", store_dir=tmp_path, out=out, err=err)
        assert code == 2

    def test_install_hint_on_stderr_when_rich_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.heatmap_cmd as heatmap_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(heatmap_module, "RICH", None)

        out = io.StringIO()
        err = io.StringIO()
        run_heatmap("any-session", store_dir=tmp_path, out=out, err=err)
        assert "uv add" in err.getvalue()
        assert "codevigil[rich]" in err.getvalue()

    def test_nothing_on_stdout_when_rich_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.heatmap_cmd as heatmap_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(heatmap_module, "RICH", None)

        out = io.StringIO()
        err = io.StringIO()
        run_heatmap("any-session", store_dir=tmp_path, out=out, err=err)
        assert out.getvalue() == ""


class TestHeatmapSessionNotFound:
    def test_exits_1_when_session_missing(self, tmp_path: Path) -> None:
        try:
            import rich  # noqa: F401
        except ImportError:
            pytest.skip("rich not installed — cannot test session-not-found path")

        out = io.StringIO()
        err = io.StringIO()
        code = run_heatmap("no-such-session", store_dir=tmp_path, out=out, err=err)
        assert code == 1
        assert "not found" in out.getvalue()


class TestHeatmapRichPresent:
    """Path: rich is installed."""

    def test_exits_0_with_real_session(self, tmp_path: Path) -> None:
        try:
            import rich  # noqa: F401
        except ImportError:
            pytest.skip("rich not installed")

        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-heat1")
        out = io.StringIO()
        err = io.StringIO()
        code = run_heatmap("agent-heat1", store_dir=tmp_path, out=out, err=err)
        assert code == 0

    def test_output_contains_metric_name(self, tmp_path: Path) -> None:
        try:
            import rich  # noqa: F401
        except ImportError:
            pytest.skip("rich not installed")

        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-heat2", metrics={"stop_phrase": 0.0})
        out = io.StringIO()
        err = io.StringIO()
        run_heatmap("agent-heat2", store_dir=tmp_path, out=out, err=err)
        text = out.getvalue()
        assert "stop_phrase" in text
