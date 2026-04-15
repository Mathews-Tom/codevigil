"""Terminal renderer color-mode output tests."""

from __future__ import annotations

import io
from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def test_color_mode_emits_ansi_codes() -> None:
    stream = io.StringIO()  # not a TTY → no clear-screen
    renderer = TerminalRenderer(stream=stream, use_color=True)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()

    output = stream.getvalue()
    # StringIO is not a TTY, so the clear-screen escape must not appear.
    assert "\x1b[2J" not in output
    # ANSI escape sequences must be present in color mode.
    assert "\x1b[" in output
    # Severity words and header must still be present.
    assert "OK" in output
    assert "WARN" in output
    assert "codevigil" in output
    assert "\x1b[1m" in output  # bold → codevigil header
    assert "\x1b[2m" in output  # dim → separators / badges


def test_plain_mode_emits_no_ansi() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    assert "\x1b[" not in stream.getvalue()
