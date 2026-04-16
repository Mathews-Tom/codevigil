"""Shared CLI progress helpers."""

from __future__ import annotations

import io
from typing import cast

import pytest
from rich.console import Console

from codevigil.ui.progress import NullProgressReporter, RichProgressReporter, should_enable_progress


def test_should_enable_progress_requires_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    assert should_enable_progress(total_items=100) is False


def test_rich_progress_reporter_emits_phase_and_message() -> None:
    stream = io.StringIO()
    console = Console(file=cast(io.TextIOWrapper, stream), force_terminal=False)
    reporter = RichProgressReporter(console=console, enabled=True)
    reporter.start(
        phase="ingesting",
        total=2,
        message="sessions",
        unit="files",
        target="root",
    )
    reporter.advance(message="first", bytes_delta=128, target="agent-1.jsonl")
    reporter.set_message("persisting")
    reporter.finish(message="done")
    reporter.__exit__(None, None, None)
    output = stream.getvalue()
    assert "done" in output
    assert "128 B" in output
    assert "agent-1.jsonl" in output
    assert "/s" in output


def test_null_progress_reporter_is_noop() -> None:
    reporter = NullProgressReporter()
    reporter.start(phase="loading", total=None, message="x", unit="sessions", target="root")
    reporter.set_total(10)
    reporter.advance(message="y", bytes_delta=1, target="file.jsonl")
    reporter.set_message("z")
    reporter.update(phase="rendering", message="z", target="target", total=1)
    reporter.finish(message="done")
