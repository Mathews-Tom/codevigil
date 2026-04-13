"""Tests for codevigil.history.detail_cmd.

Covers both the rich and plain-Markdown render paths.
The rich path is tested by monkeypatching RICH to a real or mock object.
The plain-Markdown path is tested with RICH=None.
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


class TestRunDetailMarkdown:
    """Plain Markdown path (rich absent)."""

    def test_default_out_uses_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.detail_cmd as detail_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(detail_module, "RICH", None)
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

    def test_found_session_returns_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import codevigil.history as history_module

        monkeypatch.setattr(history_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail1")
        out = io.StringIO()
        code = run_detail("agent-detail1", store_dir=tmp_path, out=out)
        assert code == 0

    def test_header_fields_present_in_markdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module

        monkeypatch.setattr(history_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail2", project_name="cool-project", model="gpt-5")
        out = io.StringIO()
        run_detail("agent-detail2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "cool-project" in text
        assert "gpt-5" in text
        assert "2026-04-14 10:00" in text

    def test_metrics_table_in_markdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module

        monkeypatch.setattr(history_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        # stop_phrase=1.5: >= 1.0 warn, < 3.0 crit -> warn
        # read_edit_ratio=4.2: >= 4.0 warn threshold -> ok (inverted: lower is worse)
        _write_session(store, "agent-detail3", metrics={"stop_phrase": 1.5, "read_edit_ratio": 4.2})
        out = io.StringIO()
        run_detail("agent-detail3", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "stop_phrase" in text
        assert "read_edit_ratio" in text
        # Severity label for stop_phrase=1.5 is warn
        assert "warn" in text

    def test_session_with_no_metrics_renders_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module

        monkeypatch.setattr(history_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-detail4", metrics={})
        out = io.StringIO()
        code = run_detail("agent-detail4", store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        assert "Metrics" in text


class TestDetailSnippetBranch:
    """Cover the snippet rendering branch in both renderers."""

    def test_markdown_renderer_shows_snippets_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.detail_cmd as detail_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(detail_module, "RICH", None)
        monkeypatch.setattr(
            detail_module,
            "_extract_stop_phrase_snippets",
            lambda _: ["context around hit one", "context around hit two"],
        )
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-snip1")
        out = io.StringIO()
        run_detail("agent-snip1", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "context around hit one" in text
        assert "Stop-Phrase Context Snippets" in text

    def test_rich_renderer_shows_snippets_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        try:
            import rich  # noqa: F401
        except ImportError:
            pytest.skip("rich not installed")

        import codevigil.history.detail_cmd as detail_module

        monkeypatch.setattr(
            detail_module,
            "_extract_stop_phrase_snippets",
            lambda _: ["snip snippet context"],
        )
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-snip2")
        out = io.StringIO()
        run_detail("agent-snip2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "snip snippet context" in text


class TestRunDetailRich:
    """Rich render path (monkeypatched to a real rich module or skipped)."""

    def test_rich_path_renders_without_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When rich is available, the output still contains session info."""
        try:
            import rich  # noqa: F401
        except ImportError:
            pytest.skip("rich not installed")

        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-rich1", project_name="rich-proj")
        out = io.StringIO()
        code = run_detail("agent-rich1", store_dir=tmp_path, out=out)
        assert code == 0
        # rich writes ANSI; the text will contain session id
        text = out.getvalue()
        assert "rich-proj" in text or "agent-rich1" in text

    def test_rich_absent_uses_markdown_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codevigil.history as history_module
        import codevigil.history.detail_cmd as detail_module

        monkeypatch.setattr(history_module, "RICH", None)
        monkeypatch.setattr(detail_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-norich2", project_name="md-project")
        out = io.StringIO()
        code = run_detail("agent-norich2", store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        assert "md-project" in text
        # Markdown heading present
        assert "#" in text
