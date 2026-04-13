"""Malformed lines log via the error channel and never crash the parser."""

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
from codevigil.parser import SessionParser
from codevigil.types import EventKind


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "parser.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _read_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["code"] for line in path.read_text().splitlines()]


def _good_user_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def test_three_malformed_variants_logged_and_skipped(error_log: Path) -> None:
    lines = [
        _good_user_line("first"),
        "{not json at all",  # invalid JSON
        _good_user_line("middle"),
        json.dumps({"timestamp": "2026-04-13T12:00:00+00:00"}),  # missing 'type'
        _good_user_line("after-missing-type"),
        json.dumps(
            {
                "type": "telepathy",
                "timestamp": "2026-04-13T12:00:00+00:00",
            }
        ),  # unknown 'type'
        _good_user_line("last"),
    ]
    parser = SessionParser(session_id="sess-1")
    events = list(parser.parse(lines))

    assert [e.kind for e in events] == [EventKind.USER_MESSAGE] * 4
    assert [e.payload["text"] for e in events] == [
        "first",
        "middle",
        "after-missing-type",
        "last",
    ]

    codes = _read_codes(error_log)
    assert "parser.malformed_line" in codes
    assert "parser.missing_type" in codes
    assert "parser.unknown_type" in codes
