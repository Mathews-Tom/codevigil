"""Happy-path round-trip: assistant + tool_use + thinking + tool_result + system."""

from __future__ import annotations

import json

from codevigil.parser import SessionParser
from codevigil.types import EventKind


def _line(obj: dict[str, object]) -> str:
    return json.dumps(obj)


def test_assistant_user_system_round_trip_emits_typed_events() -> None:
    lines = [
        _line(
            {
                "type": "assistant",
                "timestamp": "2026-04-13T12:00:00+00:00",
                "session_id": "sess-1",
                "message": {
                    "content": [
                        {"type": "text", "text": "let me look"},
                        {"type": "thinking", "thinking": "plan steps", "signature": "sig-x"},
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/foo.py"},
                        },
                    ],
                    "usage": {"output_tokens": 42},
                },
            }
        ),
        _line(
            {
                "type": "user",
                "timestamp": "2026-04-13T12:00:01+00:00",
                "session_id": "sess-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "is_error": False,
                            "content": "file contents",
                        }
                    ]
                },
            }
        ),
        _line(
            {
                "type": "system",
                "timestamp": "2026-04-13T12:00:02+00:00",
                "session_id": "sess-1",
                "subtype": "session_start",
            }
        ),
    ]
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse(lines))

    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.ASSISTANT_MESSAGE,
        EventKind.THINKING,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.SYSTEM,
    ]

    assistant = events[0]
    assert assistant.payload["text"] == "let me look"
    assert assistant.payload["token_count"] == 42

    thinking = events[1]
    assert thinking.payload["length"] == len("plan steps")
    assert thinking.payload["redacted"] is False
    assert thinking.payload["signature"] == "sig-x"
    assert thinking.payload["text"] == "plan steps"

    call = events[2]
    assert call.payload["tool_name"] == "read"
    assert call.payload["tool_use_id"] == "call-1"
    assert call.payload["input"] == {"file_path": "/tmp/foo.py"}
    assert call.payload["file_path"] == "/tmp/foo.py"

    result = events[3]
    assert result.payload["tool_use_id"] == "call-1"
    assert result.payload["is_error"] is False
    assert result.payload["output"] == "file contents"

    system = events[4]
    assert system.payload["subkind"] == "session_start"

    assert parser.stats.parse_confidence == 1.0


def test_redacted_thinking_block_emits_zero_length() -> None:
    line = _line(
        {
            "type": "assistant",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "[redacted]"},
                ]
            },
        }
    )
    events = list(SessionParser(session_id="sess-1").parse([line]))
    assert len(events) == 1
    assert events[0].kind is EventKind.THINKING
    assert events[0].payload["redacted"] is True
    assert events[0].payload["length"] == 0
