"""SessionAggregator error routing across the four ErrorLevel cases."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    CodevigilError,
    ErrorChannel,
    ErrorLevel,
    ErrorSource,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import Collector, Event, MetricSnapshot, Severity
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    FakeClock,
    FakeSource,
    bad_line,
    good_user_line,
    make_source_event,
)
from tests._watcher_helpers import read_error_records


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


class _RaisingCollector:
    """Collector whose ingest always raises a known CodevigilError."""

    name: str = "test.raising"
    complexity: str = "O(1)"

    def ingest(self, event: Event) -> None:
        raise CodevigilError(
            level=ErrorLevel.ERROR,
            source=ErrorSource.COLLECTOR,
            code="test.boom",
            message="collector blew up",
            context={"detail": "scripted"},
        )

    def snapshot(self) -> MetricSnapshot:
        return MetricSnapshot(
            name=self.name,
            value=0.0,
            label="never",
            severity=Severity.OK,
        )

    def reset(self) -> None:
        return None


def _build_aggregator(
    *,
    registry: dict[str, type[Collector]],
    enabled: list[str],
    clock: FakeClock,
) -> tuple[SessionAggregator, FakeSource]:
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {
                "stale_after_seconds": 300,
                "evict_after_seconds": 2100,
            },
            "collectors": {"enabled": enabled},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=registry,
    )
    source.push([make_source_event(SourceEventKind.NEW_SESSION)])
    list(aggregator.tick())
    return aggregator, source


def test_parser_warn_routes_with_source_parser(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    registry: dict[str, type[Collector]] = {ParseHealthCollector.name: ParseHealthCollector}
    aggregator, source = _build_aggregator(registry=registry, enabled=[], clock=clock)

    source.push([make_source_event(SourceEventKind.APPEND, line=bad_line())])
    list(aggregator.tick())

    records = read_error_records(error_log)
    parser_records = [r for r in records if r["source"] == "parser"]
    assert parser_records, "expected at least one parser-sourced error record"
    assert any(r["code"] == "parser.malformed_line" for r in parser_records)


def test_collector_error_caught_and_logged_with_source_collector(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    registry: dict[str, type[Collector]] = {
        ParseHealthCollector.name: ParseHealthCollector,
        _RaisingCollector.name: _RaisingCollector,
    }
    aggregator, source = _build_aggregator(
        registry=registry,
        enabled=[_RaisingCollector.name],
        clock=clock,
    )

    source.push([make_source_event(SourceEventKind.APPEND, line=good_user_line())])
    # Must not propagate.
    list(aggregator.tick())

    records = read_error_records(error_log)
    boom = [r for r in records if r["code"] == "test.boom"]
    assert boom, "raising collector error must be logged"
    assert all(r["source"] == "collector" for r in boom)
    assert boom[0]["context"]["collector"] == _RaisingCollector.name
    assert boom[0]["context"]["session_id"] == "sess-1"


def test_critical_parse_health_surfaces_via_snapshot(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    registry: dict[str, type[Collector]] = {ParseHealthCollector.name: ParseHealthCollector}
    aggregator, source = _build_aggregator(registry=registry, enabled=[], clock=clock)

    # Drive the parser well past the 50-line window with > 10% bad lines so
    # parse_confidence drops below 0.9 → CRITICAL severity in the snapshot.
    batch = []
    for index in range(60):
        line = bad_line() if index % 4 == 0 else good_user_line(f"msg-{index}")
        batch.append(make_source_event(SourceEventKind.APPEND, line=line))
    source.push(batch)

    results = list(aggregator.tick())
    assert results, "aggregator must yield at least one (meta, snapshots) pair"
    _, snapshots = results[0]
    parse_health_snapshot = next(s for s in snapshots if s.name == ParseHealthCollector.name)
    assert parse_health_snapshot.severity is Severity.CRITICAL
    assert parse_health_snapshot.value < 0.9
