"""Terminal renderer must not crash when the terminal is resized mid-tick."""

from __future__ import annotations

import io
import os
import shutil

import pytest

from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def test_render_survives_terminal_size_change(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)

    big = os.terminal_size((80, 24))
    small = os.terminal_size((40, 12))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): big)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    first = stream.getvalue()
    assert "codevigil" in first

    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): small)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    second_only = stream.getvalue()[len(first) :]
    # Still valid output. Design permits artifacts, not crashes.
    assert "codevigil" in second_only
    assert "read_edit_ratio" in second_only
