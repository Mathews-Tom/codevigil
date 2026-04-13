"""Partial-line semantics: an unterminated trailing fragment is never emitted."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource, SourceEventKind
from tests._watcher_helpers import install_error_log, reset_error_log, session_dir


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_error_log(tmp_path / "errors.log")
    yield tmp_path
    reset_error_log()


def test_trailing_fragment_without_newline_is_not_emitted(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"complete":1}\n{"partial":')

    src = PollingSource(fake_home / ".claude" / "projects")
    events = list(src.poll())
    appended = [ev.line for ev in events if ev.kind is SourceEventKind.APPEND]
    assert appended == ['{"complete":1}']

    # Polling again with no new bytes still emits nothing.
    assert [ev for ev in src.poll() if ev.kind is SourceEventKind.APPEND] == []

    # Finish the line and confirm it lands intact.
    with f.open("ab") as h:
        h.write(b"2}\n")
    final = [ev for ev in src.poll() if ev.kind is SourceEventKind.APPEND]
    assert [ev.line for ev in final] == ['{"partial":2}']
