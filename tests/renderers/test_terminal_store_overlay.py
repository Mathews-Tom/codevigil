"""Project-row view with persistent-store overlay (Phase C4+).

Verifies that when the in-memory aggregator produces zero live
sessions, the project-row TUI still renders the top-N most recent
projects by reading the processed-session store via the injected
``store_project_reader`` callable.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from codevigil.analysis.processed_store import (
    ProcessedMetric,
    RecentProjectAggregate,
)
from codevigil.renderers.terminal import TerminalRenderer

_FIXED_NOW = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED_NOW


def _project(
    key: str,
    *,
    last_event_time: datetime,
    session_count: int = 1,
    critical: bool = False,
) -> RecentProjectAggregate:
    severity = "critical" if critical else "ok"
    return RecentProjectAggregate(
        project_key=key,
        project_hash=f"{key}hash",
        project_name=key,
        session_count=session_count,
        last_event_time=last_event_time,
        metrics=[
            ProcessedMetric(
                collector_name="parse_health",
                metric_name="parse_health",
                value=1.0,
                severity=severity,
                label="parse",
            ),
            ProcessedMetric(
                collector_name="read_edit_ratio",
                metric_name="read_edit_ratio",
                value=3.2,
                severity=severity,
                label="ratio",
            ),
        ],
    )


def _make_renderer(
    stream: io.StringIO,
    reader: object,
    *,
    limit: int = 10,
) -> TerminalRenderer:
    return TerminalRenderer(
        stream=stream,
        use_color=False,
        display_limit=20,
        display_mode="project",
        display_project_limit=limit,
        store_project_reader=reader,  # type: ignore[arg-type]
        clock=_fixed_clock,
    )


def test_store_overlay_renders_projects_when_no_live_sessions() -> None:
    stream = io.StringIO()
    projects = [
        _project("Open-ASM", last_event_time=datetime(2025, 11, 1, tzinfo=UTC)),
        _project("codevigil", last_event_time=datetime(2026, 1, 15, tzinfo=UTC)),
    ]
    renderer = _make_renderer(stream, lambda n: projects)
    renderer.begin_tick()
    renderer.end_tick()

    out = stream.getvalue()
    assert "Open-ASM" in out
    assert "codevigil" in out
    # Placeholder must NOT appear when the store overlay has projects.
    assert "no active sessions" not in out


def test_store_overlay_sorts_by_severity_then_recency() -> None:
    stream = io.StringIO()
    projects = [
        _project("old-ok", last_event_time=datetime(2024, 1, 1, tzinfo=UTC)),
        _project("recent-ok", last_event_time=datetime(2026, 4, 10, tzinfo=UTC)),
        _project(
            "ancient-crit",
            last_event_time=datetime(2023, 6, 1, tzinfo=UTC),
            critical=True,
        ),
    ]
    renderer = _make_renderer(stream, lambda n: projects)
    renderer.begin_tick()
    renderer.end_tick()

    out = stream.getvalue()
    idx_crit = out.find("ancient-crit")
    idx_recent = out.find("recent-ok")
    idx_old = out.find("old-ok")
    assert idx_crit < idx_recent < idx_old, "critical first, then recency descending"


def test_live_session_takes_precedence_over_store_entry() -> None:
    from tests.renderers._fixtures import make_meta, make_snapshots

    stream = io.StringIO()
    projects = [
        _project("Open-ASM", last_event_time=datetime(2025, 11, 1, tzinfo=UTC)),
    ]
    renderer = _make_renderer(stream, lambda n: projects)
    renderer.begin_tick()
    renderer.render(
        make_snapshots(),
        make_meta(session_id="a" * 16, project_name="Open-ASM"),
    )
    renderer.end_tick()

    out = stream.getvalue()
    # Exactly one Open-ASM row (live wins, no duplicate from store).
    assert out.count("Open-ASM") == 1
    # Live project row has a recent "updated" timestamp, not 165d+ ago.
    assert "165d" not in out


def test_store_overlay_respects_display_project_limit() -> None:
    stream = io.StringIO()
    projects = [
        _project(f"p{i}", last_event_time=datetime(2025, 1, i + 1, tzinfo=UTC)) for i in range(5)
    ]
    calls: list[int] = []

    def reader(limit: int) -> list[RecentProjectAggregate]:
        calls.append(limit)
        return projects[:limit]

    renderer = _make_renderer(stream, reader, limit=3)
    renderer.begin_tick()
    renderer.end_tick()

    out = stream.getvalue()
    assert calls == [3], "renderer must request exactly display_project_limit rows"
    rendered = sum(1 for i in range(5) if f"p{i}" in out)
    assert rendered == 3


def test_store_reader_error_degrades_gracefully() -> None:
    """A raising reader must not crash the tick loop."""
    stream = io.StringIO()

    def broken_reader(limit: int) -> list[RecentProjectAggregate]:
        raise RuntimeError("simulated store failure")

    renderer = _make_renderer(stream, broken_reader)
    renderer.begin_tick()
    renderer.end_tick()
    # Empty fleet + no store overlay → placeholder renders.
    assert "no active sessions" in stream.getvalue()


def test_store_overlay_session_count_column_shows_project_total() -> None:
    stream = io.StringIO()
    projects = [
        _project(
            "proj-a",
            last_event_time=datetime(2026, 4, 10, tzinfo=UTC),
            session_count=42,
        ),
    ]
    renderer = _make_renderer(stream, lambda n: projects)
    renderer.begin_tick()
    renderer.end_tick()
    out = stream.getvalue()
    assert "proj-a" in out
    assert "42" in out
