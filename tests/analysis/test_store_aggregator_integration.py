"""Integration tests: aggregator → store persistence path.

Verifies that:
- With persistence disabled (default), no files are written under the store dir.
- With persistence enabled, a finalized session report is written on eviction.
- The written report is a valid SessionReport with correct field values.
- The activation log line fires on first write.
- I/O errors in the store path do not crash the aggregator (they route to the
  error channel instead).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.analysis.store import CURRENT_SCHEMA_VERSION, SessionStore
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    enable_persistence: bool = False,
    sessions_dir: Path | None = None,
) -> dict[str, object]:
    cfg: dict[str, object] = {
        "watch": {
            "stale_after_seconds": 300,
            "evict_after_seconds": 2100,
        },
        "collectors": {
            "enabled": [],
        },
        "storage": {
            "enable_persistence": enable_persistence,
            "min_observation_days": 1,
        },
    }
    return cfg


def _write_one_session_then_evict(
    *,
    enable_persistence: bool,
    sessions_dir: Path,
    clock: FakeClock,
) -> SessionAggregator:
    config = _make_config(enable_persistence=enable_persistence)
    source = FakeSource()
    ts = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION,
                session_id="sess-test",
                timestamp=ts,
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="sess-test",
                line=good_user_line("test line"),
                timestamp=ts,
            ),
        ]
    )

    agg = SessionAggregator(source=source, config=config, clock=clock)
    # Point store at tmp dir

    if enable_persistence:
        agg._store = SessionStore(base_dir=sessions_dir)
    else:
        agg._store = None

    list(agg.tick())
    clock.advance(2200)
    list(agg.tick())
    return agg


# ---------------------------------------------------------------------------
# Persistence disabled (default)
# ---------------------------------------------------------------------------


def test_persistence_disabled_writes_nothing(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sessions_dir = tmp_path / "sessions"
    _write_one_session_then_evict(
        enable_persistence=False,
        sessions_dir=sessions_dir,
        clock=clock,
    )
    # Directory must NOT have been created
    assert not sessions_dir.exists()


def test_persistence_disabled_no_json_files(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()  # Pre-create to detect spurious writes
    _write_one_session_then_evict(
        enable_persistence=False,
        sessions_dir=sessions_dir,
        clock=clock,
    )
    json_files = list(sessions_dir.glob("*.json"))
    assert json_files == []


# ---------------------------------------------------------------------------
# Persistence enabled
# ---------------------------------------------------------------------------


def test_persistence_enabled_writes_json_file(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sessions_dir = tmp_path / "sessions"
    _write_one_session_then_evict(
        enable_persistence=True,
        sessions_dir=sessions_dir,
        clock=clock,
    )
    json_files = list(sessions_dir.glob("*.json"))
    assert len(json_files) == 1


def test_persistence_enabled_json_is_valid_report(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sessions_dir = tmp_path / "sessions"
    _write_one_session_then_evict(
        enable_persistence=True,
        sessions_dir=sessions_dir,
        clock=clock,
    )
    from codevigil.analysis.store import SessionReport

    json_files = list(sessions_dir.glob("*.json"))
    assert len(json_files) == 1
    raw = json.loads(json_files[0].read_text())
    report = SessionReport.from_dict(raw)
    assert report.schema_version == CURRENT_SCHEMA_VERSION
    assert report.session_id == "sess-test"
    assert report.event_count >= 1


def test_persistence_enabled_activation_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(0.0)
    sessions_dir = tmp_path / "sessions"
    with caplog.at_level(logging.INFO, logger="codevigil.analysis.store"):
        _write_one_session_then_evict(
            enable_persistence=True,
            sessions_dir=sessions_dir,
            clock=clock,
        )
    activation = [r for r in caplog.records if "persistence enabled" in r.message]
    assert len(activation) == 1


# ---------------------------------------------------------------------------
# Config flag: enable_persistence defaults to False
# ---------------------------------------------------------------------------


def test_aggregator_default_config_no_store() -> None:
    from codevigil.config import load_config

    resolved = load_config()
    assert resolved.values["storage"]["enable_persistence"] is False


def test_aggregator_store_none_when_persistence_disabled() -> None:
    config = _make_config(enable_persistence=False)
    source = FakeSource()
    agg = SessionAggregator(source=source, config=config)
    assert agg._store is None


def test_aggregator_store_not_none_when_persistence_enabled() -> None:
    config = _make_config(enable_persistence=True)
    source = FakeSource()
    agg = SessionAggregator(source=source, config=config)
    assert agg._store is not None


# ---------------------------------------------------------------------------
# Store write failure does not crash aggregator
# ---------------------------------------------------------------------------


def test_store_write_failure_does_not_crash_aggregator(tmp_path: Path) -> None:
    """A store I/O error must not propagate to crash the aggregator loop."""
    clock = FakeClock(0.0)
    config = _make_config(enable_persistence=True)
    source = FakeSource()
    ts = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    source.push(
        [
            make_source_event(
                SourceEventKind.NEW_SESSION,
                session_id="fail-sess",
                timestamp=ts,
            ),
            make_source_event(
                SourceEventKind.APPEND,
                session_id="fail-sess",
                line=good_user_line("test"),
                timestamp=ts,
            ),
        ]
    )
    agg = SessionAggregator(source=source, config=config, clock=clock)

    # Replace store with a broken one (unwritable path)
    bad_dir = tmp_path / "a_file.txt"
    bad_dir.write_text("I am a file not a dir")

    # parent is a file, so mkdir will fail when store tries to create subdir
    agg._store = SessionStore(base_dir=bad_dir / "subdir")

    list(agg.tick())
    clock.advance(2200)
    # Must not raise even when the store write fails
    list(agg.tick())
    # Aggregator is still alive and can continue ticking
    list(agg.tick())
