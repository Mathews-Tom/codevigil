"""Watch heartbeat status row for TerminalRenderer."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import cast

from codevigil.renderers.terminal import TerminalRenderer, WatchStatus


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:  # type: ignore[override]
        return True


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 16, 10, 0, 0, tzinfo=UTC)


def test_watch_status_row_rendered_in_end_tick() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, clock=_fixed_clock)
    renderer.set_watch_status(
        WatchStatus(
            phase="idle",
            refresh_interval=60.0,
            next_refresh_at=_fixed_clock() + timedelta(seconds=42),
            last_refresh_at=_fixed_clock() - timedelta(seconds=18),
        )
    )
    renderer.begin_tick()
    renderer.end_tick()
    out = stream.getvalue()
    assert "refresh every 60s" in out
    assert "next refresh in 42s" in out
    assert "last refresh 18s ago" in out


def test_refresh_status_reuses_last_body_on_tty() -> None:
    stream = _FakeTty()
    renderer = TerminalRenderer(
        stream=cast(io.TextIOWrapper, stream),
        use_color=True,
        clock=_fixed_clock,
    )
    status = WatchStatus(
        phase="idle",
        refresh_interval=60.0,
        next_refresh_at=_fixed_clock() + timedelta(seconds=10),
        last_refresh_at=_fixed_clock() - timedelta(seconds=5),
        spinner_step=0,
    )
    renderer.set_watch_status(status)
    renderer.begin_tick()
    renderer.end_tick()
    first = stream.getvalue()
    assert first.count("\x1b[2J\x1b[H") == 1
    status.spinner_step = 1
    renderer.refresh_status()
    second = stream.getvalue()
    assert second != first
    assert "refresh every 60s" in second
    assert second.count("\x1b[2J\x1b[H") == 1


def test_watch_status_error_text() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, clock=_fixed_clock)
    renderer.set_watch_status(
        WatchStatus(
            phase="error",
            refresh_interval=60.0,
            next_refresh_at=_fixed_clock() + timedelta(seconds=42),
            last_error_at=_fixed_clock() - timedelta(seconds=7),
        )
    )
    renderer.begin_tick()
    renderer.end_tick()
    out = stream.getvalue()
    assert "retrying on next cadence" in out
    assert "last refresh failed 7s ago" in out
