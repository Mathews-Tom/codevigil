"""Delete semantics: vanished file emits DELETE and evicts the cursor."""

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


def test_unlinked_file_emits_delete_and_evicts_cursor(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"x":1}\n')

    src = PollingSource(fake_home / ".claude" / "projects")
    first = list(src.poll())
    assert any(ev.kind is SourceEventKind.NEW_SESSION for ev in first)

    f.unlink()
    second = list(src.poll())
    delete_events = [ev for ev in second if ev.kind is SourceEventKind.DELETE]
    assert len(delete_events) == 1
    assert delete_events[0].path == f
    assert delete_events[0].session_id == "session1"

    # Cursor should be gone; a third poll yields no further DELETE.
    third = list(src.poll())
    assert [ev for ev in third if ev.kind is SourceEventKind.DELETE] == []
