"""SessionAggregator smoke: source → parser → collectors → snapshots wiring."""

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


def test_aggregator_pipes_source_events_to_parser_and_collectors(
    error_log: Path,
) -> None:
    clock = FakeClock(value=0.0)
    source = FakeSource()
    registry: dict[str, type[Collector]] = {ParseHealthCollector.name: ParseHealthCollector}
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
            "collectors": {"enabled": []},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=registry,
    )
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("hi")),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("there")),
        ]
    )

    results = list(aggregator.tick())

    assert len(results) == 1
    meta, snapshots = results[0]
    assert meta.session_id == "sess-1"
    assert meta.state is SessionState.ACTIVE
    assert meta.event_count == 2
    # The hash extracted from the canonical fake path is "abc12345".
    assert meta.project_hash == "abc12345"
    assert meta.project_name == "abc12345"
    # parse_health is always-on even though it is not in enabled.
    snapshot_names = {s.name for s in snapshots}
    assert ParseHealthCollector.name in snapshot_names
