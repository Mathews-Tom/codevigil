"""Append semantics: partial lines, multi-line appends, pending-byte carryover."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource, SourceEvent, SourceEventKind
from tests._watcher_helpers import (
    install_error_log,
    read_error_codes,
    reset_error_log,
    session_dir,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_error_log(tmp_path / "errors.log")
    yield tmp_path
    reset_error_log()


def _appends(events: list[SourceEvent]) -> list[str]:
    return [ev.line for ev in events if ev.kind is SourceEventKind.APPEND and ev.line is not None]


def test_partial_line_held_until_newline_arrives(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"half": ')

    src = PollingSource(fake_home / ".claude" / "projects")
    events_first = list(src.poll())
    assert _appends(events_first) == []
    kinds = [ev.kind for ev in events_first]
    assert SourceEventKind.NEW_SESSION in kinds

    with f.open("ab") as h:
        h.write(b'"yes"}\n')
    events_second = list(src.poll())
    assert _appends(events_second) == ['{"half": "yes"}']


def test_multi_line_append_emits_one_event_per_line(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"a":1}\n{"b":2}\n')

    src = PollingSource(fake_home / ".claude" / "projects")
    events_first = list(src.poll())
    assert _appends(events_first) == ['{"a":1}', '{"b":2}']

    with f.open("ab") as h:
        h.write(b'{"c":3}\n{"d":4}\n')
    events_second = list(src.poll())
    assert _appends(events_second) == ['{"c":3}', '{"d":4}']


def test_pending_carries_over_three_polls(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"x":')

    src = PollingSource(fake_home / ".claude" / "projects")
    assert _appends(list(src.poll())) == []

    with f.open("ab") as h:
        h.write(b' "still ')
    assert _appends(list(src.poll())) == []

    with f.open("ab") as h:
        h.write(b'pending"}\n')
    assert _appends(list(src.poll())) == ['{"x": "still pending"}']

    # No spurious errors.
    assert read_error_codes(fake_home / "errors.log") == []
