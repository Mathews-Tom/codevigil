"""Behavioural tests for ThinkingCollector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.thinking import ThinkingCollector
from codevigil.types import Event, EventKind, Severity


def _thinking(text: str = "", *, redacted: bool = False, signature: str | None = None) -> Event:
    payload: dict[str, Any] = {
        "length": len(text),
        "redacted": redacted,
        "text": text,
    }
    if signature is not None:
        payload["signature"] = signature
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.THINKING,
        payload=payload,
    )


def _make() -> ThinkingCollector:
    return ThinkingCollector({"experimental": True})


def test_no_thinking_blocks_reports_zero_ratio() -> None:
    snap = _make().snapshot()
    assert snap.value == 0.0
    assert snap.severity is Severity.OK
    assert snap.detail is not None
    assert snap.detail["thinking_blocks"] == 0
    assert snap.detail["visible_chars_median"] is None


def test_visible_blocks_count_and_median() -> None:
    c = _make()
    c.ingest(_thinking("a" * 100))
    c.ingest(_thinking("a" * 200))
    c.ingest(_thinking("a" * 300))
    snap = c.snapshot()
    assert snap.value == 1.0
    assert snap.detail is not None
    assert snap.detail["visible_blocks"] == 3
    assert snap.detail["visible_chars_median"] == 200.0


def test_redacted_blocks_excluded_from_median() -> None:
    c = _make()
    c.ingest(_thinking("a" * 50))
    c.ingest(_thinking(redacted=True, signature="sig-payload"))
    c.ingest(_thinking(redacted=True, signature="another-signature"))
    snap = c.snapshot()
    # 1 visible / 3 total
    assert abs(snap.value - (1 / 3)) < 1e-9
    assert snap.detail is not None
    assert snap.detail["thinking_blocks"] == 3
    assert snap.detail["visible_blocks"] == 1
    assert snap.detail["redacted_blocks"] == 2
    assert snap.detail["visible_chars_median"] == 50.0
    # Two signatures observed.
    assert snap.detail["signature_chars_median"] is not None


def test_non_thinking_events_ignored() -> None:
    c = _make()
    c.ingest(
        Event(
            timestamp=datetime.now(tz=UTC),
            session_id="s",
            kind=EventKind.TOOL_CALL,
            payload={"tool_name": "read", "tool_use_id": "t", "input": {}},
        )
    )
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["thinking_blocks"] == 0


def test_reset_clears_state() -> None:
    c = _make()
    c.ingest(_thinking("hello"))
    c.reset()
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["thinking_blocks"] == 0


def test_serialize_and_restore_round_trip() -> None:
    c = _make()
    c.ingest(_thinking("a" * 80, signature="sig"))
    c.ingest(_thinking(redacted=True, signature="sig2"))
    state = c.serialize_state()

    other = _make()
    other.restore_state(state)
    snap = other.snapshot()
    assert snap.detail is not None
    assert snap.detail["thinking_blocks"] == 2
    assert snap.detail["visible_blocks"] == 1
    assert snap.detail["visible_chars_median"] == 80.0
