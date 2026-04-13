"""Standalone truncate semantics: same inode, size shrinks to non-zero."""

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


def test_truncate_resets_cursor_and_rereads(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"line1":1}\n{"line2":2}\n{"line3":3}\n')

    src = PollingSource(fake_home / ".claude" / "projects")
    first = list(src.poll())
    appended_first = [ev.line for ev in first if ev.kind is SourceEventKind.APPEND]
    assert appended_first == ['{"line1":1}', '{"line2":2}', '{"line3":3}']
    inode_before = f.stat().st_ino

    # Truncate to a smaller size with new content. Same inode preserved.
    with f.open("wb") as h:
        h.write(b'{"only":1}\n')
    assert f.stat().st_ino == inode_before

    second = list(src.poll())
    kinds = [ev.kind for ev in second]
    assert SourceEventKind.TRUNCATE in kinds
    appended_second = [ev.line for ev in second if ev.kind is SourceEventKind.APPEND]
    assert appended_second == ['{"only":1}']
