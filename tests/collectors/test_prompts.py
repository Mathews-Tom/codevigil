"""Tests for PromptsCollector."""

from __future__ import annotations

from datetime import UTC, datetime

from codevigil.collectors.prompts import PromptsCollector
from codevigil.types import Event, EventKind


def _user(text: str = "hi") -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.USER_MESSAGE,
        payload={"text": text},
    )


def _tool() -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.TOOL_CALL,
        payload={"tool_name": "read", "tool_use_id": "t", "input": {}},
    )


def test_zero_when_no_user_turns() -> None:
    c = PromptsCollector({"experimental": True})
    snap = c.snapshot()
    assert snap.value == 0.0
    assert snap.detail == {"user_turns": 0, "experimental": True}


def test_counts_user_messages_only() -> None:
    c = PromptsCollector({"experimental": True})
    c.ingest(_user("a"))
    c.ingest(_tool())
    c.ingest(_user("b"))
    c.ingest(_user("c"))
    snap = c.snapshot()
    assert snap.value == 3.0
    assert snap.detail is not None
    assert snap.detail["user_turns"] == 3


def test_serialize_round_trip() -> None:
    c = PromptsCollector({"experimental": True})
    for _ in range(7):
        c.ingest(_user())
    state = c.serialize_state()
    other = PromptsCollector({"experimental": True})
    other.restore_state(state)
    assert other.snapshot().value == 7.0
