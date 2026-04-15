"""Project-row view rendering (Phase C4).

Exercises the ``display_mode='project'`` code path: one row per
project in the current tick, aggregated across all active sessions in
that project, sorted by severity then recency.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from codevigil.renderers.terminal import TerminalRenderer
from codevigil.types import MetricSnapshot, Severity
from tests.renderers._fixtures import make_meta, make_snapshots

_FIXED_NOW = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED_NOW


def _project_renderer(stream: io.StringIO, *, limit: int = 10) -> TerminalRenderer:
    return TerminalRenderer(
        stream=stream,
        use_color=False,
        display_limit=20,
        display_mode="project",
        display_project_limit=limit,
        clock=_fixed_clock,
    )


def test_project_mode_emits_one_row_per_project() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()

    snaps = make_snapshots()
    renderer.render(snaps, make_meta(session_id="s1" * 8, project_name="Open-ASM"))
    renderer.render(snaps, make_meta(session_id="s2" * 8, project_name="Open-ASM"))
    renderer.render(snaps, make_meta(session_id="s3" * 8, project_name="codevigil"))
    renderer.end_tick()

    out = stream.getvalue()
    assert "Open-ASM" in out
    assert "codevigil" in out
    # Project mode must not emit per-session block headers.
    assert "session: " not in out


def test_project_mode_session_count_column() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()
    snaps = make_snapshots()
    renderer.render(snaps, make_meta(session_id="a" * 16, project_name="proj-a"))
    renderer.render(snaps, make_meta(session_id="b" * 16, project_name="proj-a"))
    renderer.render(snaps, make_meta(session_id="c" * 16, project_name="proj-a"))
    renderer.end_tick()
    out = stream.getvalue()
    # Three sessions aggregated into one proj-a row; count column reads "3".
    assert "proj-a" in out
    # session column appears as "3" somewhere on the proj-a row.
    assert "3" in out


def test_project_mode_critical_severity_wins_rollup() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()

    ok_snaps = [
        MetricSnapshot(name="read_edit_ratio", value=1.0, label="R:E 1.0", severity=Severity.OK),
    ]
    crit_snaps = [
        MetricSnapshot(
            name="read_edit_ratio", value=0.1, label="R:E 0.1", severity=Severity.CRITICAL
        ),
    ]
    renderer.render(ok_snaps, make_meta(session_id="ok" * 8, project_name="alpha"))
    renderer.render(crit_snaps, make_meta(session_id="cr" * 8, project_name="alpha"))
    renderer.end_tick()

    out = stream.getvalue()
    assert "alpha" in out
    assert "CRIT" in out, "project row must adopt the worst session's severity"


def test_project_mode_sorts_critical_projects_first() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()

    ok_snaps = [
        MetricSnapshot(name="read_edit_ratio", value=1.0, label="ok", severity=Severity.OK),
    ]
    crit_snaps = [
        MetricSnapshot(name="read_edit_ratio", value=0.1, label="crit", severity=Severity.CRITICAL),
    ]
    renderer.render(ok_snaps, make_meta(session_id="a1" * 8, project_name="alpha-ok"))
    renderer.render(crit_snaps, make_meta(session_id="b1" * 8, project_name="beta-crit"))
    renderer.end_tick()

    out = stream.getvalue()
    idx_crit = out.find("beta-crit")
    idx_ok = out.find("alpha-ok")
    assert idx_crit != -1 and idx_ok != -1
    assert idx_crit < idx_ok, "critical projects must render first"


def test_project_mode_display_project_limit_truncates() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream, limit=2)
    renderer.begin_tick()
    snaps = make_snapshots()
    for i in range(5):
        renderer.render(
            snaps,
            make_meta(session_id=f"{i:016x}", project_name=f"proj-{i}"),
        )
    renderer.end_tick()

    out = stream.getvalue()
    # Only the first 2 projects by severity-then-recency render.
    rendered_projects = sum(1 for i in range(5) if f"proj-{i}" in out)
    assert rendered_projects == 2
    assert "5 active projects" in out
    assert "display_project_limit" in out


def test_project_mode_empty_shows_placeholder() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()
    renderer.end_tick()
    out = stream.getvalue()
    assert "no active sessions" in out


def test_project_mode_updated_column_formats_recent_delta() -> None:
    stream = io.StringIO()
    renderer = _project_renderer(stream)
    renderer.begin_tick()
    snaps = make_snapshots()
    # Meta's last_event_time is _FIXED_NOW - something based on make_meta.
    # Make one session that updated 30 seconds before the renderer's clock.
    recent_meta = make_meta(session_id="r" * 16, project_name="recent")
    # Override last_event_time to be exactly 30 seconds before fixed now.
    import dataclasses

    recent_meta = dataclasses.replace(
        recent_meta, last_event_time=_FIXED_NOW - timedelta(seconds=30)
    )
    renderer.render(snaps, recent_meta)
    renderer.end_tick()

    out = stream.getvalue()
    assert "recent" in out
    assert "30s ago" in out


def test_session_mode_preserves_legacy_blocks() -> None:
    """Default ``display_mode='session'`` must still emit per-session blocks."""
    stream = io.StringIO()
    renderer = TerminalRenderer(
        stream=stream,
        use_color=False,
        display_limit=20,
        display_mode="session",
        clock=_fixed_clock,
    )
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta(session_id="a" * 16))
    renderer.end_tick()

    out = stream.getvalue()
    assert "session: " in out
