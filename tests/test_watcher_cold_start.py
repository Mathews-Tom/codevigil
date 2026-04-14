"""Cold-start replay: PollingSource stamps NEW_SESSION with st_mtime.

Each fixture file has its mtime back-dated via stamp_watch_fixtures so that
PollingSource.poll() emits a NEW_SESSION SourceEvent whose timestamp carries
the file's true age.  The aggregator then back-dates last_monotonic from that
timestamp, and the lifecycle pass on the first tick classifies every session
correctly without waiting for real wall-clock time.

Tests use a realistic FakeClock seeded at time.monotonic() so lifecycle math
is correct: the back-dating formula subtracts age from self._clock() and
datetime.now(UTC) must match the scale of the fake clock value.
"""

from __future__ import annotations

import time
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
from codevigil.types import SessionState
from codevigil.watcher import PollingSource
from tests._aggregator_helpers import FakeClock
from tests.fixtures._watch_factory import FIXTURE_AGES, stamp_watch_fixtures

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Override HOME so PollingSource's privacy scope check passes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    err_path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(err_path)))
    yield tmp_path
    reset_error_channel()


def _watch_root(home: Path) -> Path:
    """Create the Claude projects directory layout under fake_home."""
    root = home / ".claude" / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_aggregator(source: PollingSource, clock: FakeClock) -> SessionAggregator:
    return SessionAggregator(
        source,
        config={
            "watch": {
                "stale_after_seconds": 300,  # 5 min
                "evict_after_seconds": 2100,  # 35 min
            },
            "collectors": {"enabled": []},
        },
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )


def _clock_for_now() -> FakeClock:
    """Return a FakeClock seeded at the current monotonic value.

    The back-dating formula computes:
        age = datetime.now(UTC) - source_event.timestamp  (wall-clock delta)
        last_monotonic = self._clock() - age              (monotonic shift)

    For the resulting silence (self._clock() - last_monotonic == age) to hold,
    the fake clock value must be on the same time scale as time.monotonic().
    Seeding from time.monotonic() achieves this without using the real clock
    in the lifecycle pass itself.
    """
    return FakeClock(value=time.monotonic())


def _state_for(aggregator: SessionAggregator, sid: str) -> SessionState:
    """Return the session state; EVICTED when absent from sessions dict."""
    ctx = aggregator.sessions.get(sid)
    if ctx is not None:
        return ctx.state
    return SessionState.EVICTED


# ---------------------------------------------------------------------------
# Individual fixture classification
# ---------------------------------------------------------------------------


def test_cold_replay_fresh_active_stays_active(fake_home: Path) -> None:
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = paths["fresh_active"].stem
    state = _state_for(aggregator, sid)
    assert state is SessionState.ACTIVE, (
        f"fresh_active (age {FIXTURE_AGES['fresh_active']}s) expected ACTIVE, got {state}"
    )


def test_cold_replay_recently_silent_stays_active(fake_home: Path) -> None:
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = paths["recently_silent"].stem
    state = _state_for(aggregator, sid)
    assert state is SessionState.ACTIVE, (
        f"recently_silent (age {FIXTURE_AGES['recently_silent']}s) expected ACTIVE, got {state}"
    )


def test_cold_replay_stale_becomes_stale(fake_home: Path) -> None:
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = paths["stale"].stem
    assert sid in aggregator.sessions, f"stale ({FIXTURE_AGES['stale']}s) evicted; expected STALE"
    state = aggregator.sessions[sid].state
    assert state is SessionState.STALE, (
        f"stale (age {FIXTURE_AGES['stale']}s) expected STALE, got {state}"
    )


def test_cold_replay_evicted_hours_is_evicted(fake_home: Path) -> None:
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = paths["evicted_hours"].stem
    assert sid not in aggregator.sessions, (
        f"evicted_hours (age {FIXTURE_AGES['evicted_hours']}s) expected EVICTED "
        f"(absent), but state is {aggregator.sessions.get(sid)}"
    )


def test_cold_replay_evicted_days_is_evicted(fake_home: Path) -> None:
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = paths["evicted_days"].stem
    assert sid not in aggregator.sessions, (
        f"evicted_days (age {FIXTURE_AGES['evicted_days']}s) expected EVICTED "
        f"(absent), but is still present"
    )


# ---------------------------------------------------------------------------
# Mixed cold-replay: all five fixtures at once
# ---------------------------------------------------------------------------


def test_cold_replay_mixed_only_fresh_and_recent_are_active(fake_home: Path) -> None:
    """Replaying all five fixtures simultaneously.

    After one tick:
    - fresh_active and recently_silent → ACTIVE (under 5-min stale threshold)
    - stale → STALE (over stale, under 35-min evict threshold)
    - evicted_hours and evicted_days → EVICTED (removed from sessions dict)
    """
    root = _watch_root(fake_home)
    paths = stamp_watch_fixtures(root)
    clock = _clock_for_now()
    source = PollingSource(root)
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    active_stems = {"fresh_active", "recently_silent"}
    stale_stems = {"stale"}
    evicted_stems = {"evicted_hours", "evicted_days"}

    for stem in active_stems:
        sid = paths[stem].stem
        assert sid in aggregator.sessions, f"{stem} missing from sessions dict"
        state = aggregator.sessions[sid].state
        assert state is SessionState.ACTIVE, (
            f"{stem} (age {FIXTURE_AGES[stem]}s) expected ACTIVE, got {state}"
        )

    for stem in stale_stems:
        sid = paths[stem].stem
        assert sid in aggregator.sessions, (
            f"{stem} ({FIXTURE_AGES[stem]}s) was evicted; expected STALE"
        )
        state = aggregator.sessions[sid].state
        assert state is SessionState.STALE, (
            f"{stem} (age {FIXTURE_AGES[stem]}s) expected STALE, got {state}"
        )

    for stem in evicted_stems:
        sid = paths[stem].stem
        assert sid not in aggregator.sessions, (
            f"{stem} (age {FIXTURE_AGES[stem]}s) expected EVICTED (absent), but still in sessions"
        )
