"""Tests for write_precision in the read_edit_ratio collector.

write_precision = write_calls / (write_calls + edit_calls)

Covers:
- None when no mutation sub-category calls observed.
- 1.0 when only write calls observed.
- 0.0 when only edit calls observed.
- 0.5 when equal write and edit calls observed.
- Reset clears write_precision state.
- Appears in the snapshot detail dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codevigil.collectors.read_edit_ratio import ReadEditRatioCollector
from codevigil.types import Event, EventKind


def _event(tool_name: str, file_path: str | None = None) -> Event:
    payload: dict[str, object] = {"tool_name": tool_name}
    if file_path is not None:
        payload["file_path"] = file_path
    return Event(
        timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        session_id="test",
        kind=EventKind.TOOL_CALL,
        payload=payload,
    )


class TestWritePrecision:
    def test_none_when_no_mutations(self) -> None:
        c = ReadEditRatioCollector()
        # Only reads — no mutations at all.
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("read", "/g.py"))
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_precision"] is None

    def test_one_when_only_write_calls(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        c.ingest(_event("write", "/g.py"))
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_precision"] == 1.0

    def test_zero_when_only_edit_calls(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("edit", "/f.py"))
        c.ingest(_event("multi_edit", "/f.py"))
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_precision"] == 0.0

    def test_half_when_equal_write_and_edit(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        c.ingest(_event("edit", "/f.py"))
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_precision"] == pytest.approx(0.5, abs=1e-6)

    def test_notebook_edit_counts_as_edit(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/nb.py"))
        c.ingest(_event("write", "/nb.py"))
        c.ingest(_event("notebook_edit", "/nb.py"))
        snap = c.snapshot()
        # 1 write, 1 notebook_edit => precision = 0.5
        assert snap.detail is not None
        assert snap.detail["write_precision"] == pytest.approx(0.5, abs=1e-6)

    def test_write_calls_and_edit_calls_in_detail(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        c.ingest(_event("edit", "/f.py"))
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_calls"] == 2
        assert snap.detail["edit_calls"] == 1

    def test_reset_clears_write_precision(self) -> None:
        c = ReadEditRatioCollector()
        c.ingest(_event("read", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        c.reset()
        snap = c.snapshot()
        assert snap.detail is not None
        assert snap.detail["write_precision"] is None
        assert snap.detail["write_calls"] == 0
        assert snap.detail["edit_calls"] == 0

    def test_accumulates_across_window_evictions(self) -> None:
        """write_precision is session-cumulative, not window-bounded."""
        c = ReadEditRatioCollector({"window_size": 3, **_minimal_config()})
        # Drive the window past capacity with reads.
        for _ in range(5):
            c.ingest(_event("read", "/f.py"))
        c.ingest(_event("write", "/f.py"))
        snap = c.snapshot()
        # write_precision should be 1.0 (one write, zero edits).
        assert snap.detail is not None
        assert snap.detail["write_precision"] == 1.0


def _minimal_config() -> dict[str, object]:
    """Return a minimal collector config dict."""
    return {
        "window_size": 3,
        "warn_threshold": 4.0,
        "critical_threshold": 2.0,
        "blind_edit_window": 10,
        "blind_edit_confidence_floor": 0.95,
        "min_events_for_severity": 5,
        "experimental": False,
    }
