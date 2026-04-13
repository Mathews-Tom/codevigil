"""Shared JSONL fixture builders for CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj)


def write_fixture_session(
    path: Path,
    *,
    session_id: str = "sess-cli-1",
    timestamp: str = "2026-04-13T12:00:00+00:00",
    include_stop_phrase: bool = False,
) -> Path:
    """Write a deterministic 5-event JSONL session file and return ``path``.

    The fixture is stable across runs: same timestamps, same ids, same text.
    Tests can diff or hash the produced report output against a golden.
    """

    lines: list[str] = [
        _line(
            {
                "type": "system",
                "timestamp": timestamp,
                "session_id": session_id,
                "subtype": "session_start",
                "cwd": "/home/user/proj",
            }
        ),
        _line(
            {
                "type": "user",
                "timestamp": "2026-04-13T12:00:01+00:00",
                "session_id": session_id,
                "message": {"content": [{"type": "text", "text": "please check the file"}]},
            }
        ),
        _line(
            {
                "type": "assistant",
                "timestamp": "2026-04-13T12:00:02+00:00",
                "session_id": session_id,
                "message": {
                    "content": [
                        {"type": "text", "text": _assistant_text(include_stop_phrase)},
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/foo.py"},
                        },
                    ],
                    "usage": {"output_tokens": 17},
                },
            }
        ),
        _line(
            {
                "type": "user",
                "timestamp": "2026-04-13T12:00:03+00:00",
                "session_id": session_id,
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
                "type": "assistant",
                "timestamp": "2026-04-13T12:00:04+00:00",
                "session_id": session_id,
                "message": {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"output_tokens": 3},
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _assistant_text(include_stop_phrase: bool) -> str:
    if include_stop_phrase:
        # "should I continue" is in the default stop_phrase table (see
        # codevigil/collectors/stop_phrase.py); matching it triggers a hit.
        return "reviewed. should I continue?"
    return "reviewed the file"


__all__ = ["write_fixture_session"]
