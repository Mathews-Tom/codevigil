"""Cold-replay regression: unresolved event timestamps must not reset
``last_monotonic`` forward.

Reproduces the failure mode where a session JSONL file is 5 days old on
disk but its individual event records carry no parseable ``timestamp``
field (pre-v1 shapes, records with empty timestamp strings, or records
where the timestamp key is absent entirely). The parser falls back to
``datetime.now(UTC)`` with ``timestamp_resolved=False``. Before the fix
the aggregator treated that fallback as a fresh event and advanced
``last_monotonic`` forward to now, flagging the 5-day-old session as
ACTIVE. After the fix unresolved timestamps are ignored by the
lifecycle advance path and the first lifecycle pass evicts the session
as intended.
"""

from __future__ import annotations

import json
import os
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


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    err_path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(err_path)))
    yield tmp_path
    reset_error_channel()


def _watch_root(home: Path) -> Path:
    root = home / ".claude" / "projects" / "proj" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_aggregator(source: PollingSource, clock: FakeClock) -> SessionAggregator:
    return SessionAggregator(
        source,
        config={
            "watch": {
                "stale_after_seconds": 300,
                "evict_after_seconds": 2100,
            },
            "collectors": {"enabled": []},
        },
        project_registry=ProjectRegistry(),
        clock=clock,
        registry={ParseHealthCollector.name: ParseHealthCollector},
    )


def _write_timestampless_session(path: Path, *, age_seconds: float) -> None:
    """Write a JSONL session file with records whose ``timestamp`` fields
    are empty strings, then back-date the file's mtime by ``age_seconds``.
    """
    lines = [
        json.dumps(
            {
                "type": "user",
                "timestamp": "",
                "session_id": path.stem,
                "message": {
                    "id": "u1",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "",
                "session_id": path.stem,
                "message": {
                    "id": "a1",
                    "content": [{"type": "text", "text": "yo"}],
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    now_wall = time.time()
    target_mtime = now_wall - age_seconds
    os.utime(path, (target_mtime, target_mtime))


def test_unresolved_timestamps_do_not_resurrect_old_session(fake_home: Path) -> None:
    """A 5-day-old file whose events have no parseable timestamp is
    EVICTED on the first lifecycle tick."""
    root = _watch_root(fake_home)
    path = root / "agent-abc123.jsonl"
    _write_timestampless_session(path, age_seconds=5 * 86400.0)

    clock = FakeClock(value=time.monotonic())
    source = PollingSource(fake_home / ".claude" / "projects")
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = path.stem
    assert sid not in aggregator.sessions, (
        f"5-day-old session with unresolved timestamps expected EVICTED, "
        f"but still present as {aggregator.sessions.get(sid)}"
    )


def test_unresolved_timestamps_do_not_bump_stale_to_active(fake_home: Path) -> None:
    """A session with no parseable event timestamps must not receive the
    STALE→ACTIVE transition from the 'coffee break' rule."""
    root = _watch_root(fake_home)
    path = root / "agent-def456.jsonl"
    # 10 minutes old: lifecycle should classify as STALE on tick 1, not
    # get resurrected by the subsequent event dispatch.
    _write_timestampless_session(path, age_seconds=10 * 60.0)

    clock = FakeClock(value=time.monotonic())
    source = PollingSource(fake_home / ".claude" / "projects")
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = path.stem
    assert sid in aggregator.sessions, "10-min-old session dropped from aggregator; expected STALE"
    state = aggregator.sessions[sid].state
    assert state is SessionState.STALE, f"unresolved-timestamp session expected STALE, got {state}"


def test_resolved_timestamps_still_evict_old_session(fake_home: Path) -> None:
    """Control: the same 5-day-old file with real timestamps must still
    be evicted. This guards against the fix over-correcting and breaking
    the resolved path."""
    root = _watch_root(fake_home)
    path = root / "agent-real.jsonl"
    from datetime import UTC, datetime, timedelta

    old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    lines = [
        json.dumps(
            {
                "type": "user",
                "timestamp": old_ts,
                "session_id": path.stem,
                "message": {
                    "id": "u1",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    now_wall = time.time()
    target_mtime = now_wall - 5 * 86400.0
    os.utime(path, (target_mtime, target_mtime))

    clock = FakeClock(value=time.monotonic())
    source = PollingSource(fake_home / ".claude" / "projects")
    aggregator = _make_aggregator(source, clock)

    list(aggregator.tick())

    sid = path.stem
    assert sid not in aggregator.sessions
