"""Tests for the wall-clock fleet header in TerminalRenderer.

The fleet header's ``updated=`` field must reflect the injected clock time at
render, not the maximum event timestamp seen in any session. It must also tick
forward across consecutive ticks even when no new events arrive.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots

# A deliberately old event timestamp so it can never match clock time.
_OLD_EVENT_TIME = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
_TICK_ONE_CLOCK = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
_TICK_TWO_CLOCK = _TICK_ONE_CLOCK + timedelta(seconds=5)


class _ControllableClock:
    """Mutable callable clock for deterministic tests."""

    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


# ---------------------------------------------------------------------------
# updated= reflects injected clock time, not event timestamp
# ---------------------------------------------------------------------------


def test_header_updated_reflects_clock_not_event_timestamp() -> None:
    """The header updated= must equal the injected clock, not any event time."""
    clock = _ControllableClock(_TICK_ONE_CLOCK)
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, clock=clock)

    # The meta's last_event_time is old — the header must NOT show this value.
    meta = make_meta(session_id="aabbccdd00112233")
    # Patch last_event_time to be far in the past to make the test unambiguous.
    from dataclasses import replace

    old_meta = replace(meta, last_event_time=_OLD_EVENT_TIME)

    renderer.begin_tick()
    renderer.render(make_snapshots(), old_meta)
    renderer.end_tick()

    output = stream.getvalue()
    clock_ts = _TICK_ONE_CLOCK.isoformat(timespec="seconds")
    old_ts = _OLD_EVENT_TIME.isoformat(timespec="seconds")

    assert clock_ts in output, f"Expected clock time {clock_ts!r} in output"
    assert old_ts not in output, f"Old event time {old_ts!r} must not appear in header"


# ---------------------------------------------------------------------------
# Header ticks forward across consecutive renders with no new events
# ---------------------------------------------------------------------------


def test_header_ticks_forward_with_no_new_events() -> None:
    """Two ticks with no new events produce different updated= timestamps."""
    clock = _ControllableClock(_TICK_ONE_CLOCK)
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, clock=clock)

    # Tick 1 — render one session.
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()

    first_output = stream.getvalue()

    # Advance clock before tick 2 but do NOT add any sessions.
    clock.advance(timedelta(seconds=5))

    # Tick 2 — no sessions.
    renderer.begin_tick()
    renderer.end_tick()

    second_output = stream.getvalue()[len(first_output) :]

    tick_one_ts = _TICK_ONE_CLOCK.isoformat(timespec="seconds")
    tick_two_ts = _TICK_TWO_CLOCK.isoformat(timespec="seconds")

    assert tick_one_ts in first_output
    assert tick_two_ts in second_output
    assert tick_one_ts not in second_output


# ---------------------------------------------------------------------------
# Header is present even when no sessions are buffered (empty tick)
# ---------------------------------------------------------------------------


def test_header_present_on_empty_tick() -> None:
    clock = _ControllableClock(_TICK_ONE_CLOCK)
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, clock=clock)

    renderer.begin_tick()
    renderer.end_tick()

    output = stream.getvalue()
    assert "codevigil" in output
    clock_ts = _TICK_ONE_CLOCK.isoformat(timespec="seconds")
    assert clock_ts in output
