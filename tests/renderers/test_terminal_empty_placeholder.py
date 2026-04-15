"""Empty-fleet placeholder line in TerminalRenderer.end_tick.

When the aggregator yields zero non-evicted sessions in a tick (e.g.
every session has gone STALE/EVICTED under the 35-min cold-start rule),
the renderer must still emit a visible body line so ``codevigil watch``
does not look frozen on a single-line header.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from codevigil.renderers.terminal import TerminalRenderer

_FIXED_NOW = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)

_PLACEHOLDER_SUBSTRING = "no active sessions"


def _fixed_clock() -> datetime:
    return _FIXED_NOW


def _make_renderer(stream: io.StringIO) -> TerminalRenderer:
    return TerminalRenderer(
        stream=stream,
        use_color=False,
        display_limit=20,
        clock=_fixed_clock,
    )


def test_empty_tick_emits_placeholder_line() -> None:
    stream = io.StringIO()
    renderer = _make_renderer(stream)
    renderer.begin_tick()
    renderer.end_tick()
    output = stream.getvalue()
    assert "codevigil" in output
    assert _PLACEHOLDER_SUBSTRING in output
    assert "session:" not in output


def test_empty_tick_placeholder_absent_when_blocks_present() -> None:
    from tests.renderers._fixtures import make_meta, make_snapshots

    stream = io.StringIO()
    renderer = _make_renderer(stream)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta(session_id="a" * 16))
    renderer.end_tick()
    output = stream.getvalue()
    assert "session:" in output
    assert _PLACEHOLDER_SUBSTRING not in output
