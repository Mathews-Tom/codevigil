"""End-to-end aggregator + bootstrap integration.

Drives a fake source through ``SessionAggregator`` with a
``BootstrapManager`` wired in, verifies severity is pinned during the
window, that the state file is persisted on completion, and that
post-bootstrap snapshots regain their real severity.
"""

from __future__ import annotations

from pathlib import Path

from codevigil.aggregator import SessionAggregator
from codevigil.bootstrap import BootstrapManager
from codevigil.types import Event, MetricSnapshot, Severity
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)


class _AlwaysCriticalCollector:
    """Collector that always reports CRITICAL so the clamp is observable."""

    name: str = "always_critical"
    complexity: str = "O(1)"

    def __init__(self) -> None:
        self._ingested: int = 0

    def ingest(self, event: Event) -> None:
        self._ingested += 1

    def snapshot(self) -> MetricSnapshot:
        return MetricSnapshot(
            name=self.name,
            value=float(self._ingested),
            label="boom",
            severity=Severity.CRITICAL,
        )

    def reset(self) -> None:
        self._ingested = 0


def _drive_one_session(
    aggregator: SessionAggregator,
    source: FakeSource,
    clock: FakeClock,
    session_id: str,
) -> list[MetricSnapshot]:
    path = Path(f"/home/u/.claude/projects/abc12345/sessions/{session_id}.jsonl")
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION,
                session_id=session_id,
                path=path,
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id=session_id,
                path=path,
                line=good_user_line("hi"),
            ),
        ]
    )
    snaps: list[MetricSnapshot] = []
    for _meta, collected in aggregator.tick():
        snaps.extend(collected)
    # Evict by advancing past the evict threshold.
    clock.advance(10_000.0)
    source.push([])
    for _ in aggregator.tick():
        pass
    return snaps


def test_aggregator_clamps_severity_during_bootstrap(tmp_path: Path) -> None:
    registry = {"always_critical": _AlwaysCriticalCollector}
    config = {
        "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
        "collectors": {"enabled": ["always_critical"]},
    }

    clock = FakeClock()
    source = FakeSource()
    mgr = BootstrapManager(
        state_path=tmp_path / "bootstrap.json",
        target_sessions=3,
        hard_caps={"always_critical.always_critical": (100.0, 0.0)},
    )
    mgr.load()
    aggregator = SessionAggregator(
        source,
        config=config,
        clock=clock,
        registry=registry,
        bootstrap=mgr,
    )

    # Sessions 1 and 2: clamp active.
    for i in range(2):
        snaps = _drive_one_session(aggregator, source, clock, f"sess-{i}")
        assert snaps, "expected at least one snapshot"
        for snap in snaps:
            assert snap.severity is Severity.OK
            assert "[bootstrap" in snap.label
        assert mgr.is_active()

    # Session 3: still active during the tick, but completes on eviction.
    snaps = _drive_one_session(aggregator, source, clock, "sess-2")
    for snap in snaps:
        assert snap.severity is Severity.OK
    assert mgr.is_active() is False
    assert (tmp_path / "bootstrap.json").exists()

    # Post-bootstrap: new session is no longer clamped.
    snaps = _drive_one_session(aggregator, source, clock, "sess-post")
    assert snaps
    for snap in snaps:
        assert snap.severity is Severity.CRITICAL
        assert "[bootstrap" not in snap.label


def test_aggregator_close_observes_live_sessions(tmp_path: Path) -> None:
    registry = {"always_critical": _AlwaysCriticalCollector}
    config = {
        "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
        "collectors": {"enabled": ["always_critical"]},
    }
    clock = FakeClock()
    source = FakeSource()
    mgr = BootstrapManager(
        state_path=tmp_path / "bootstrap.json",
        target_sessions=1,
        hard_caps={"always_critical.always_critical": (100.0, 0.0)},
    )
    mgr.load()
    aggregator = SessionAggregator(
        source,
        config=config,
        clock=clock,
        registry=registry,
        bootstrap=mgr,
    )
    path = Path("/home/u/.claude/projects/abc12345/sessions/sess-live.jsonl")
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, session_id="sess-live", path=path),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-live",
                path=path,
                line=good_user_line("hi"),
            ),
        ]
    )
    for _ in aggregator.tick():
        pass
    # Close without eviction. Bootstrap should still observe this session.
    aggregator.close()
    assert mgr.is_active() is False
    assert mgr.sessions_observed() == 1
