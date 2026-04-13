"""Tests for SessionAggregator status metrics added in Phase 0.

Covers:
- eviction_churn counter: sessions evicted per tick, reset at tick start.
- cohort_size gauge: live session count at end of tick.
- invalid-path WARN: emitted once per distinct bad path; fallback hash is
  non-empty and deterministic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    ErrorChannel,
    ErrorLevel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import Collector
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import FakeClock, FakeSource, make_source_event
from tests._watcher_helpers import read_error_records

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


_MINIMAL_CONFIG: dict[str, object] = {
    "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
    "collectors": {"enabled": []},
}

_REGISTRY: dict[str, type[Collector]] = {ParseHealthCollector.name: ParseHealthCollector}


def _make_aggregator(
    source: FakeSource,
    clock: FakeClock,
) -> SessionAggregator:
    return SessionAggregator(
        source,
        config=_MINIMAL_CONFIG,
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_REGISTRY,
    )


# ---------------------------------------------------------------------------
# eviction_churn counter
# ---------------------------------------------------------------------------


def test_eviction_churn_is_zero_before_any_tick(error_log: Path) -> None:
    source = FakeSource()
    agg = _make_aggregator(source, FakeClock())
    assert agg.eviction_churn == 0


def test_eviction_churn_increments_on_delete(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push([make_source_event(SourceEventKind.NEW_SESSION, session_id="s1")])
    list(agg.tick())
    assert agg.eviction_churn == 0

    source.push([make_source_event(SourceEventKind.DELETE, session_id="s1")])
    list(agg.tick())
    assert agg.eviction_churn == 1


def test_eviction_churn_resets_to_zero_on_next_tick(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push([make_source_event(SourceEventKind.NEW_SESSION, session_id="s1")])
    list(agg.tick())

    source.push([make_source_event(SourceEventKind.DELETE, session_id="s1")])
    list(agg.tick())
    assert agg.eviction_churn == 1

    # Next tick has no evictions.
    list(agg.tick())
    assert agg.eviction_churn == 0


def test_eviction_churn_counts_silence_timeout_evictions(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    # Create two sessions.
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s1"),
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s2"),
        ]
    )
    list(agg.tick())
    assert agg.eviction_churn == 0

    # Advance clock past evict_after_seconds (2100).
    clock.advance(2101.0)
    list(agg.tick())
    assert agg.eviction_churn == 2


# ---------------------------------------------------------------------------
# cohort_size gauge
# ---------------------------------------------------------------------------


def test_cohort_size_zero_before_any_tick(error_log: Path) -> None:
    source = FakeSource()
    agg = _make_aggregator(source, FakeClock())
    assert agg.cohort_size == 0


def test_cohort_size_counts_live_sessions(error_log: Path) -> None:
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s1"),
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s2"),
        ]
    )
    list(agg.tick())
    assert agg.cohort_size == 2


def test_cohort_size_decrements_on_eviction(error_log: Path) -> None:
    clock = FakeClock(value=0.0)
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s1"),
            make_source_event(SourceEventKind.NEW_SESSION, session_id="s2"),
        ]
    )
    list(agg.tick())
    assert agg.cohort_size == 2

    source.push([make_source_event(SourceEventKind.DELETE, session_id="s1")])
    list(agg.tick())
    assert agg.cohort_size == 1


# ---------------------------------------------------------------------------
# Invalid-path WARN + deterministic fallback hash
# ---------------------------------------------------------------------------


def test_invalid_path_emits_warn_not_info(error_log: Path) -> None:
    """A path lacking 'projects/<hash>' triggers a WARN-level log record."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    bad_path = Path("/tmp/something/sessions/s1.jsonl")
    source.push([make_source_event(SourceEventKind.NEW_SESSION, path=bad_path, session_id="s1")])
    list(agg.tick())

    records = read_error_records(error_log)
    warn_records = [
        r
        for r in records
        if r.get("code") == "aggregator.project_layout_unknown"
        and r.get("level") == ErrorLevel.WARN.value
    ]
    assert len(warn_records) == 1, (
        f"expected exactly one WARN for project_layout_unknown; got: {warn_records}"
    )


def test_invalid_path_warn_fires_only_once_per_distinct_path(error_log: Path) -> None:
    """The WARN is de-duplicated: same path seen twice emits only one record."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    bad_path = Path("/tmp/other/sessions/s1.jsonl")

    # First session with the bad path.
    source.push([make_source_event(SourceEventKind.NEW_SESSION, path=bad_path, session_id="s1")])
    list(agg.tick())

    # Second session reusing the same invalid path.
    source.push([make_source_event(SourceEventKind.NEW_SESSION, path=bad_path, session_id="s2")])
    list(agg.tick())

    records = read_error_records(error_log)
    warn_records = [r for r in records if r.get("code") == "aggregator.project_layout_unknown"]
    assert len(warn_records) == 1, f"expected de-duplicated WARN; got {len(warn_records)} records"


def test_invalid_path_different_paths_each_warn_once(error_log: Path) -> None:
    """Two distinct bad paths each emit exactly one WARN."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    path_a = Path("/tmp/a/sessions/s1.jsonl")
    path_b = Path("/tmp/b/sessions/s2.jsonl")

    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION, path=path_a, session_id="s1"),
            make_source_event(SourceEventKind.NEW_SESSION, path=path_b, session_id="s2"),
        ]
    )
    list(agg.tick())

    records = read_error_records(error_log)
    warn_records = [r for r in records if r.get("code") == "aggregator.project_layout_unknown"]
    assert len(warn_records) == 2


def test_invalid_path_fallback_hash_is_nonempty(error_log: Path) -> None:
    """The project_hash for an invalid path is a non-empty string."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    bad_path = Path("/tmp/noproject/sessions/s1.jsonl")
    source.push([make_source_event(SourceEventKind.NEW_SESSION, path=bad_path, session_id="s1")])
    results = list(agg.tick())

    assert len(results) == 1
    meta, _ = results[0]
    assert meta.project_hash != "", "project_hash must never be empty string"
    assert len(meta.project_hash) == 16, "fallback hash should be 16 hex chars"


def test_invalid_path_fallback_hash_is_deterministic(error_log: Path) -> None:
    """The fallback hash matches the SHA-256 prefix of the raw path string."""
    bad_path = Path("/tmp/noproject/sessions/s1.jsonl")
    expected = hashlib.sha256(str(bad_path).encode()).hexdigest()[:16]

    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push([make_source_event(SourceEventKind.NEW_SESSION, path=bad_path, session_id="s1")])
    results = list(agg.tick())

    meta, _ = results[0]
    assert meta.project_hash == expected


def test_valid_path_emits_no_warn(error_log: Path) -> None:
    """A path with a 'projects/<hash>' segment produces no WARN."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    # make_source_event default path is the canonical layout.
    source.push([make_source_event(SourceEventKind.NEW_SESSION, session_id="s1")])
    list(agg.tick())

    records = read_error_records(error_log)
    warn_records = [r for r in records if r.get("code") == "aggregator.project_layout_unknown"]
    assert len(warn_records) == 0


def test_valid_path_project_hash_extracted_correctly(error_log: Path) -> None:
    """The hash component from a valid path is returned unchanged."""
    clock = FakeClock()
    source = FakeSource()
    agg = _make_aggregator(source, clock)

    source.push([make_source_event(SourceEventKind.NEW_SESSION, session_id="s1")])
    results = list(agg.tick())

    meta, _ = results[0]
    # The default path in make_source_event is:
    # /home/u/.claude/projects/abc12345/sessions/sess-1.jsonl
    assert meta.project_hash == "abc12345"
