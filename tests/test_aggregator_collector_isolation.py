"""SessionAggregator collector isolation: one raising collector cannot poison peers."""

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
    CountingCollector,
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)
from tests._watcher_helpers import read_error_codes


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


class _AlwaysRaises:
    name: str = "test.always_raises"
    complexity: str = "O(1)"

    def __init__(self) -> None:
        self.calls: int = 0

    def ingest(self, event: Event) -> None:
        self.calls += 1
        raise CodevigilError(
            level=ErrorLevel.ERROR,
            source=ErrorSource.COLLECTOR,
            code="test.always_raises",
            message="boom",
        )

    def snapshot(self) -> MetricSnapshot:
        return MetricSnapshot(
            name=self.name,
            value=0.0,
            label="raised",
            severity=Severity.OK,
        )

    def reset(self) -> None:
        return None


def test_raising_collector_does_not_poison_recording_collector(
    error_log: Path,
) -> None:
    clock = FakeClock(value=0.0)
    registry: dict[str, type[Collector]] = {
        ParseHealthCollector.name: ParseHealthCollector,
        _AlwaysRaises.name: _AlwaysRaises,
        CountingCollector.name: CountingCollector,
    }
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
            "collectors": {"enabled": [_AlwaysRaises.name, CountingCollector.name]},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=registry,
    )
    source.push([make_source_event(SourceEventKind.NEW_SESSION)])
    list(aggregator.tick())

    # Feed five good events.
    source.push(
        [make_source_event(SourceEventKind.APPEND, line=good_user_line(f"m-{i}")) for i in range(5)]
    )
    list(aggregator.tick())

    ctx = aggregator.sessions["sess-1"]
    counting = ctx.collectors[CountingCollector.name]
    raising = ctx.collectors[_AlwaysRaises.name]
    assert isinstance(counting, CountingCollector)
    assert isinstance(raising, _AlwaysRaises)

    # The recording collector saw every event despite the raising peer.
    assert len(counting.ingested) == 5
    # The raising collector was called for every event (errors swallowed
    # at the aggregator boundary, not at the per-event boundary).
    assert raising.calls == 5

    # Each raise produced exactly one logged error record with code test.always_raises.
    codes = read_error_codes(error_log)
    assert codes.count("test.always_raises") == 5


def test_snapshot_isolation_when_one_collector_raises_in_snapshot(
    error_log: Path,
) -> None:
    """If ``snapshot()`` raises for one collector, the others still report."""

    clock = FakeClock(value=0.0)

    class _RaisingSnapshot:
        name: str = "test.raising_snapshot"
        complexity: str = "O(1)"

        def ingest(self, event: Event) -> None:
            return None

        def snapshot(self) -> MetricSnapshot:
            raise CodevigilError(
                level=ErrorLevel.ERROR,
                source=ErrorSource.COLLECTOR,
                code="test.snapshot_boom",
                message="boom",
            )

        def reset(self) -> None:
            return None

    registry: dict[str, type[Collector]] = {
        ParseHealthCollector.name: ParseHealthCollector,
        _RaisingSnapshot.name: _RaisingSnapshot,
        CountingCollector.name: CountingCollector,
    }
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
            "collectors": {"enabled": [_RaisingSnapshot.name, CountingCollector.name]},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=registry,
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line()),
        ]
    )
    results = list(aggregator.tick())
    assert results
    _, snapshots = results[0]
    names = {s.name for s in snapshots}
    assert "test.counting" in names
    assert ParseHealthCollector.name in names
    assert "test.raising_snapshot" not in names
    assert "test.snapshot_boom" in read_error_codes(error_log)
