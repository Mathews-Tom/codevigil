"""Tests for codevigil.history.diff_cmd.

Covers LCS metric alignment, missing-session error paths, and determinism.
rich is NOT used in the diff command — no monkeypatching needed.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.diff_cmd import _render_diff, run_diff


def _write_session(
    store: SessionStore,
    session_id: str,
    metrics: dict[str, float],
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
    }
    defaults.update(kwargs)
    report = build_report(session_id=session_id, metrics=metrics, **defaults)  # type: ignore[arg-type]
    store.write(report)


class TestRunDiff:
    def test_missing_session_a_returns_1(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-b1", metrics={})
        out = io.StringIO()
        code = run_diff("no-a", "agent-b1", store_dir=tmp_path, out=out)
        assert code == 1
        assert "not found" in out.getvalue()

    def test_missing_session_b_returns_1(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-a1", metrics={})
        out = io.StringIO()
        code = run_diff("agent-a1", "no-b", store_dir=tmp_path, out=out)
        assert code == 1
        assert "not found" in out.getvalue()

    def test_both_missing_reports_both_ids(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = run_diff("no-a", "no-b", store_dir=tmp_path, out=out)
        assert code == 1
        text = out.getvalue()
        assert "no-a" in text
        assert "no-b" in text

    def test_successful_diff_returns_0(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-da1", metrics={"stop_phrase": 0.0})
        _write_session(store, "agent-db1", metrics={"stop_phrase": 5.0})
        out = io.StringIO()
        code = run_diff("agent-da1", "agent-db1", store_dir=tmp_path, out=out)
        assert code == 0

    def test_diff_output_contains_session_ids(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-da2", metrics={})
        _write_session(store, "agent-db2", metrics={})
        out = io.StringIO()
        run_diff("agent-da2", "agent-db2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "agent-da2" in text
        assert "agent-db2" in text

    def test_diff_output_contains_metric_delta(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-da3", metrics={"stop_phrase": 0.5})
        _write_session(store, "agent-db3", metrics={"stop_phrase": 1.5})
        out = io.StringIO()
        run_diff("agent-da3", "agent-db3", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "stop_phrase" in text
        # delta = 1.5 - 0.5 = 1.0
        assert "+1.0000" in text

    def test_diff_output_shows_absent_metric(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-da4", metrics={"metric_only_a": 1.0})
        _write_session(store, "agent-db4", metrics={"metric_only_b": 2.0})
        out = io.StringIO()
        run_diff("agent-da4", "agent-db4", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "metric_only_a" in text
        assert "metric_only_b" in text
        assert "absent" in text

    def test_diff_shows_insert_for_metric_only_in_b(self, tmp_path: Path) -> None:
        # A has "aaa"; B has "aaa" + "bbb" -> equal("aaa") + insert("bbb")
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-ins1", metrics={"aaa": 1.0})
        _write_session(store, "agent-ins2", metrics={"aaa": 1.0, "bbb": 2.0})
        out = io.StringIO()
        run_diff("agent-ins1", "agent-ins2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "bbb" in text
        assert "absent" in text

    def test_diff_shows_delete_for_metric_only_in_a(self, tmp_path: Path) -> None:
        # A has "aaa" + "bbb"; B has "aaa" -> equal("aaa") + delete("bbb")
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-del1", metrics={"aaa": 1.0, "bbb": 2.0})
        _write_session(store, "agent-del2", metrics={"aaa": 1.0})
        out = io.StringIO()
        run_diff("agent-del1", "agent-del2", store_dir=tmp_path, out=out)
        text = out.getvalue()
        assert "bbb" in text
        assert "absent" in text

    def test_diff_is_deterministic(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-det1", metrics={"a": 1.0, "b": 2.0, "c": 3.0})
        _write_session(store, "agent-det2", metrics={"a": 1.5, "b": 2.5, "c": 3.5})
        out1 = io.StringIO()
        out2 = io.StringIO()
        run_diff("agent-det1", "agent-det2", store_dir=tmp_path, out=out1)
        run_diff("agent-det1", "agent-det2", store_dir=tmp_path, out=out2)
        assert out1.getvalue() == out2.getvalue()


class TestRenderDiff:
    """Unit tests for the _render_diff internal function."""

    def _make_report(
        self,
        session_id: str,
        metrics: dict[str, float],
        duration: float = 1800.0,
    ) -> object:
        t0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
        return build_report(
            session_id=session_id,
            project_hash="hash",
            project_name=None,
            model="gpt-4.1",
            permission_mode="default",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=duration),
            event_count=10,
            parse_confidence=0.99,
            metrics=metrics,
        )

    def test_equal_metrics_show_zero_delta(self) -> None:
        from codevigil.analysis.store import SessionReport

        a = self._make_report("a", {"stop_phrase": 5.0})
        b = self._make_report("b", {"stop_phrase": 5.0})
        assert isinstance(a, SessionReport)
        assert isinstance(b, SessionReport)
        result = _render_diff(a, b)
        assert "+0.0000" in result

    def test_negative_delta_shows_negative_sign(self) -> None:
        from codevigil.analysis.store import SessionReport

        a = self._make_report("a", {"stop_phrase": 10.0})
        b = self._make_report("b", {"stop_phrase": 5.0})
        assert isinstance(a, SessionReport)
        assert isinstance(b, SessionReport)
        result = _render_diff(a, b)
        assert "-5.0000" in result

    def test_duration_delta_in_header(self) -> None:
        from codevigil.analysis.store import SessionReport

        a = self._make_report("a", {}, duration=1800.0)
        b = self._make_report("b", {}, duration=3600.0)
        assert isinstance(a, SessionReport)
        assert isinstance(b, SessionReport)
        result = _render_diff(a, b)
        # delta = 3600 - 1800 = +1800s
        assert "+1800s" in result

    def test_lcs_aligns_shared_metrics(self) -> None:
        from codevigil.analysis.store import SessionReport

        a = self._make_report("a", {"read_edit_ratio": 2.0, "stop_phrase": 1.0})
        b = self._make_report("b", {"read_edit_ratio": 3.0, "stop_phrase": 2.0})
        assert isinstance(a, SessionReport)
        assert isinstance(b, SessionReport)
        result = _render_diff(a, b)
        # Both metrics should appear on rows (not as absent)
        assert "read_edit_ratio" in result
        assert "stop_phrase" in result
        # No absent markers since both exist in both
        lines_with_absent = [ln for ln in result.splitlines() if "absent" in ln]
        assert len(lines_with_absent) == 0
