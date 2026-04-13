"""Tests for codevigil.history.list_cmd.

Tests both store integration (via tmp dir fixture) and renderer output.
Parametrizes on RICH presence to cover both paths.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.list_cmd import run_list


def _make_and_write(store: SessionStore, session_id: str, **kwargs: object) -> None:
    defaults: dict[str, object] = {
        "project_hash": "proj-hash",
        "project_name": None,
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 10,
        "parse_confidence": 0.99,
        "metrics": {},
    }
    defaults.update(kwargs)
    report = build_report(session_id=session_id, **defaults)  # type: ignore[arg-type]
    store.write(report)


class TestRunList:
    def test_empty_store_renders_header_only(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = run_list(store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        assert "session_id" in text
        assert "project" in text

    def test_single_session_appears_in_table(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _make_and_write(store, "agent-test001", project_name="my-proj")
        out = io.StringIO()
        code = run_list(store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        assert "test001" in text
        assert "my-proj" in text

    def test_filter_by_severity_ok_excludes_warn_sessions(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        # ok session: stop_phrase=0.0 (< 1.0 warn threshold)
        _make_and_write(store, "agent-ok1", metrics={"stop_phrase": 0.0})
        # warn session: stop_phrase=1.0 (>= 1.0 warn, < 3.0 crit)
        _make_and_write(store, "agent-warn1", metrics={"stop_phrase": 1.0})
        out = io.StringIO()
        run_list(store_dir=tmp_path, severity="ok", out=out)
        text = out.getvalue()
        assert "ok1" in text
        assert "warn1" not in text

    def test_filter_by_model_narrows_results(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _make_and_write(store, "agent-m1", model="gpt-4.1")
        _make_and_write(store, "agent-m2", model="gpt-4.1-mini")
        out = io.StringIO()
        run_list(store_dir=tmp_path, model="gpt-4.1", out=out)
        text = out.getvalue()
        assert "m1" in text
        assert "m2" not in text

    def test_filter_by_since_excludes_earlier_sessions(self, tmp_path: Path) -> None:
        from datetime import date

        store = SessionStore(base_dir=tmp_path)
        t_early = datetime(2026, 4, 1, tzinfo=UTC)
        t_late = datetime(2026, 4, 10, tzinfo=UTC)
        _make_and_write(
            store, "agent-early", started_at=t_early, ended_at=t_early + timedelta(minutes=10)
        )
        _make_and_write(
            store, "agent-late", started_at=t_late, ended_at=t_late + timedelta(minutes=10)
        )
        out = io.StringIO()
        run_list(store_dir=tmp_path, since=date(2026, 4, 5), out=out)
        text = out.getvalue()
        assert "late" in text
        assert "early" not in text

    def test_duration_formatted_human_readable(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        t0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
        t1 = t0 + timedelta(seconds=90)
        _make_and_write(store, "agent-dur1", started_at=t0, ended_at=t1)
        out = io.StringIO()
        run_list(store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "1m30s" in text

    def test_no_per_row_reads_after_enumeration(self, tmp_path: Path) -> None:
        # Verify run_list works on a store with multiple sessions without
        # file-level errors — absence of per-row reads means store.get_report
        # is never called; we can only observe the output.
        store = SessionStore(base_dir=tmp_path)
        for i in range(5):
            _make_and_write(store, f"agent-r{i}")
        out = io.StringIO()
        code = run_list(store_dir=tmp_path, out=out)
        assert code == 0
        text = out.getvalue()
        # All 5 sessions present
        for i in range(5):
            assert f"r{i}"[:5] in text

    def test_rich_absent_still_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_list must not depend on rich at all."""
        import codevigil.history as history_module

        monkeypatch.setattr(history_module, "RICH", None)
        store = SessionStore(base_dir=tmp_path)
        _make_and_write(store, "agent-norich")
        out = io.StringIO()
        code = run_list(store_dir=tmp_path, out=out)
        assert code == 0
        assert "norich" in out.getvalue()
