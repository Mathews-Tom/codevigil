"""Message-ID deduplication tests for SessionParser.

Tests four scenarios:
  (a) single file with intra-file duplicate messages
  (b) two files fed sequentially to the same parser with overlapping IDs
  (c) events whose message.id is None are never deduplicated
  (d) two different session parser instances do not share dedup state
"""

from __future__ import annotations

import json
from pathlib import Path

from codevigil.parser import SessionParser
from codevigil.types import EventKind

_FIXTURES = Path(__file__).parent / "fixtures" / "duplicate_messages"


def _jsonl(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


# ---------------------------------------------------------------------------
# (a) Intra-file duplicates: 20 JSONL lines, 5 repeated message IDs
# ---------------------------------------------------------------------------


def test_intra_file_duplicates_emits_deduplicated_events() -> None:
    """intra_file_duplicates.jsonl has 5 duplicate IDs; parser emits 15 events."""
    parser = SessionParser(session_id="fixture-dedup-intra")
    events = list(parser.parse(_jsonl(_FIXTURES / "intra_file_duplicates.jsonl")))
    assert len(events) == 15
    assert parser.stats.duplicate_count == 5


def test_intra_file_duplicates_duplicate_count_matches_suppressed_events() -> None:
    """duplicate_count precisely counts how many events were suppressed."""
    parser = SessionParser(session_id="fixture-dedup-intra")
    events = list(parser.parse(_jsonl(_FIXTURES / "intra_file_duplicates.jsonl")))
    # 20 lines total; 5 duplicate lines suppressed; 15 unique events emitted.
    # duplicate_count must equal exactly the number of suppressed lines.
    assert parser.stats.duplicate_count == 5
    assert len(events) == 15


# ---------------------------------------------------------------------------
# (b) Cross-file: two files fed sequentially to the same parser instance
# ---------------------------------------------------------------------------


def test_cross_file_sequential_feeds_emit_union_without_overlap() -> None:
    """Feeding cross_file_a then cross_file_b to the same parser deduplicates overlap."""
    parser = SessionParser(session_id="fixture-dedup-cross")
    lines_a = _jsonl(_FIXTURES / "cross_file_a.jsonl")
    lines_b = _jsonl(_FIXTURES / "cross_file_b.jsonl")
    events = list(parser.parse(lines_a + lines_b))
    assert parser.stats.duplicate_count == 6
    # 18 from a + 12 from b - 6 duplicates = 24 unique events
    assert len(events) == 24


def test_cross_file_no_event_appears_twice() -> None:
    """Every emitted event has a unique (message_id, kind, timestamp) triple."""
    parser = SessionParser(session_id="fixture-dedup-cross")
    lines_a = _jsonl(_FIXTURES / "cross_file_a.jsonl")
    lines_b = _jsonl(_FIXTURES / "cross_file_b.jsonl")
    events = list(parser.parse(lines_a + lines_b))
    # Build a set of (message_id, kind, timestamp) for non-None-id events.
    # None-id events (system events) are excluded from the uniqueness check.
    ids_seen: set[tuple[str | None, EventKind, str]] = set()
    for ev in events:
        if ev.message_id is not None:
            key = (ev.message_id, ev.kind, ev.timestamp.isoformat())
            assert key not in ids_seen, f"duplicate event in output: {key}"
            ids_seen.add(key)


# ---------------------------------------------------------------------------
# (c) Events with message.id is None are NEVER deduplicated
# ---------------------------------------------------------------------------


def _make_line(obj: dict[str, object]) -> str:
    return json.dumps(obj) + "\n"


def test_none_id_events_are_never_deduplicated() -> None:
    """Events with no message.id field must always be emitted regardless of repetition."""
    # Two identical user messages with NO id field — both must be emitted.
    user_line_no_id = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-none-id",
            "message": {
                # Deliberately omit "id" — older session format.
                "role": "user",
                "content": "hello again",
            },
        }
    )
    lines = [user_line_no_id, user_line_no_id, user_line_no_id]
    parser = SessionParser(session_id="sess-none-id")
    events = list(parser.parse(lines))
    # All three must be emitted; none are deduplicated.
    assert len(events) == 3, (
        f"Expected 3 events for None-id messages but got {len(events)}; "
        "None-id events must never be deduplicated"
    )
    assert parser.stats.duplicate_count == 0


def test_none_id_events_have_message_id_none() -> None:
    """Events built from messages without an id field carry message_id=None."""
    user_line_no_id = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-none-id",
            "message": {
                "role": "user",
                "content": "hello",
            },
        }
    )
    parser = SessionParser(session_id="sess-none-id")
    events = list(parser.parse([user_line_no_id]))
    assert len(events) == 1
    assert events[0].message_id is None


def test_none_id_events_not_deduplicated_even_after_string_id_events() -> None:
    """None-id events pass through even when a string-id event was seen before them."""
    system_line = _make_line(
        {
            "type": "system",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-mixed",
            "subtype": "session_start",
        }
    )
    user_with_id = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:01+00:00",
            "session_id": "sess-mixed",
            "message": {"id": "msg-x", "role": "user", "content": "hello"},
        }
    )
    user_no_id = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:02+00:00",
            "session_id": "sess-mixed",
            "message": {"role": "user", "content": "world"},
        }
    )
    parser = SessionParser(session_id="sess-mixed")
    events = list(parser.parse([system_line, user_with_id, user_no_id, user_no_id]))
    # system + user_with_id + user_no_id * 2 = 4 events
    assert len(events) == 4, (
        f"Expected 4 events but got {len(events)}; None-id events must always be emitted"
    )
    assert parser.stats.duplicate_count == 0


# ---------------------------------------------------------------------------
# (d) Different parser instances have independent dedup sets
# ---------------------------------------------------------------------------


def test_two_parser_instances_have_independent_dedup_sets() -> None:
    """Duplicate message IDs across two parser instances are NOT deduplicated."""
    shared_id_line = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-a",
            "message": {"id": "shared-msg-id", "role": "user", "content": "hello"},
        }
    )
    parser_a = SessionParser(session_id="sess-a")
    parser_b = SessionParser(session_id="sess-b")

    events_a = list(parser_a.parse([shared_id_line]))
    events_b = list(parser_b.parse([shared_id_line]))

    # Both parsers are independent — each must emit the event.
    assert len(events_a) == 1, "parser_a should emit the event"
    assert len(events_b) == 1, "parser_b should emit the event independently"
    assert parser_a.stats.duplicate_count == 0
    assert parser_b.stats.duplicate_count == 0


def test_same_parser_instance_deduplicates_same_id_repeated() -> None:
    """Within a single parser instance, repeated message IDs are suppressed."""
    user_line = _make_line(
        {
            "type": "user",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-a",
            "message": {"id": "msg-dup", "role": "user", "content": "hello"},
        }
    )
    parser = SessionParser(session_id="sess-a")
    events = list(parser.parse([user_line, user_line, user_line]))
    assert len(events) == 1
    assert parser.stats.duplicate_count == 2


# ---------------------------------------------------------------------------
# Event.message_id propagation checks
# ---------------------------------------------------------------------------


def test_assistant_events_carry_message_id() -> None:
    """Events from assistant messages carry the message's id."""
    line = _make_line(
        {
            "type": "assistant",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-1",
            "message": {
                "id": "msg-asst-1",
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            },
        }
    )
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse([line]))
    assert len(events) == 1
    assert events[0].message_id == "msg-asst-1"


def test_all_blocks_in_same_message_share_message_id() -> None:
    """All content blocks within one assistant message share the same message_id."""
    line = _make_line(
        {
            "type": "assistant",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-1",
            "message": {
                "id": "msg-multi",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "starting"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x.py"},
                    },
                ],
            },
        }
    )
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse([line]))
    assert len(events) == 2
    assert all(ev.message_id == "msg-multi" for ev in events)


def test_system_events_have_message_id_none() -> None:
    """System events always have message_id=None (they have no message object)."""
    line = _make_line(
        {
            "type": "system",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": "sess-1",
            "subtype": "session_start",
        }
    )
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse([line]))
    assert len(events) == 1
    assert events[0].message_id is None


def test_clean_fixture_has_zero_duplicates() -> None:
    """clean.jsonl contains no duplicate IDs; duplicate_count must be 0."""
    parser = SessionParser(session_id="fixture-dedup-clean")
    events = list(parser.parse(_jsonl(_FIXTURES / "clean.jsonl")))
    assert parser.stats.duplicate_count == 0
    assert len(events) == 22
