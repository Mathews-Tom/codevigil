"""TOOL_ALIASES table coverage and one-INFO-per-session unknown-tool dedup."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.parser import TOOL_ALIASES, SessionParser, canonicalise_tool_name
from codevigil.types import EventKind


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "tool_aliases.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize(("raw", "canonical"), sorted(TOOL_ALIASES.items()))
def test_canonicalise_table_entries(raw: str, canonical: str) -> None:
    assert canonicalise_tool_name(raw) == canonical


def _tool_call_line(name: str, call_id: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {},
                    }
                ]
            },
        }
    )


def test_unknown_tool_logs_exactly_once_per_session(error_log: Path) -> None:
    lines = [_tool_call_line("MysteryTool", f"call-{i}") for i in range(5)]
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse(lines))

    assert len(events) == 5
    assert all(e.kind is EventKind.TOOL_CALL for e in events)
    assert all(e.payload["tool_name"] == "MysteryTool" for e in events)

    records = _read_records(error_log)
    unknown = [r for r in records if r["code"] == "parser.unknown_tool"]
    assert len(unknown) == 1
    assert unknown[0]["level"] == "info"
    assert unknown[0]["context"]["tool_name"] == "MysteryTool"
