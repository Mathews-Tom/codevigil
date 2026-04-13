"""Bounded walk: oversized session trees emit exactly one WARN."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource
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


def test_overflow_emits_one_warn_and_caps_cursors(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    # 25 files, max_files=20 → 5 over the cap.
    for i in range(25):
        (sessions / f"session{i:03d}.jsonl").write_bytes(b'{"x":1}\n')

    src = PollingSource(fake_home / ".claude" / "projects", max_files=20)
    list(src.poll())
    list(src.poll())

    codes = read_error_codes(fake_home / "errors.log")
    assert codes.count("watcher.bounded_walk_overflow") == 1

    # After the first poll exactly max_files cursors exist.
    assert len(src._cursors) == 20
