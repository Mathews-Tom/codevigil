"""Tests for TurnGrouper state machine and Turn dataclass.

Covers the four required scenarios from the Phase 4 plan:
1. Simple one-turn session (user → assistant → end) produces exactly one Turn.
2. Multi-turn session with N user messages produces N turns.
3. Session with tool calls inside a turn records them in the right order.
4. Session closed mid-turn via eviction finalizes the in-progress turn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codevigil.turns import Turn, TurnGrouper
from codevigil.types import Event, EventKind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DT = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)


def _dt(offset_seconds: int) -> datetime:
    return _BASE_DT + timedelta(seconds=offset_seconds)


def _user_event(
    text: str = "hello",
    *,
    offset: int = 0,
    session_id: str = "sess-x",
) -> Event:
    return Event(
        timestamp=_dt(offset),
        session_id=session_id,
        kind=EventKind.USER_MESSAGE,
        payload={"text": text},
    )


def _assistant_event(
    text: str = "ok",
    *,
    offset: int = 1,
    session_id: str = "sess-x",
) -> Event:
    return Event(
        timestamp=_dt(offset),
        session_id=session_id,
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"text": text},
    )


def _tool_call_event(
    tool_name: str,
    *,
    offset: int = 2,
    session_id: str = "sess-x",
) -> Event:
    return Event(
        timestamp=_dt(offset),
        session_id=session_id,
        kind=EventKind.TOOL_CALL,
        payload={"tool_name": tool_name, "tool_use_id": f"id-{tool_name}"},
    )


def _tool_result_event(
    tool_use_id: str = "id-read",
    *,
    offset: int = 3,
    session_id: str = "sess-x",
) -> Event:
    return Event(
        timestamp=_dt(offset),
        session_id=session_id,
        kind=EventKind.TOOL_RESULT,
        payload={"tool_use_id": tool_use_id, "is_error": False, "output": "result"},
    )


# ---------------------------------------------------------------------------
# Test 1: simple one-turn session
# ---------------------------------------------------------------------------


def test_one_turn_session_produces_exactly_one_turn() -> None:
    # Arrange
    grouper = TurnGrouper("sess-x")
    events = [
        _user_event("do something", offset=0),
        _assistant_event("ok", offset=1),
    ]

    # Act — ingest all events; no boundary yet since no second user message
    emitted: list[Turn] = []
    for e in events:
        t = grouper.ingest(e)
        if t is not None:
            emitted.append(t)
    final = grouper.finalize()

    # Assert
    assert len(emitted) == 0  # no mid-session boundaries
    assert final is not None
    assert final.user_message_text == "do something"
    assert final.event_count == 2
    assert final.session_id == "sess-x"


# ---------------------------------------------------------------------------
# Test 2: multi-turn session with N user messages produces N turns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_turns", [2, 3, 5])
def test_multi_turn_session_produces_n_turns(n_turns: int) -> None:
    # Arrange
    grouper = TurnGrouper("sess-y")
    all_events: list[Event] = []
    for i in range(n_turns):
        all_events.append(_user_event(f"turn {i}", offset=i * 10))
        all_events.append(_assistant_event(f"reply {i}", offset=i * 10 + 1))

    # Act
    emitted: list[Turn] = []
    for e in all_events:
        t = grouper.ingest(e)
        if t is not None:
            emitted.append(t)
    final = grouper.finalize()

    # Assert: N-1 turns emitted mid-session, plus the final one on finalize
    assert len(emitted) == n_turns - 1
    if final is not None:
        emitted.append(final)
    assert len(emitted) == n_turns
    for i, turn in enumerate(emitted):
        assert turn.user_message_text == f"turn {i}"


# ---------------------------------------------------------------------------
# Test 3: tool calls recorded in order
# ---------------------------------------------------------------------------


def test_tool_calls_recorded_in_order() -> None:
    # Arrange
    grouper = TurnGrouper("sess-z")
    events = [
        _user_event("implement X", offset=0),
        _tool_call_event("read", offset=1),
        _tool_result_event("id-read", offset=2),
        _tool_call_event("edit", offset=3),
        _tool_result_event("id-edit", offset=4),
        _tool_call_event("bash", offset=5),
        _tool_result_event("id-bash", offset=6),
        _assistant_event("done", offset=7),
    ]

    # Act
    for e in events:
        grouper.ingest(e)
    turn = grouper.finalize()

    # Assert
    assert turn is not None
    assert turn.tool_calls == ("read", "edit", "bash")
    assert turn.event_count == len(events)


def test_tool_calls_are_empty_when_no_tools_used() -> None:
    # Arrange
    grouper = TurnGrouper("sess-notool")

    # Act
    grouper.ingest(_user_event("just talk", offset=0))
    grouper.ingest(_assistant_event("sure", offset=1))
    turn = grouper.finalize()

    # Assert
    assert turn is not None
    assert turn.tool_calls == ()


def test_tool_calls_preserve_duplicates() -> None:
    """Same tool used twice in a turn → both appearances recorded."""
    # Arrange
    grouper = TurnGrouper("sess-dup")
    events = [
        _user_event("read two files", offset=0),
        _tool_call_event("read", offset=1),
        _tool_result_event("id-read", offset=2),
        _tool_call_event("read", offset=3),
        _tool_result_event("id-read-2", offset=4),
    ]

    # Act
    for e in events:
        grouper.ingest(e)
    turn = grouper.finalize()

    # Assert
    assert turn is not None
    assert turn.tool_calls == ("read", "read")


# ---------------------------------------------------------------------------
# Test 4: eviction finalizes in-progress turn
# ---------------------------------------------------------------------------


def test_eviction_mid_turn_finalizes_in_progress_turn() -> None:
    # Arrange
    grouper = TurnGrouper("sess-evict")
    events = [
        _user_event("start something", offset=0),
        _tool_call_event("bash", offset=1),
        # session ends here — no closing assistant message, no second user message
    ]

    # Act
    emitted: list[Turn] = []
    for e in events:
        t = grouper.ingest(e)
        if t is not None:
            emitted.append(t)
    final = grouper.finalize()

    # Assert
    assert len(emitted) == 0  # no boundary crossed mid-ingest
    assert final is not None
    assert final.user_message_text == "start something"
    assert final.tool_calls == ("bash",)
    assert final.event_count == 2


def test_finalize_returns_none_when_no_turn_in_progress() -> None:
    # Arrange
    grouper = TurnGrouper("sess-empty")

    # Act — finalize with no events ingested
    result = grouper.finalize()

    # Assert
    assert result is None


def test_finalize_after_boundary_closes_last_turn() -> None:
    """Boundary emits turn N, finalize emits turn N+1."""
    # Arrange
    grouper = TurnGrouper("sess-2t")
    events = [
        _user_event("first", offset=0),
        _assistant_event("first reply", offset=1),
        _user_event("second", offset=2),
        # session ends without a reply to "second"
    ]

    # Act
    emitted: list[Turn] = []
    for e in events:
        t = grouper.ingest(e)
        if t is not None:
            emitted.append(t)
    final = grouper.finalize()

    # Assert
    assert len(emitted) == 1
    assert emitted[0].user_message_text == "first"
    assert final is not None
    assert final.user_message_text == "second"


# ---------------------------------------------------------------------------
# Turn dataclass properties
# ---------------------------------------------------------------------------


def test_turn_is_frozen() -> None:
    """Turn must be frozen — attribute assignment must raise."""
    turn = Turn(
        session_id="s",
        started_at=_dt(0),
        ended_at=_dt(1),
        user_message_text="hi",
        tool_calls=(),
        event_count=1,
    )
    with pytest.raises((AttributeError, TypeError)):
        turn.task_type = "exploration"  # type: ignore[misc]


def test_turn_task_type_defaults_to_none() -> None:
    turn = Turn(
        session_id="s",
        started_at=_dt(0),
        ended_at=_dt(1),
        user_message_text="hi",
        tool_calls=(),
        event_count=1,
    )
    assert turn.task_type is None


def test_turn_timestamps_match_events() -> None:
    """started_at matches user event timestamp; ended_at matches last event."""
    # Arrange
    grouper = TurnGrouper("sess-ts")
    user_ts = _dt(0)
    last_ts = _dt(5)
    events = [
        Event(
            timestamp=user_ts,
            session_id="sess-ts",
            kind=EventKind.USER_MESSAGE,
            payload={"text": "go"},
        ),
        Event(
            timestamp=_dt(3),
            session_id="sess-ts",
            kind=EventKind.ASSISTANT_MESSAGE,
            payload={"text": "ok"},
        ),
        Event(
            timestamp=last_ts,
            session_id="sess-ts",
            kind=EventKind.TOOL_CALL,
            payload={"tool_name": "read", "tool_use_id": "x"},
        ),
    ]

    # Act
    for e in events:
        grouper.ingest(e)
    turn = grouper.finalize()

    # Assert
    assert turn is not None
    assert turn.started_at == user_ts
    assert turn.ended_at == last_ts


def test_non_user_events_before_first_user_message_are_ignored() -> None:
    """Events arriving before any USER_MESSAGE do not start a turn."""
    # Arrange
    grouper = TurnGrouper("sess-pre")
    events = [
        Event(
            timestamp=_dt(0),
            session_id="sess-pre",
            kind=EventKind.SYSTEM,
            payload={"subkind": "session_start"},
        ),
        Event(
            timestamp=_dt(1),
            session_id="sess-pre",
            kind=EventKind.ASSISTANT_MESSAGE,
            payload={"text": "init"},
        ),
    ]

    # Act
    emitted = [grouper.ingest(e) for e in events]
    final = grouper.finalize()

    # Assert
    assert all(t is None for t in emitted)
    assert final is None
