"""Alternate-screen buffer lifecycle for TerminalRenderer.

``codevigil watch`` uses the terminal alternate-screen buffer (DECSET
1049 / DECRST 1049) so the full-redraw clears do not clobber the user's
shell scrollback. The enter sequence must fire exactly once on the
first live end_tick and the leave sequence must fire on close.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import cast

from codevigil.renderers.terminal import TerminalRenderer


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:  # type: ignore[override]
        return True


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)


def _make_tty_renderer(stream: io.StringIO) -> TerminalRenderer:
    return TerminalRenderer(
        stream=cast(io.TextIOWrapper, stream),
        use_color=True,
        display_limit=20,
        clock=_fixed_clock,
    )


def test_alt_screen_entered_on_first_end_tick() -> None:
    stream = _FakeTty()
    renderer = _make_tty_renderer(stream)
    renderer.begin_tick()
    renderer.end_tick()
    out = stream.getvalue()
    assert "\x1b[?1049h" in out, "expected DECSET 1049 (enter alt screen)"


def test_alt_screen_entered_only_once_across_ticks() -> None:
    stream = _FakeTty()
    renderer = _make_tty_renderer(stream)
    for _ in range(3):
        renderer.begin_tick()
        renderer.end_tick()
    out = stream.getvalue()
    assert out.count("\x1b[?1049h") == 1


def test_alt_screen_left_on_close() -> None:
    stream = _FakeTty()
    renderer = _make_tty_renderer(stream)
    renderer.begin_tick()
    renderer.end_tick()
    renderer.close()
    out = stream.getvalue()
    assert "\x1b[?1049l" in out, "expected DECRST 1049 (leave alt screen)"
    # Enter/leave must be paired: leave comes after enter.
    assert out.rindex("\x1b[?1049l") > out.rindex("\x1b[?1049h")


def test_alt_screen_not_entered_without_tty() -> None:
    stream = io.StringIO()  # isatty() returns False
    renderer = TerminalRenderer(
        stream=stream,
        use_color=True,
        display_limit=20,
        clock=_fixed_clock,
    )
    renderer.begin_tick()
    renderer.end_tick()
    renderer.close()
    out = stream.getvalue()
    assert "\x1b[?1049h" not in out
    assert "\x1b[?1049l" not in out


def test_alt_screen_not_entered_when_color_disabled() -> None:
    stream = _FakeTty()
    renderer = TerminalRenderer(
        stream=cast(io.TextIOWrapper, stream),
        use_color=False,
        display_limit=20,
        clock=_fixed_clock,
    )
    renderer.begin_tick()
    renderer.end_tick()
    renderer.close()
    out = stream.getvalue()
    assert "\x1b[?1049h" not in out


def test_close_without_prior_tick_is_noop() -> None:
    stream = _FakeTty()
    renderer = _make_tty_renderer(stream)
    renderer.close()
    out = stream.getvalue()
    # No enter was emitted, so no leave either.
    assert "\x1b[?1049l" not in out
