"""Large-file growth: oversize delta WARNs once and still processes the data."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource, SourceEventKind
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


def test_oversize_growth_warns_once_and_processes(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    f = sessions / "session1.jsonl"

    # Write a file whose initial size already exceeds the 1 KiB threshold.
    payload = b'{"k":"' + (b"x" * 2000) + b'"}\n'
    f.write_bytes(payload)

    src = PollingSource(
        fake_home / ".claude" / "projects",
        large_file_warn_bytes=1024,
    )
    events = list(src.poll())
    appended = [ev for ev in events if ev.kind is SourceEventKind.APPEND]
    assert len(appended) == 1
    assert appended[0].line is not None
    assert appended[0].line.startswith('{"k":"')

    codes = read_error_codes(fake_home / "errors.log")
    assert codes.count("watcher.large_file_growth") == 1

    # Adding another big chunk to the same file does not trigger a second
    # warn for that file.
    with f.open("ab") as h:
        h.write(b'{"k2":"' + (b"y" * 2000) + b'"}\n')
    list(src.poll())
    codes = read_error_codes(fake_home / "errors.log")
    assert codes.count("watcher.large_file_growth") == 1
