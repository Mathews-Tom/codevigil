"""ANSI-colored terminal renderer output tests."""

from __future__ import annotations

import io

from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def test_color_mode_emits_ansi_around_severity_words() -> None:
    stream = io.StringIO()  # not a TTY → no clear-screen
    renderer = TerminalRenderer(stream=stream, use_color=True)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()

    output = stream.getvalue()
    # StringIO is not a TTY, so the clear-screen escape must not appear.
    assert "\x1b[2J" not in output
    # Severity words should carry their color codes and a reset.
    assert "\x1b[32mOK\x1b[0m" in output
    assert "\x1b[33mWARN\x1b[0m" in output
    # Bold codevigil header.
    assert "\x1b[1mcodevigil\x1b[0m" in output


def test_plain_mode_emits_no_ansi() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    assert "\x1b[" not in stream.getvalue()
