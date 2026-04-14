"""Shared fixture builders for renderer tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from codevigil.types import MetricSnapshot, SessionMeta, SessionState, Severity


def make_meta(
    *,
    session_id: str = "a3f7c2d0abcdef01",
    project_name: str | None = "iree-loom",
    state: SessionState = SessionState.ACTIVE,
    duration_s: float = 154.0,
    parse_confidence: float = 1.0,
    session_task_type: str | None = None,
) -> SessionMeta:
    start = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
    return SessionMeta(
        session_id=session_id,
        project_hash="deadbeefcafef00d",
        project_name=project_name,
        file_path=Path("/tmp/nonexistent.jsonl"),
        start_time=start,
        last_event_time=start + timedelta(seconds=duration_s),
        event_count=42,
        parse_confidence=parse_confidence,
        state=state,
        session_task_type=session_task_type,
    )


def make_snapshots() -> list[MetricSnapshot]:
    return [
        MetricSnapshot(
            name="read_edit_ratio",
            value=5.2,
            label="R:E 5.2 | research:mut 7.1",
            severity=Severity.OK,
        ),
        MetricSnapshot(
            name="stop_phrase",
            value=0.0,
            label="0 hits",
            severity=Severity.OK,
        ),
        MetricSnapshot(
            name="reasoning_loop",
            value=6.4,
            label="6.4/1K tool calls | burst: 2",
            severity=Severity.WARN,
        ),
    ]
