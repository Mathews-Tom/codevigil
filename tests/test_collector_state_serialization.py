"""Collector state serialize/restore round-trip (Phase C5).

Each persistent collector must expose ``serialize_state()`` returning a
JSON-serialisable dict, and ``restore_state(dict)`` that round-trips
the serialised payload back into an equivalent in-memory state. Round
trips are tested by snapshotting the restored collector and comparing
to the source.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.collectors.read_edit_ratio import ReadEditRatioCollector
from codevigil.collectors.reasoning_loop import ReasoningLoopCollector
from codevigil.collectors.stop_phrase import StopPhraseCollector
from codevigil.config import CONFIG_DEFAULTS
from codevigil.parser import ParseStats
from codevigil.types import Event, EventKind
from codevigil.watch_roots import legacy_session_key


def _tool_call(tool_name: str, file_path: str | None = None) -> Event:
    payload: dict[str, Any] = {"tool_name": tool_name}
    if file_path is not None:
        payload["file_path"] = file_path
    return Event(
        timestamp=datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC),
        session_id="agent-test",
        kind=EventKind.TOOL_CALL,
        payload=payload,
    )


def _assistant_message(text: str) -> Event:
    return Event(
        timestamp=datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC),
        session_id="agent-test",
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"text": text},
    )


def _assert_json_serialisable(state: dict[str, Any]) -> None:
    json.dumps(state)


def test_parse_health_round_trip() -> None:
    stats = ParseStats()
    stats.total_lines = 100
    stats.parsed_events = 95
    stats.duplicate_count = 3
    stats.missing_fields = {"timestamp": 2, "session_id": 1}

    src = ParseHealthCollector(stats=stats)
    state = src.serialize_state()
    _assert_json_serialisable(state)

    dst = ParseHealthCollector()
    dst.restore_state(state)

    assert dst.stats.total_lines == 100
    assert dst.stats.parsed_events == 95
    assert dst.stats.duplicate_count == 3
    assert dst.stats.missing_fields == {"timestamp": 2, "session_id": 1}


def test_read_edit_ratio_round_trip_preserves_rolling_window() -> None:
    cfg = dict(CONFIG_DEFAULTS["collectors"]["read_edit_ratio"])
    src = ReadEditRatioCollector(config=cfg)
    for tool in ["read", "read", "grep", "write", "edit", "read"]:
        src.ingest(_tool_call(tool, file_path="/tmp/a.py"))

    src_snapshot = src.snapshot()
    state = src.serialize_state()
    _assert_json_serialisable(state)

    dst = ReadEditRatioCollector(config=cfg)
    dst.restore_state(state)
    dst_snapshot = dst.snapshot()

    assert dst_snapshot.value == src_snapshot.value
    assert dst_snapshot.severity == src_snapshot.severity
    # Subsequent ingest on the restored collector must continue
    # accumulating from the persisted state, not from zero.
    dst.ingest(_tool_call("edit", file_path="/tmp/a.py"))
    src.ingest(_tool_call("edit", file_path="/tmp/a.py"))
    assert dst.snapshot().value == src.snapshot().value


def test_stop_phrase_round_trip_preserves_hit_counts() -> None:
    cfg = dict(CONFIG_DEFAULTS["collectors"]["stop_phrase"])
    src = StopPhraseCollector(config=cfg)
    for text in [
        "this is going well",
        "I cannot continue without more information",
        "plain text with no triggers",
    ]:
        src.ingest(_assistant_message(text))

    state = src.serialize_state()
    _assert_json_serialisable(state)

    dst = StopPhraseCollector(config=cfg)
    dst.restore_state(state)

    assert dst.snapshot().value == src.snapshot().value


def test_reasoning_loop_round_trip_preserves_burst() -> None:
    cfg = dict(CONFIG_DEFAULTS["collectors"]["reasoning_loop"])
    src = ReasoningLoopCollector(config=cfg)
    for tool in ["read", "read", "grep", "grep", "read"]:
        src.ingest(_tool_call(tool))

    state = src.serialize_state()
    _assert_json_serialisable(state)

    dst = ReasoningLoopCollector(config=cfg)
    dst.restore_state(state)

    # Metric value is defined by tool_calls and matches; both must match.
    assert dst.snapshot().value == src.snapshot().value
    assert dst.snapshot().severity == src.snapshot().severity


def test_restore_state_tolerates_empty_dict() -> None:
    """Restoring from an empty dict must not crash — it's treated as
    a no-op that leaves the collector in its fresh construction state."""
    ParseHealthCollector().restore_state({})
    ReadEditRatioCollector(
        config=dict(CONFIG_DEFAULTS["collectors"]["read_edit_ratio"])
    ).restore_state({})
    StopPhraseCollector(config=dict(CONFIG_DEFAULTS["collectors"]["stop_phrase"])).restore_state({})
    ReasoningLoopCollector(
        config=dict(CONFIG_DEFAULTS["collectors"]["reasoning_loop"])
    ).restore_state({})


def test_aggregator_absorbs_garbage_collector_state() -> None:
    """Aggregator's ``_maybe_restore_collector_state`` must tolerate a
    collector that raises on a malformed state slice — the collector
    is left in its fresh default and ingestion continues."""
    from codevigil.aggregator import SessionAggregator
    from codevigil.projects import ProjectRegistry
    from codevigil.watcher import SourceEvent, SourceEventKind

    def provider(session_key: str) -> dict[str, dict[str, Any]] | None:
        return {"parse_health": {"total_lines": "not an int"}}

    aggregator = SessionAggregator(
        source=_StubSource(),
        config={
            "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
            "collectors": {"enabled": []},
        },
        project_registry=ProjectRegistry(),
        collector_state_provider=provider,
    )
    event = SourceEvent(
        kind=SourceEventKind.NEW_SESSION,
        session_id="agent-test",
        path=__file__ and __import__("pathlib").Path(__file__),  # type: ignore[misc]
        inode=0,
        line=None,
        timestamp=datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC),
    )
    aggregator._dispatch_source_event(event)
    ctx = aggregator.sessions.get(legacy_session_key("agent-test"))
    assert ctx is not None
    # parse_health is always instantiated; its _stats must be fresh.
    ph = ctx.collectors["parse_health"]
    assert ph.stats.total_lines == 0  # type: ignore[attr-defined]


class _StubSource:
    def poll(self) -> Any:
        return iter(())

    def close(self) -> None:
        return None
