"""Tests for task type tag in the watch session header.

The ``_session_header_text`` method appends a right-side task tag
``[task: <label>] [experimental]`` when ``meta.session_task_type`` is set
and the renderer has ``show_experimental_badge=True``. The tag is suppressed
when ``session_task_type`` is None or when the experimental badge is off.
"""

from __future__ import annotations

import io

from codevigil.renderers.terminal import TerminalRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def _render_and_capture(
    *,
    session_task_type: str | None,
    show_experimental_badge: bool = True,
) -> str:
    """Render one tick and return the captured output as a string."""
    buf = io.StringIO()
    renderer = TerminalRenderer(
        stream=buf,
        use_color=False,
        show_experimental_badge=show_experimental_badge,
    )
    meta = make_meta(session_task_type=session_task_type)
    snapshots = make_snapshots()
    renderer.begin_tick()
    renderer.render(snapshots, meta)
    renderer.end_tick()
    return buf.getvalue()


class TestWatchTaskHeader:
    def test_task_tag_present_when_task_type_set(self) -> None:
        """[task: exploration] appears in the session header."""
        text = _render_and_capture(session_task_type="exploration")
        assert "[task: exploration]" in text

    def test_task_tag_absent_when_task_type_none(self) -> None:
        """No task tag when session_task_type is None."""
        text = _render_and_capture(session_task_type=None)
        assert "[task:" not in text

    def test_experimental_badge_with_task_tag(self) -> None:
        """[experimental] badge appears adjacent to the task tag."""
        text = _render_and_capture(
            session_task_type="mutation_heavy",
            show_experimental_badge=True,
        )
        assert "[task: mutation_heavy]" in text
        assert "[experimental]" in text

    def test_no_experimental_badge_when_flag_false(self) -> None:
        """No [experimental] badge when show_experimental_badge=False."""
        text = _render_and_capture(
            session_task_type="debug_loop",
            show_experimental_badge=False,
        )
        assert "[task: debug_loop]" in text
        assert "[experimental]" not in text

    def test_task_tag_absent_when_both_none_and_no_badge(self) -> None:
        """No task tag when session_task_type=None even with badge enabled."""
        text = _render_and_capture(
            session_task_type=None,
            show_experimental_badge=True,
        )
        assert "[task:" not in text

    def test_task_type_value_is_exact(self) -> None:
        """The task type label in the header matches exactly."""
        for label in ("exploration", "mutation_heavy", "debug_loop", "planning", "mixed"):
            text = _render_and_capture(session_task_type=label)
            assert f"[task: {label}]" in text, f"missing tag for label {label!r}"


class TestWatchClassifierDisabledDegradation:
    def test_no_tag_when_session_task_type_none(self) -> None:
        """Degradation: classifier disabled → session_task_type=None → no tag."""
        # The aggregator sets session_task_type=None when classifier is disabled.
        # The renderer degrades based solely on the value of meta.session_task_type.
        text = _render_and_capture(session_task_type=None)
        assert "[task:" not in text
        assert "[experimental]" not in text
