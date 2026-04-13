"""Rotation semantics: rename-based and copy-truncate variants."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource, SourceEvent, SourceEventKind
from tests._watcher_helpers import install_error_log, reset_error_log, session_dir


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_error_log(tmp_path / "errors.log")
    yield tmp_path
    reset_error_log()


def _kinds(events: list[SourceEvent]) -> list[SourceEventKind]:
    return [ev.kind for ev in events]


def _appends(events: list[SourceEvent]) -> list[str]:
    return [ev.line for ev in events if ev.kind is SourceEventKind.APPEND and ev.line is not None]


def test_rename_based_rotation_emits_rotate_and_resets_cursor(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"original":1}\n')

    src = PollingSource(fake_home / ".claude" / "projects")
    first = list(src.poll())
    assert _appends(first) == ['{"original":1}']
    old_inode = f.stat().st_ino

    # Rotate: move current file to .1, write a brand-new file at the same
    # path. The new file gets a different inode.
    f.rename(sessions / "session1.jsonl.1")
    f.write_bytes(b'{"after_rotate":1}\n')
    assert f.stat().st_ino != old_inode

    second = list(src.poll())
    kinds = _kinds(second)
    assert SourceEventKind.ROTATE in kinds
    rotate_index = kinds.index(SourceEventKind.ROTATE)
    # After ROTATE the new content is read as APPEND.
    assert SourceEventKind.APPEND in kinds[rotate_index:]
    assert '{"after_rotate":1}' in _appends(second)


def test_copy_truncate_rotation_emits_truncate(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"
    f.write_bytes(b'{"a":1}\n{"b":2}\n')

    src = PollingSource(fake_home / ".claude" / "projects")
    first = list(src.poll())
    assert _appends(first) == ['{"a":1}', '{"b":2}']
    inode_before = f.stat().st_ino

    # Copy-truncate: same inode, file rewritten with smaller contents.
    with f.open("wb") as h:
        h.write(b'{"fresh":1}\n')
    assert f.stat().st_ino == inode_before

    second = list(src.poll())
    kinds = _kinds(second)
    # The design says copy-truncate (size < old size, same inode) is a
    # TRUNCATE transition.
    assert SourceEventKind.TRUNCATE in kinds
    assert '{"fresh":1}' in _appends(second)
