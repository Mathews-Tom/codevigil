"""Multi-root source fan-in semantics."""

from __future__ import annotations

from codevigil.watcher import MultiSource, SourceEventKind
from tests._aggregator_helpers import FakeSource, make_source_event


def test_multi_source_yields_events_from_all_sources() -> None:
    first = FakeSource(
        batches=[[make_source_event(SourceEventKind.NEW_SESSION, session_id="a", inode=1)]]
    )
    second = FakeSource(
        batches=[[make_source_event(SourceEventKind.NEW_SESSION, session_id="b", inode=2)]]
    )

    source = MultiSource([first, second])
    events = list(source.poll())

    assert [event.session_id for event in events] == ["a", "b"]


def test_multi_source_closes_all_children() -> None:
    first = FakeSource()
    second = FakeSource()

    source = MultiSource([first, second])
    source.close()

    assert first.closed is True
    assert second.closed is True
