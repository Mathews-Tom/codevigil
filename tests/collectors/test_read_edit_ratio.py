"""Behavioural tests for ReadEditRatioCollector."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.read_edit_ratio import ReadEditRatioCollector
from codevigil.types import Event, EventKind, Severity


def _tool_call(tool: str, file_path: str | None = None) -> Event:
    payload: dict[str, Any] = {"tool_name": tool, "tool_use_id": "x", "input": {}}
    if file_path is not None:
        payload["file_path"] = file_path
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s1",
        kind=EventKind.TOOL_CALL,
        payload=payload,
    )


def _make(**overrides: Any) -> ReadEditRatioCollector:
    cfg: dict[str, Any] = {
        "window_size": 50,
        "warn_threshold": 4.0,
        "critical_threshold": 2.0,
        "blind_edit_window": 20,
        "blind_edit_confidence_floor": 0.95,
        "min_events_for_severity": 10,
        "experimental": True,
    }
    cfg.update(overrides)
    return ReadEditRatioCollector(cfg)


def test_happy_ratio_above_warn_reports_ok() -> None:
    c = _make()
    for _ in range(8):
        c.ingest(_tool_call("read", "a.py"))
    c.ingest(_tool_call("edit", "a.py"))
    c.ingest(_tool_call("grep", "a.py"))
    snap = c.snapshot()
    assert snap.severity is Severity.OK
    assert snap.value == 8.0
    assert snap.detail is not None
    assert snap.detail["mutations"] == 1
    assert snap.detail["reads"] == 8


def test_warm_up_state_clamps_to_ok() -> None:
    c = _make()
    c.ingest(_tool_call("edit", "a.py"))
    snap = c.snapshot()
    assert snap.severity is Severity.OK
    assert snap.label == "warming up"


def test_warn_severity_in_band() -> None:
    c = _make()
    # 6 reads + 2 mutations = ratio 3.0, in [2.0, 4.0) WARN band.
    for _ in range(6):
        c.ingest(_tool_call("read", "a.py"))
    for _ in range(2):
        c.ingest(_tool_call("edit", "a.py"))
    for _ in range(2):
        c.ingest(_tool_call("grep", "a.py"))
    snap = c.snapshot()
    assert snap.severity is Severity.WARN
    assert snap.value == 3.0


def test_critical_below_floor() -> None:
    c = _make()
    for _ in range(2):
        c.ingest(_tool_call("read", "a.py"))
    for _ in range(8):
        c.ingest(_tool_call("edit", "a.py"))
    snap = c.snapshot()
    assert snap.severity is Severity.CRITICAL


def test_blind_edit_detected_when_mutation_unread() -> None:
    c = _make()
    # 9 reads of a.py, then edit b.py without reading it.
    for _ in range(9):
        c.ingest(_tool_call("read", "a.py"))
    c.ingest(_tool_call("edit", "b.py"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["blind_edit_rate"]["value"] == 1.0
    assert snap.detail["blind_edit_rate"]["tracking_confidence"] == 1.0


def test_blind_edit_zero_when_file_was_read() -> None:
    c = _make()
    for _ in range(9):
        c.ingest(_tool_call("read", "a.py"))
    c.ingest(_tool_call("edit", "a.py"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["blind_edit_rate"]["value"] == 0.0


def test_low_tracking_confidence_marks_insufficient_data() -> None:
    c = _make()
    for _ in range(9):
        c.ingest(_tool_call("read", "a.py"))
    # 10 mutations, none with file_path -> tracking_confidence = 0.0.
    for _ in range(10):
        c.ingest(_tool_call("edit"))
    snap = c.snapshot()
    assert snap.detail is not None
    blind = snap.detail["blind_edit_rate"]
    assert blind["tracking_confidence"] == 0.0
    assert blind["label"] == "insufficient data"


def test_reset_clears_state() -> None:
    c = _make()
    for _ in range(5):
        c.ingest(_tool_call("read", "a.py"))
    c.ingest(_tool_call("edit", "a.py"))
    c.reset()
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["reads"] == 0
    assert snap.detail["mutations"] == 0


def test_window_eviction_drops_old_entries() -> None:
    c = _make(window_size=4)
    for _ in range(3):
        c.ingest(_tool_call("read", "a.py"))
    c.ingest(_tool_call("edit", "a.py"))
    # Now push four edits, evicting the reads.
    for _ in range(4):
        c.ingest(_tool_call("edit", "a.py"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["reads"] == 0
    assert snap.detail["mutations"] == 4


def test_throughput_micro_benchmark_under_500ms() -> None:
    c = _make()
    events = [_tool_call("read", "a.py") for _ in range(2000)]
    start = time.perf_counter()
    for ev in events:
        c.ingest(ev)
    c.snapshot()
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"ingest of 2000 events took {elapsed:.3f}s"


def test_ingest_swallows_payload_errors() -> None:
    c = _make()
    bad = Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s1",
        kind=EventKind.TOOL_CALL,
        payload={"tool_name": 12345},  # type: ignore[dict-item]
    )
    c.ingest(bad)  # must not raise
    snap = c.snapshot()
    assert snap.severity is Severity.OK
