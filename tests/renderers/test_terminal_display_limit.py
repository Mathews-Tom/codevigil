"""Tests for TerminalRenderer display_limit cap and truncation footer."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from codevigil.renderers.terminal import TerminalRenderer
from codevigil.types import MetricSnapshot, Severity
from tests.renderers._fixtures import make_meta, make_snapshots

_FIXED_NOW = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED_NOW


def _make_renderer(display_limit: int, stream: io.StringIO) -> TerminalRenderer:
    return TerminalRenderer(
        stream=stream,
        use_color=False,
        display_limit=display_limit,
        clock=_fixed_clock,
    )


def _populate_sessions(renderer: TerminalRenderer, count: int) -> None:
    """Buffer ``count`` distinct sessions into the renderer's current tick."""
    snapshots = make_snapshots()
    for i in range(count):
        sid = f"{i:016x}"
        meta = make_meta(session_id=sid)
        renderer.render(snapshots, meta)


# ---------------------------------------------------------------------------
# 30 sessions, display_limit=20
# ---------------------------------------------------------------------------


def test_display_limit_renders_capped_count_of_session_blocks() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=20, stream=stream)
    renderer.begin_tick()
    _populate_sessions(renderer, 30)
    renderer.end_tick()
    output = stream.getvalue()
    # Each session header contains "session:" — count occurrences.
    session_block_count = output.count("session:")
    assert session_block_count == 20


def test_display_limit_footer_present_when_truncated() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=20, stream=stream)
    renderer.begin_tick()
    _populate_sessions(renderer, 30)
    renderer.end_tick()
    output = stream.getvalue()
    assert "showing 20 of 30 active sessions" in output
    assert "watch.display_limit" in output


# ---------------------------------------------------------------------------
# 5 sessions, display_limit=20 — all fit, no footer
# ---------------------------------------------------------------------------


def test_display_limit_no_footer_when_set_fits() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=20, stream=stream)
    renderer.begin_tick()
    _populate_sessions(renderer, 5)
    renderer.end_tick()
    output = stream.getvalue()
    assert output.count("session:") == 5
    assert "showing" not in output
    assert "watch.display_limit" not in output


# ---------------------------------------------------------------------------
# display_limit=1 edge case
# ---------------------------------------------------------------------------


def test_display_limit_one_renders_exactly_one_session() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=1, stream=stream)
    renderer.begin_tick()
    _populate_sessions(renderer, 5)
    renderer.end_tick()
    output = stream.getvalue()
    assert output.count("session:") == 1
    assert "showing 1 of 5 active sessions" in output


# ---------------------------------------------------------------------------
# display_limit exactly equals session count — no footer
# ---------------------------------------------------------------------------


def test_display_limit_exact_match_no_footer() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=10, stream=stream)
    renderer.begin_tick()
    _populate_sessions(renderer, 10)
    renderer.end_tick()
    output = stream.getvalue()
    assert output.count("session:") == 10
    assert "showing" not in output


# ---------------------------------------------------------------------------
# Severity sort is preserved through the cap — CRIT sessions appear first
# ---------------------------------------------------------------------------


def test_display_limit_severity_sort_preserved() -> None:
    """CRITICAL session must be in the rendered cap even with a tight limit."""
    stream = io.StringIO()
    renderer = _make_renderer(display_limit=1, stream=stream)
    renderer.begin_tick()

    crit_snap = MetricSnapshot(
        name="stop_phrase",
        value=5.0,
        label="5 hits",
        severity=Severity.CRITICAL,
    )
    ok_snap = MetricSnapshot(
        name="stop_phrase",
        value=0.0,
        label="0 hits",
        severity=Severity.OK,
    )
    # Render the OK session first so ordering matters.
    renderer.render([ok_snap], make_meta(session_id="aaaaaaaaaaaaaaaa"))
    renderer.render([crit_snap], make_meta(session_id="bbbbbbbbbbbbbbbb"))
    renderer.end_tick()

    output = stream.getvalue()
    # Only one session rendered; it must be the CRITICAL one (bbbb…).
    assert output.count("session:") == 1
    assert "bbbbbbbb" in output
    assert "showing 1 of 2 active sessions" in output
