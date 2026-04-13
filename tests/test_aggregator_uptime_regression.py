"""Uptime regression: sessions older than 60 seconds render non-zero uptime.

Root cause: ``_SessionContext.first_event_time`` was initialised from
``source_event.timestamp`` — the watcher's current wall-clock time when it
discovered the file — not from the first successfully-parsed event's own
``timestamp`` field. A session file that had been running for hours would
therefore show 0s uptime on the first codevigil tick.

The fix: overwrite ``first_event_time`` in ``_ingest_line`` the first time
we parse a real event, using the event's own ``timestamp``. This test drives
the aggregator through a fake source that supplies a JSON line whose embedded
timestamp is 90 seconds in the past, then asserts that the emitted
``SessionMeta.start_time`` reflects that past timestamp, not the current
wall-clock time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import FakeClock, FakeSource, make_source_event


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _config() -> dict[str, object]:
    return {
        "watch": {
            "stale_after_seconds": 300,
            "evict_after_seconds": 2100,
            "tick_interval": 1.0,
        },
        "collectors": {"enabled": []},
    }


def _session_line(timestamp: datetime) -> str:
    """Build a valid JSONL line carrying the given timestamp."""
    return json.dumps(
        {
            "type": "user",
            "timestamp": timestamp.isoformat(),
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
    )


def test_start_time_from_parsed_event_not_watcher_clock(error_log: Path) -> None:
    """SessionMeta.start_time reflects the first parsed event's own timestamp.

    The aggregator must use the embedded ``timestamp`` from the first parsed
    event, not the watcher's wall-clock time when the file was discovered.
    We supply two events: one from 90 seconds ago (session start), and one
    from now (session's most-recent event). The delta must be ≥ 60 seconds.
    """
    clock = FakeClock(value=1000.0)
    source = FakeSource()

    # First event: 90 seconds ago (the true session start).
    session_start = datetime.now(tz=UTC) - timedelta(seconds=90)
    # Second event: "now" (the most recent activity).
    session_last = datetime.now(tz=UTC)

    watcher_now = datetime.now(tz=UTC)
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION,
                session_id="sess-1",
                timestamp=watcher_now,
            ),
            # Both APPEND events carry watcher_now as their SourceEvent
            # timestamp — the bug is that the aggregator was using this
            # watcher timestamp instead of the event's embedded timestamp.
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(session_start),
                timestamp=watcher_now,
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(session_last),
                timestamp=watcher_now,
            ),
        ]
    )

    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )

    pairs = list(aggregator.tick())
    assert len(pairs) == 1, "Expected one session in the tick output"
    meta, _ = pairs[0]

    # start_time must be session_start (90s ago), not watcher_now.
    # The rendered delta: last_event_time - start_time must be ≥ 60s.
    delta = (meta.last_event_time - meta.start_time).total_seconds()
    assert delta >= 60, (
        f"Expected uptime ≥ 60s but got {delta:.1f}s; "
        f"start_time={meta.start_time.isoformat()}, "
        f"last_event_time={meta.last_event_time.isoformat()}"
    )
    # start_time must be close to session_start, not watcher_now.
    start_delta = abs((meta.start_time - session_start).total_seconds())
    assert start_delta < 5.0, (
        f"start_time {meta.start_time.isoformat()} is not close to "
        f"session_start {session_start.isoformat()}; diff={start_delta:.1f}s"
    )


def test_start_time_does_not_change_on_subsequent_events(error_log: Path) -> None:
    """first_event_time is set from the first event and never overwritten."""
    clock = FakeClock(value=1000.0)
    source = FakeSource()

    t0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=60)

    watcher_now = datetime.now(tz=UTC)
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION, session_id="sess-1", timestamp=watcher_now
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(t0),
                timestamp=watcher_now,
            ),
        ]
    )

    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )
    list(aggregator.tick())

    # Second tick — new event with a later timestamp.
    clock.advance(1.0)
    source.push(
        [
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(t1),
                timestamp=watcher_now,
            )
        ]
    )
    pairs = list(aggregator.tick())
    assert len(pairs) == 1
    meta, _ = pairs[0]

    # start_time must still be t0, not t1.
    assert meta.start_time == t0, (
        f"start_time changed from t0 on second event: {meta.start_time.isoformat()}"
    )
    # Uptime should be t1 - t0 = 60s.
    assert meta.last_event_time == t1
    delta = (meta.last_event_time - meta.start_time).total_seconds()
    assert abs(delta - 60.0) < 1.0


def test_uptime_zero_before_any_parsed_events(error_log: Path) -> None:
    """Before any events are parsed, start_time == watcher timestamp (0s uptime)."""
    clock = FakeClock(value=1000.0)
    source = FakeSource()

    watcher_now = datetime.now(tz=UTC)
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION, session_id="sess-1", timestamp=watcher_now
            ),
        ]
    )

    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )
    pairs = list(aggregator.tick())
    # NEW_SESSION with no APPENDs: the session may or may not appear,
    # but if it does, start_time must equal the source event timestamp.
    if pairs:
        meta, _ = pairs[0]
        delta = (meta.last_event_time - meta.start_time).total_seconds()
        # No parsed events → start_time == last_event_time == watcher timestamp.
        assert abs(delta) < 2.0  # allow clock skew up to 2s


def test_uptime_regression_nonzero_when_session_60s_old(error_log: Path) -> None:
    """Integration check: session older than 60s must render non-zero uptime.

    This is the canonical regression test for the uptime bug. The watcher
    discovers a session file whose first and last event timestamps span 90
    seconds. The SessionMeta.start_time must reflect the first event's embedded
    timestamp, so the rendered uptime shows the session's true age.
    """
    import io

    from codevigil.renderers.terminal import TerminalRenderer

    clock = FakeClock(value=1000.0)
    source = FakeSource()

    session_start = datetime.now(tz=UTC) - timedelta(seconds=90)
    session_last = datetime.now(tz=UTC)
    watcher_now = datetime.now(tz=UTC)

    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION, session_id="sess-1", timestamp=watcher_now
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(session_start),
                timestamp=watcher_now,
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-1",
                line=_session_line(session_last),
                timestamp=watcher_now,
            ),
        ]
    )

    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )

    pairs = list(aggregator.tick())
    assert pairs, "Expected at least one session"

    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    for meta, snapshots in pairs:
        renderer.render(snapshots, meta)
    renderer.end_tick()

    output = stream.getvalue()
    # "0m 00s" must NOT appear — the session spans 90 seconds.
    assert "0m 00s" not in output, (
        f"Uptime shown as 0m 00s for a 90s-old session. Output: {output!r}"
    )
