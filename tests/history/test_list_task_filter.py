"""Tests for ``--task-type`` filter in ``history list``.

``--task-type <name>`` restricts output to sessions whose
``session_task_type`` matches the given label exactly. Sessions with
``session_task_type = None`` never match a non-None filter.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.history.list_cmd import run_list


def _write_session(
    store: SessionStore,
    session_id: str,
    *,
    session_task_type: str | None = None,
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
        "metrics": {},
    }
    defaults.update(kwargs)
    report = build_report(
        session_id=session_id,
        session_task_type=session_task_type,
        **defaults,  # type: ignore[arg-type]
    )
    store.write(report)


class TestTaskTypeFilter:
    def test_filter_returns_only_matching_sessions(self, tmp_path: Path) -> None:
        """--task-type debug_loop returns only sessions with that task type."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-debug1", session_task_type="debug_loop")
        _write_session(store, "agent-explore1", session_task_type="exploration")
        _write_session(store, "agent-notask1", session_task_type=None)
        out = io.StringIO()
        run_list(store_dir=tmp_path, task_type="debug_loop", out=out)
        text = out.getvalue()
        assert "debug1" in text
        assert "explore1" not in text
        assert "notask1" not in text

    def test_filter_excludes_none_task_type_sessions(self, tmp_path: Path) -> None:
        """Sessions with session_task_type=None are excluded by any non-None filter."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-null1", session_task_type=None)
        _write_session(store, "agent-mut1", session_task_type="mutation_heavy")
        out = io.StringIO()
        run_list(store_dir=tmp_path, task_type="mutation_heavy", out=out)
        text = out.getvalue()
        assert "mut1" in text
        assert "null1" not in text

    def test_filter_returns_empty_when_no_match(self, tmp_path: Path) -> None:
        """Empty result when no session matches the task type filter."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-explore2", session_task_type="exploration")
        out = io.StringIO()
        run_list(store_dir=tmp_path, task_type="planning", out=out)
        text = out.getvalue()
        assert "explore2" not in text

    def test_no_filter_returns_all_sessions(self, tmp_path: Path) -> None:
        """task_type=None (no filter) returns all sessions regardless of task type."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-all1", session_task_type="debug_loop")
        _write_session(store, "agent-all2", session_task_type="exploration")
        _write_session(store, "agent-all3", session_task_type=None)
        out = io.StringIO()
        run_list(store_dir=tmp_path, task_type=None, out=out)
        text = out.getvalue()
        assert "all1" in text
        assert "all2" in text
        assert "all3" in text

    def test_filter_multiple_matching_sessions(self, tmp_path: Path) -> None:
        """Multiple sessions with the same task type are all returned."""
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-plan1", session_task_type="planning")
        _write_session(store, "agent-plan2", session_task_type="planning")
        _write_session(store, "agent-debug2", session_task_type="debug_loop")
        out = io.StringIO()
        run_list(store_dir=tmp_path, task_type="planning", out=out)
        text = out.getvalue()
        assert "plan1" in text
        assert "plan2" in text
        assert "debug2" not in text
