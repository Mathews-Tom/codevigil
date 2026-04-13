"""Schema fingerprint sampler: known shape silent, unknown shape one-time WARN."""

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


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "fingerprint.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _read_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["code"] for line in path.read_text().splitlines()]


def _known_assistant_line(idx: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": f"hello-{idx}"}]},
        }
    )


def _novel_shape_line(idx: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": f"hi-{idx}"}]},
            "novel_top_level_field": True,
            "another_unknown_key": [1, 2, 3],
        }
    )


def test_known_fingerprint_session_emits_no_warning(error_log: Path) -> None:
    lines = [_known_assistant_line(i) for i in range(15)]
    list(SessionParser(session_id="sess-1").parse(lines))
    assert "parser.unknown_fingerprint" not in _read_codes(error_log)


def test_novel_fingerprint_emits_exactly_one_warning(error_log: Path) -> None:
    lines = [_novel_shape_line(i) for i in range(25)]
    parser = SessionParser(session_id="sess-1")
    list(parser.parse(lines))

    codes = _read_codes(error_log)
    assert codes.count("parser.unknown_fingerprint") == 1
    assert parser.fingerprint_warned is True
