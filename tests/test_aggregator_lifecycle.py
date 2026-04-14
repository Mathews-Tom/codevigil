"""SessionAggregator lifecycle: ACTIVE → STALE → EVICTED with a fake clock."""

from __future__ import annotations

import json
import time
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


# ---------------------------------------------------------------------------
# Back-dating invariant tests
# ---------------------------------------------------------------------------


def _old_event_line(age_seconds: float) -> str:
    """Return a valid JSONL user event whose timestamp is age_seconds in the past."""
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return json.dumps(
        {
            "type": "user",
            "timestamp": ts,
            "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": "old content"}]},
        }
    )


def test_ensure_session_backdates_last_monotonic_from_event_timestamp(
    error_log: Path,
) -> None:
    """_ensure_session sets last_monotonic so silence == age_seconds on first tick.

    A NEW_SESSION event whose timestamp is age_seconds old should produce a
    _SessionContext where clock() - last_monotonic ≈ age_seconds (within 2s
    tolerance for test execution overhead).

    Use 400s — past the 5-min stale threshold but under the 35-min evict
    threshold — so the session survives the first lifecycle pass and we can
    inspect its last_monotonic.
    """
    age_seconds = 400.0  # > stale (300s), < evict (2100s)
    clock = FakeClock(value=time.monotonic())
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(toml_path=error_log.parent / "absent.toml"),
        clock=clock,
        registry=_registry_with_counting(),
    )

    old_timestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    source.push([make_source_event(SourceEventKind.NEW_SESSION, timestamp=old_timestamp)])
    list(aggregator.tick())

    # Session should be STALE (400s > stale threshold), not evicted.
    ctx = aggregator.sessions["sess-1"]
    assert ctx.state is SessionState.STALE, (
        f"expected STALE for {age_seconds}s-old session, got {ctx.state}"
    )
    silence = clock() - ctx.last_monotonic
    assert abs(silence - age_seconds) < 2.0, (
        f"expected silence ≈ {age_seconds}s, got {silence:.1f}s"
    )


def test_ingest_old_event_does_not_refresh_last_monotonic_forward(
    error_log: Path,
) -> None:
    """Replaying old events must not advance last_monotonic toward the current clock.

    After a session is created with a back-dated timestamp (400s old), APPEND
    events whose JSONL timestamps are equally old must not push last_monotonic
    to self._clock().  If they did, the lifecycle pass would see silence ≈ 0
    and keep the session ACTIVE when it should be STALE.
    """
    age_seconds = 400.0  # > stale (300s), < evict (2100s)
    clock = FakeClock(value=time.monotonic())
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_config(),
        project_registry=ProjectRegistry(toml_path=error_log.parent / "absent.toml"),
        clock=clock,
        registry=_registry_with_counting(),
    )

    old_timestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, timestamp=old_timestamp),
            make_source_event(
                SourceEventKind.APPEND,
                line=_old_event_line(age_seconds),
                timestamp=old_timestamp,
            ),
        ]
    )
    list(aggregator.tick())

    # Session must be STALE — the old APPEND must not have reset it to ACTIVE.
    ctx = aggregator.sessions["sess-1"]
    assert ctx.state is SessionState.STALE, (
        f"old APPEND reset lifecycle: expected STALE, got {ctx.state}"
    )
    # last_monotonic must remain far in the past; not close to clock().
    silence = clock() - ctx.last_monotonic
    assert silence > age_seconds - 5.0, (
        f"last_monotonic was advanced forward: silence={silence:.1f}s < {age_seconds - 5}s"
    )


def test_ingest_fresh_event_refreshes_last_monotonic_normally(
    error_log: Path,
) -> None:
    """A live APPEND event with a current timestamp advances last_monotonic.

    For a session that started live (timestamp ≈ now), ingesting a fresh
    JSONL event (timestamp ≈ now) must advance last_monotonic to
    approximately self._clock() so the silence stays near 0 and the session
    remains ACTIVE.
    """
    clock = FakeClock(value=time.monotonic())
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
            make_source_event(SourceEventKind.APPEND, line=good_user_line("live")),
        ]
    )
    list(aggregator.tick())

    ctx = aggregator.sessions["sess-1"]
    silence = clock() - ctx.last_monotonic
    # Fresh event: silence must be very small (under 5s for test overhead).
    assert silence < 5.0, f"fresh APPEND did not refresh last_monotonic: silence={silence:.1f}s"
