"""SessionAggregator lifecycle: ACTIVE → STALE → EVICTED with a fake clock."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import Collector, SessionState
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    CountingCollector,
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _registry_with_counting() -> dict[str, type[Collector]]:
    return {
        ParseHealthCollector.name: ParseHealthCollector,
        CountingCollector.name: CountingCollector,
    }


def _config() -> dict[str, object]:
    return {
        "watch": {
            "stale_after_seconds": 300,
            "evict_after_seconds": 2100,
            "tick_interval": 1.0,
        },
        "collectors": {"enabled": [CountingCollector.name]},
    }


def _aggregator_with_session(
    *, clock: FakeClock, error_log: Path
) -> tuple[SessionAggregator, FakeSource]:
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(toml_path=error_log.parent / "absent.toml"),
        clock=clock,
        registry=_registry_with_counting(),
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("init")),
        ]
    )
    list(aggregator.tick())
    return aggregator, source


def test_session_starts_active(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, _ = _aggregator_with_session(clock=clock, error_log=error_log)

    ctx = aggregator.sessions["sess-1"]
    assert ctx.state is SessionState.ACTIVE
    counting = ctx.collectors[CountingCollector.name]
    assert isinstance(counting, CountingCollector)
    assert len(counting.ingested) == 1
    assert counting.resets == 0


def test_active_to_stale_after_stale_threshold(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, _ = _aggregator_with_session(clock=clock, error_log=error_log)

    clock.value = 305.0
    list(aggregator.tick())

    ctx = aggregator.sessions["sess-1"]
    assert ctx.state is SessionState.STALE
    counting = ctx.collectors[CountingCollector.name]
    assert isinstance(counting, CountingCollector)
    # STALE must NOT call reset — collector state is preserved.
    assert counting.resets == 0
    assert len(counting.ingested) == 1


def test_stale_to_active_on_new_append_preserves_state(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, source = _aggregator_with_session(clock=clock, error_log=error_log)

    clock.value = 400.0
    list(aggregator.tick())  # → STALE
    ctx = aggregator.sessions["sess-1"]
    assert ctx.state is SessionState.STALE

    source.push([make_source_event(SourceEventKind.APPEND, line=good_user_line("back"))])
    list(aggregator.tick())

    counting = ctx.collectors[CountingCollector.name]
    assert isinstance(counting, CountingCollector)
    assert ctx.state is SessionState.ACTIVE
    # Coffee break rule: collector state preserved across STALE→ACTIVE.
    assert counting.resets == 0
    assert len(counting.ingested) == 2


def test_eviction_calls_reset_exactly_once_and_drops_session(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, _ = _aggregator_with_session(clock=clock, error_log=error_log)

    ctx = aggregator.sessions["sess-1"]
    counting = ctx.collectors[CountingCollector.name]
    assert isinstance(counting, CountingCollector)

    clock.value = 2200.0  # > evict_after (2100)
    list(aggregator.tick())

    assert "sess-1" not in aggregator.sessions
    assert counting.resets == 1


def test_delete_source_event_evicts_immediately(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, source = _aggregator_with_session(clock=clock, error_log=error_log)
    counting_before = aggregator.sessions["sess-1"].collectors[CountingCollector.name]
    assert isinstance(counting_before, CountingCollector)

    source.push([make_source_event(SourceEventKind.DELETE)])
    list(aggregator.tick())

    assert "sess-1" not in aggregator.sessions
    assert counting_before.resets == 1


def test_close_resets_all_collectors_and_closes_source(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    aggregator, source = _aggregator_with_session(clock=clock, error_log=error_log)
    counting = aggregator.sessions["sess-1"].collectors[CountingCollector.name]
    assert isinstance(counting, CountingCollector)

    aggregator.close()

    assert source.closed is True
    assert counting.resets == 1
    assert aggregator.sessions == {}


def test_parse_health_always_instantiated_even_when_not_in_enabled(
    error_log: Path,
) -> None:
    clock = FakeClock(value=0.0)
    aggregator, _ = _aggregator_with_session(clock=clock, error_log=error_log)

    ctx = aggregator.sessions["sess-1"]
    assert ParseHealthCollector.name in ctx.collectors
    parse_health = ctx.collectors[ParseHealthCollector.name]
    assert isinstance(parse_health, ParseHealthCollector)
    # Bound to the parser's stats handle, so live counters flow through.
    assert parse_health.stats is ctx.parser.stats
