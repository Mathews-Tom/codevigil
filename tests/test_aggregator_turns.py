"""Aggregator integration tests for the Turn sidecar (Phase 4).

Verifies that:
- The aggregator populates _SessionContext.completed_turns as events flow in.
- Session eviction flushes the in-progress turn via finalize().
- SessionReport.turns is populated when persistence is enabled.
- Pre-existing SessionReport records without a ``turns`` key read back cleanly
  (backward-compat / schema additive-only guarantee).

Every existing aggregator test continues to pass unchanged — this suite only
adds new assertions on the new sidecar; it does not touch the collector or
lifecycle surfaces.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.analysis.store import SessionReport, SessionStore, build_report
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.turns import Turn
from codevigil.types import Collector
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _minimal_config(store_dir: Path | None = None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
        "collectors": {"enabled": []},
    }
    if store_dir is not None:
        cfg["storage"] = {"enable_persistence": True}
    return cfg


def _registry() -> dict[str, type[Collector]]:
    return {ParseHealthCollector.name: ParseHealthCollector}


def _assistant_line(text: str = "ok") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-14T10:00:05+00:00",
            "session_id": "sess-1",
            "message": {
                "content": [{"type": "text", "text": text}],
                "usage": {"output_tokens": 10},
            },
        }
    )


def _tool_call_line(tool_name: str = "Read", tool_use_id: str = "call-1") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-14T10:00:10+00:00",
            "session_id": "sess-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_name,
                        "input": {"file_path": "/tmp/f.py"},
                    }
                ],
                "usage": {"output_tokens": 5},
            },
        }
    )


# ---------------------------------------------------------------------------
# Test: completed_turns populated during ingest
# ---------------------------------------------------------------------------


def test_aggregator_accumulates_completed_turns_on_second_user_message(
    error_log: Path,
) -> None:
    """Two user messages → one completed turn in ctx.completed_turns after tick."""
    # Arrange
    clock = FakeClock(value=0.0)
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_minimal_config(),
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("first prompt")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("first reply")),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("second prompt")),
        ]
    )

    # Act
    list(aggregator.tick())

    # Assert
    ctx = aggregator.sessions["sess-1"]
    assert len(ctx.completed_turns) == 1
    turn = ctx.completed_turns[0]
    assert isinstance(turn, Turn)
    assert turn.user_message_text == "first prompt"
    assert turn.session_id == "sess-1"
    assert turn.task_type is None  # Phase 5 — not populated here


def test_aggregator_no_completed_turns_before_second_user_message(
    error_log: Path,
) -> None:
    """One user message → no completed turns yet (turn is still in progress)."""
    # Arrange
    clock = FakeClock(value=0.0)
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_minimal_config(),
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("only prompt")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply")),
        ]
    )

    # Act
    list(aggregator.tick())

    # Assert
    ctx = aggregator.sessions["sess-1"]
    assert len(ctx.completed_turns) == 0  # turn still open


def test_aggregator_tool_calls_recorded_in_completed_turns(
    error_log: Path,
) -> None:
    """Tool calls in a turn appear in Turn.tool_calls in order."""
    # Arrange
    clock = FakeClock(value=0.0)
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_minimal_config(),
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("do work")),
            # tool calls inside the assistant turn
            make_source_event(SourceEventKind.APPEND, line=_tool_call_line("Read", "c1")),
            make_source_event(SourceEventKind.APPEND, line=_tool_call_line("Edit", "c2")),
            # second user message closes the turn
            make_source_event(SourceEventKind.APPEND, line=good_user_line("next")),
        ]
    )

    # Act
    list(aggregator.tick())

    # Assert
    ctx = aggregator.sessions["sess-1"]
    assert len(ctx.completed_turns) == 1
    turn = ctx.completed_turns[0]
    assert turn.tool_calls == ("read", "edit")


# ---------------------------------------------------------------------------
# Test: eviction finalizes in-progress turn
# ---------------------------------------------------------------------------


def test_eviction_finalizes_in_progress_turn(error_log: Path) -> None:
    """Session evicted mid-turn: the open turn is flushed to completed_turns."""
    # Arrange
    clock = FakeClock(value=0.0)
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 10, "evict_after_seconds": 20},
            "collectors": {"enabled": []},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )

    # Push one user message (turn opens but never closes)
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("mid-turn")),
        ]
    )
    list(aggregator.tick())  # session is ACTIVE with one open turn

    # Advance clock past eviction threshold and evict via DELETE
    clock.advance(30.0)
    source.push([make_source_event(SourceEventKind.DELETE)])
    list(aggregator.tick())

    # After eviction the session is gone from the dict, but we need to verify
    # the turn was flushed. We do this via the store path below — here we just
    # verify the session is no longer live.
    assert "sess-1" not in aggregator.sessions


# ---------------------------------------------------------------------------
# Test: SessionReport.turns populated by persistence path
# ---------------------------------------------------------------------------


def test_session_report_turns_populated_on_eviction(error_log: Path, tmp_path: Path) -> None:
    """When persistence is enabled, evicted session report contains turns."""
    # Arrange
    store_dir = tmp_path / "sessions"
    store_dir.mkdir()
    clock = FakeClock(value=0.0)
    source = FakeSource()
    store = SessionStore(base_dir=store_dir)
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 10, "evict_after_seconds": 20},
            "collectors": {"enabled": []},
            "storage": {"enable_persistence": True},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
        bootstrap=None,
    )
    # Patch the store in the aggregator to use our temp dir
    aggregator._store = store  # type: ignore[attr-defined]

    # Feed two complete turns
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("first")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply 1")),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("second")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply 2")),
        ]
    )
    list(aggregator.tick())

    # Evict via DELETE
    source.push([make_source_event(SourceEventKind.DELETE)])
    list(aggregator.tick())

    # Assert: report was written and contains turns
    report = store.get_report("sess-1")
    assert report is not None
    assert report.turns is not None
    # Two user messages → first turn closed at second user message; second turn
    # closed at finalize. Both should appear.
    assert len(report.turns) == 2
    assert report.turns[0].user_message_text == "first"
    assert report.turns[1].user_message_text == "second"
    # task_type is None (classifier is Phase 5)
    assert all(t.task_type is None for t in report.turns)


# ---------------------------------------------------------------------------
# Test: backward compat — old records without turns key read back cleanly
# ---------------------------------------------------------------------------


def test_session_report_missing_turns_key_returns_none() -> None:
    """Pre-Phase-4 records without a 'turns' key must deserialise without error."""
    # Arrange: a minimal v1 record that does not have 'turns'
    raw: dict[str, object] = {
        "schema_version": 1,
        "session_id": "old-sess",
        "project_hash": "abc12345",
        "project_name": None,
        "model": None,
        "permission_mode": None,
        "started_at": "2026-01-01T10:00:00+00:00",
        "ended_at": "2026-01-01T10:30:00+00:00",
        "duration_seconds": 1800.0,
        "event_count": 10,
        "parse_confidence": 1.0,
        "metrics": {"parse_health": 1.0},
        "eviction_churn": 0,
        "cohort_size": 1,
        # deliberately no "turns" key
    }

    # Act
    report = SessionReport.from_dict(raw)

    # Assert
    assert report.turns is None  # safe default for pre-upgrade records
    assert report.session_id == "old-sess"


def test_build_report_with_no_turns_excludes_key() -> None:
    """build_report with turns=None does not write a 'turns' key to the dict."""
    from datetime import UTC, datetime

    report = build_report(
        session_id="s1",
        project_hash="abc12345",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        event_count=0,
        parse_confidence=1.0,
        metrics={},
        turns=None,
    )
    assert "turns" not in report.as_dict()
    assert report.turns is None


def test_build_report_with_empty_turns_round_trips() -> None:
    """An empty tuple of turns serialises and deserialises correctly."""
    from datetime import UTC, datetime

    report = build_report(
        session_id="s2",
        project_hash="abc12345",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        event_count=0,
        parse_confidence=1.0,
        metrics={},
        turns=(),
    )
    # An empty tuple is stored as an empty list, then deserialised back.
    assert report.turns == ()
    raw = report.as_dict()
    reloaded = SessionReport.from_dict(raw)
    assert reloaded.turns == ()
