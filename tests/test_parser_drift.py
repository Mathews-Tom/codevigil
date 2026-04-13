"""Drift detection: ParseHealthCollector flips CRITICAL below 0.9 confidence."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.parser import SessionParser
from codevigil.types import Severity


@pytest.fixture(autouse=True)
def _isolate_error_channel(tmp_path: Path) -> Iterator[None]:
    path = tmp_path / "drift.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield
    reset_error_channel()


def _good_line(idx: int) -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": f"msg-{idx}"}]},
        }
    )


def _bad_line() -> str:
    return "{ broken json"


def test_collector_stays_ok_above_threshold() -> None:
    # 50 lines, 5 broken (90% confidence — boundary, not below).
    lines = [_good_line(i) if i % 10 != 0 else _bad_line() for i in range(50)]
    parser = SessionParser(session_id="sess-1")
    collector = ParseHealthCollector(stats=parser.stats)
    for event in parser.parse(lines):
        collector.ingest(event)

    snapshot = collector.snapshot()
    assert snapshot.severity is Severity.OK
    assert snapshot.value >= 0.9


def test_collector_flips_critical_below_threshold() -> None:
    # 50 lines, 15 broken → confidence 0.7, well under 0.9.
    lines = [_bad_line() if i < 15 else _good_line(i) for i in range(50)]
    parser = SessionParser(session_id="sess-1")
    collector = ParseHealthCollector(stats=parser.stats)
    for event in parser.parse(lines):
        collector.ingest(event)

    snapshot = collector.snapshot()
    assert snapshot.severity is Severity.CRITICAL
    assert snapshot.value < 0.9
    assert snapshot.label == "schema drift detected"
    assert snapshot.detail is not None
    assert "missing_fields" in snapshot.detail
    assert snapshot.detail["missing_fields"]


def test_collector_idle_until_window_full() -> None:
    # Even a 0% parse rate must not flap CRITICAL until the window is full.
    parser = SessionParser(session_id="sess-1")
    collector = ParseHealthCollector(stats=parser.stats)
    for _ in range(10):
        list(parser.parse([_bad_line()]))
    snapshot = collector.snapshot()
    assert snapshot.severity is Severity.OK
