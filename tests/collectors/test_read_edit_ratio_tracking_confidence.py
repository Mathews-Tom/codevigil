"""Low blind-edit tracking confidence degrades gracefully."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.read_edit_ratio import ReadEditRatioCollector
from codevigil.types import Event, EventKind


def _tool_call(tool: str, file_path: str | None = None) -> Event:
    payload: dict[str, Any] = {"tool_name": tool, "tool_use_id": "x", "input": {}}
    if file_path is not None:
        payload["file_path"] = file_path
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.TOOL_CALL,
        payload=payload,
    )


def test_low_tracking_confidence_emits_insufficient_data_label() -> None:
    c = ReadEditRatioCollector()
    for _ in range(9):
        c.ingest(_tool_call("read", "a.py"))
    # Mix mutations: only 1 of 10 has file_path -> tracking 0.1 < 0.95 floor.
    c.ingest(_tool_call("edit", "a.py"))
    for _ in range(9):
        c.ingest(_tool_call("edit"))
    snap = c.snapshot()
    assert snap.detail is not None
    blind = snap.detail["blind_edit_rate"]
    assert blind["tracking_confidence"] < 0.95
    assert blind["label"] == "insufficient data"


def test_full_tracking_confidence_no_label_degradation() -> None:
    c = ReadEditRatioCollector()
    for _ in range(9):
        c.ingest(_tool_call("read", "a.py"))
    for _ in range(2):
        c.ingest(_tool_call("edit", "a.py"))
    snap = c.snapshot()
    assert snap.detail is not None
    blind = snap.detail["blind_edit_rate"]
    assert blind["tracking_confidence"] == 1.0
    assert "label" not in blind
