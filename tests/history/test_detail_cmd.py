"""Tests for codevigil.history.detail_cmd.

All rendering goes through rich. Tests capture output via io.StringIO and
verify content without caring about box-drawing characters or ANSI codes.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.detail_cmd import run_detail


def _write_session(store: SessionStore, session_id: str, **kwargs: object) -> None:
    defaults = {
        "project_hash": "proj-hash",
        "project_name": "my-project",
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 42,
        "parse_confidence": 0.98,
        "metrics": {"stop_phrase": 0.0, "read_edit_ratio": 2.0},
    }
    defaults.update(kwargs)
    report = build_report(session_id=session_id, **defaults)  # type: ignore[arg-type]
    store.write(report)


class TestRunDetail:
    def test_default_out_uses_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-stdout1")
        with patch("codevigil.history.detail_cmd.SessionStore") as mock_cls:
            mock_cls.return_value = store
            code = run_detail("agent-stdout1")
        assert code == 0
        captured = capsys.readouterr()
        assert "agent-stdout1" in captured.out

    def test_missing_session_returns_1(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = run_detail("no-such-id", store_dir=tmp_path, out=out)
        assert code == 1
        assert "not found" in out.getvalue()

    def test_found_session_returns_0(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail1")
        out = io.StringIO()
        code = run_detail("agent-detail1", store_dir=tmp_path, out=out)
        assert code == 0

    def test_header_fields_present(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail2", project_name="cool-project", model="gpt-5")
        out = io.StringIO()
        run_detail("agent-detail2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "cool-project" in text
        assert "gpt-5" in text
        assert "2026-04-14 10:00" in text

    def test_metrics_table_present(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail3", metrics={"stop_phrase": 1.5, "read_edit_ratio": 4.2})
        out = io.StringIO()
        run_detail("agent-detail3", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "stop_phrase" in text
        assert "read_edit_ratio" in text
        assert "Metrics" in text

    def test_session_with_no_metrics_renders_cleanly(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail4", metrics={})
        out = io.StringIO()
        code = run_detail("agent-detail4", store_dir=tmp_path, out=out)
        assert code == 0
        assert "Metrics" in out.getvalue()


class TestDetailSnippetBranch:
    def test_renderer_shows_snippets_when_present(self, tmp_path: Path) -> None:
        import codevigil.history.detail_cmd as detail_module

        patch_target = "codevigil.history.detail_cmd._extract_stop_phrase_snippets"
        with patch(patch_target, return_value=["context around hit one", "context around hit two"]):
            store = SessionStore(base_dir=tmp_path)
            _write_session(store, "agent-snip1")
            out = io.StringIO()
            run_detail("agent-snip1", store_dir=tmp_path, out=out)

        text = out.getvalue()
        assert "context around hit one" in text
        assert "Stop-Phrase Context Snippets" in text
        _ = detail_module  # suppress unused-import warning
