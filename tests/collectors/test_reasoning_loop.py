"""Behavioural tests for ReasoningLoopCollector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.reasoning_loop import ReasoningLoopCollector
from codevigil.types import Event, EventKind, Severity


def _msg(text: str) -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"text": text},
    )


def _tool() -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.TOOL_CALL,
        payload={"tool_name": "read", "tool_use_id": "t", "input": {}},
    )


def _make(**overrides: Any) -> ReasoningLoopCollector:
    cfg: dict[str, Any] = {
        "warn_threshold": 10.0,
        "critical_threshold": 20.0,
        "experimental": True,
    }
    cfg.update(overrides)
    return ReasoningLoopCollector(cfg)


def test_clean_messages_report_ok() -> None:
    c = _make()
    for _ in range(100):
        c.ingest(_tool())
    c.ingest(_msg("Refactored cleanly. Tests pass."))
    snap = c.snapshot()
    assert snap.severity is Severity.OK
    assert snap.value == 0.0


def test_loop_rate_per_1k_tool_calls() -> None:
    c = _make()
    for _ in range(100):
        c.ingest(_tool())
    # 1 match across 100 tool calls -> rate 10.0/1000.
    c.ingest(_msg("Actually, let me reconsider that approach."))
    snap = c.snapshot()
    # Two matches: "actually" and "let me reconsider".
    assert snap.detail is not None
    assert snap.detail["matches"] == 2
    assert snap.value == 20.0
    assert snap.severity is Severity.CRITICAL


def test_warn_band() -> None:
    c = _make()
    for _ in range(100):
        c.ingest(_tool())
    c.ingest(_msg("Actually, that works."))  # 1 match -> 10.0
    snap = c.snapshot()
    assert snap.severity is Severity.WARN


def test_max_burst_tracks_consecutive_hit_messages() -> None:
    c = _make()
    c.ingest(_msg("Actually one."))
    c.ingest(_msg("Actually two."))
    c.ingest(_msg("Actually three."))
    c.ingest(_msg("clean message"))
    c.ingest(_msg("Actually four."))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["max_burst"] == 3


def test_word_boundary_does_not_match_factually() -> None:
    c = _make()
    c.ingest(_msg("This is factually correct."))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["matches"] == 0


def test_correction_substring_mode_matches_inside_word_join() -> None:
    c = _make()
    c.ingest(_msg("Correction: I was wrong about that earlier."))
    snap = c.snapshot()
    assert snap.detail is not None
    # "correction:" (substring mode) and "i was wrong" (word mode) both match.
    assert snap.detail["matches"] >= 2


def test_reset_clears_state() -> None:
    c = _make()
    c.ingest(_msg("Actually that's wrong."))
    c.ingest(_tool())
    c.reset()
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["matches"] == 0
    assert snap.detail["tool_calls"] == 0
    assert snap.detail["max_burst"] == 0


def test_ignores_user_messages() -> None:
    c = _make()
    c.ingest(
        Event(
            timestamp=datetime.now(tz=UTC),
            session_id="s",
            kind=EventKind.USER_MESSAGE,
            payload={"text": "actually you should rewrite it"},
        )
    )
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["matches"] == 0
