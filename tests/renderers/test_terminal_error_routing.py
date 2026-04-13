"""render_error level → route behaviour for the terminal renderer."""

from __future__ import annotations

import io

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource
from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def _make_err(level: ErrorLevel) -> CodevigilError:
    return CodevigilError(
        level=level,
        source=ErrorSource.PARSER,
        code="parser.test",
        message="something drifted",
    )


def _render_with_error(level: ErrorLevel) -> str:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    meta = make_meta()
    renderer.begin_tick()
    renderer.render(make_snapshots(), meta)
    renderer.render_error(_make_err(level), meta)
    renderer.end_tick()
    return stream.getvalue()


def test_info_is_silent_in_terminal() -> None:
    output = _render_with_error(ErrorLevel.INFO)
    assert "parser.test" not in output
    assert "something drifted" not in output


def test_warn_emits_footer_under_session_block() -> None:
    output = _render_with_error(ErrorLevel.WARN)
    assert "parser.test: something drifted" in output
    session_idx = output.index("session: a3f7c2d0")
    footer_idx = output.index("parser.test")
    assert footer_idx > session_idx


def test_error_emits_footer_under_session_block() -> None:
    output = _render_with_error(ErrorLevel.ERROR)
    assert "parser.test: something drifted" in output
    assert output.index("parser.test") > output.index("session: a3f7c2d0")


def test_critical_emits_banner_above_session_header() -> None:
    output = _render_with_error(ErrorLevel.CRITICAL)
    assert "CRITICAL" in output
    banner_idx = output.index("parser.test")
    session_idx = output.index("session: a3f7c2d0")
    assert banner_idx < session_idx


def test_critical_banner_is_red_when_colored() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=True)
    meta = make_meta()
    renderer.begin_tick()
    renderer.render(make_snapshots(), meta)
    renderer.render_error(_make_err(ErrorLevel.CRITICAL), meta)
    renderer.end_tick()
    output = stream.getvalue()
    # Red color escape should precede the banner text.
    assert "\x1b[31m" in output
