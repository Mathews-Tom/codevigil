"""Plain-text terminal renderer output shape tests."""

from __future__ import annotations

import io

from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def test_render_produces_header_session_and_metric_lines() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    meta = make_meta()
    snapshots = make_snapshots()

    renderer.begin_tick()
    renderer.render(snapshots, meta)
    assert stream.getvalue() == ""  # buffered until end_tick
    renderer.end_tick()

    output = stream.getvalue()
    assert "codevigil" in output
    assert "parse_confidence: 1.00" in output
    assert "[experimental thresholds]" in output
    assert "session: a3f7c2d0" in output
    assert "iree-loom" in output
    assert "ACTIVE" in output
    assert "2m 34s" in output
    for name in ("read_edit_ratio", "stop_phrase", "reasoning_loop"):
        assert name in output
    assert "5.2" in output
    assert "OK" in output
    assert "WARN" in output


def test_end_tick_single_flush_then_reset() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()

    first = stream.getvalue()
    # Next tick should start from a fresh block set.
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta(session_id="bbbbbbbbbbbb0000"))
    renderer.end_tick()

    second_full = stream.getvalue()
    second_only = second_full[len(first) :]
    assert "bbbbbbbb" in second_only
    # Previous session must not reappear in the second tick's payload.
    assert "a3f7c2d0" not in second_only


def test_experimental_badge_hidden_when_disabled() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, show_experimental_badge=False, use_color=False)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    assert "[experimental thresholds]" not in stream.getvalue()


def test_parse_confidence_uses_parse_health_snapshot() -> None:
    from codevigil.types import MetricSnapshot, Severity

    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    snapshots = [
        MetricSnapshot(
            name="parse_health",
            value=0.88,
            label="schema drift",
            severity=Severity.CRITICAL,
        )
    ]
    renderer.begin_tick()
    renderer.render(snapshots, make_meta(parse_confidence=1.0))
    renderer.end_tick()
    assert "parse_confidence: 0.88" in stream.getvalue()
