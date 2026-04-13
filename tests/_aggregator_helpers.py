"""Shared helpers for SessionAggregator tests.

Provides a fake :class:`Source` driven by a scripted batch list and a fake
clock so lifecycle transitions can be exercised deterministically without
sleeping or touching the wall clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from codevigil.types import Event, MetricSnapshot, Severity
from codevigil.watcher import SourceEvent, SourceEventKind


@dataclass(slots=True)
class FakeClock:
    """Mutable monotonic-clock stand-in. Tests set ``value`` directly.

    The aggregator accepts any zero-arg callable returning a float for its
    ``clock`` parameter; pass ``fake_clock.read`` (or ``fake_clock`` itself —
    instances are callable via ``__call__``).
    """

    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(slots=True)
class FakeSource:
    """Source stub whose ``poll()`` returns successive scripted batches.

    ``batches`` is a queue of lists; each ``poll()`` call pops the head. When
    empty, subsequent polls yield nothing. ``closed`` is set by ``close()``
    so tests can assert the aggregator tears the source down cleanly.
    """

    batches: list[list[SourceEvent]] = field(default_factory=list)
    closed: bool = False

    def poll(self) -> Iterator[SourceEvent]:
        if not self.batches:
            return iter(())
        head = self.batches.pop(0)
        return iter(head)

    def close(self) -> None:
        self.closed = True

    def push(self, events: list[SourceEvent]) -> None:
        self.batches.append(events)


def make_source_event(
    kind: SourceEventKind,
    *,
    session_id: str = "sess-1",
    path: Path | None = None,
    line: str | None = None,
    inode: int = 1,
    timestamp: datetime | None = None,
) -> SourceEvent:
    return SourceEvent(
        kind=kind,
        session_id=session_id,
        path=path
        if path is not None
        else Path("/home/u/.claude/projects/abc12345/sessions") / f"{session_id}.jsonl",
        inode=inode,
        line=line,
        timestamp=timestamp if timestamp is not None else datetime.now(tz=UTC),
    )


def good_user_line(text: str = "hello") -> str:
    import json

    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-04-13T12:00:00+00:00",
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def bad_line() -> str:
    return "{ this is not json"


class CountingCollector:
    """Sentinel collector that counts every lifecycle call.

    Honors the :class:`~codevigil.types.Collector` protocol via the class
    attributes ``name``/``complexity`` and the three required methods below.
    Implemented as a plain class (not a dataclass) so ``CountingCollector.name``
    resolves to the literal string ``"test.counting"`` at the class level —
    a slots dataclass would expose a member descriptor instead, which the
    aggregator's registry lookups would not key against correctly.
    """

    name: str = "test.counting"
    complexity: str = "O(1)"

    def __init__(self) -> None:
        self.ingested: list[Event] = []
        self.snapshots_taken: int = 0
        self.resets: int = 0

    def ingest(self, event: Event) -> None:
        self.ingested.append(event)

    def snapshot(self) -> MetricSnapshot:
        self.snapshots_taken += 1
        return MetricSnapshot(
            name=self.name,
            value=float(len(self.ingested)),
            label=f"{len(self.ingested)} events",
            severity=Severity.OK,
        )

    def reset(self) -> None:
        self.resets += 1
        self.ingested.clear()
