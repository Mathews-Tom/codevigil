"""``codevigil history list`` renderer.

Reads all sessions from the ``SessionStore``, applies the caller-supplied
filters, and renders a compact Markdown table to stdout. No per-row disk
reads after the initial store enumeration — the entire filtered list is
built in memory before any rendering occurs.

Columns:
- ``session_id`` — short form (12-char truncation after stripping ``agent-``)
- ``project`` — ``project_name`` if set, else ``project_hash``
- ``started_at`` — ``YYYY-MM-DD HH:MM``
- ``duration`` — human-readable (``1m30s``, ``1h02m``)
- ``severity`` — worst severity across all metrics (``ok``/``warn``/``crit``)
- ``model`` — model identifier or ``—``
- ``permission_mode`` — permission mode or ``—``
- ``metrics_summary`` — top-2 metrics by absolute value (``metric=val, …``)
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

from codevigil.analysis.store import SessionStore
from codevigil.history.filters import (
    SeverityLabel,
    apply_filters,
    format_duration,
    format_started_at,
    severity_of_report,
    short_id,
    top_metrics_summary,
)


def run_list(
    *,
    store_dir: Path | None = None,
    project: str | None = None,
    since: date | None = None,
    until: date | None = None,
    severity: SeverityLabel | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    thresholds: dict[str, tuple[float, float]] | None = None,
    out: Any = None,
) -> int:
    """Enumerate the store, apply filters, render Markdown table.

    Parameters:
        store_dir: Override the default ``SessionStore`` directory. Passed
            through to ``SessionStore(base_dir=...)``. ``None`` uses the
            XDG default.
        project: Filter by project name or project hash.
        since: Inclusive lower bound on ``started_at`` calendar date.
        until: Inclusive upper bound on ``started_at`` calendar date.
        severity: Filter by worst-severity label.
        model: Filter by model identifier.
        permission_mode: Filter by permission mode.
        thresholds: Per-metric (warn, crit) thresholds for severity
            classification. ``None`` uses built-in defaults.
        out: Output stream. Defaults to ``sys.stdout``.

    Returns:
        Exit code — ``0`` on success, ``1`` when the store directory does
        not exist.
    """
    if out is None:
        out = sys.stdout

    store = SessionStore(base_dir=store_dir)
    # Single enumeration pass — no per-row disk reads after this.
    all_reports = store.list_reports()

    filtered = apply_filters(
        all_reports,
        project=project,
        since=since,
        until=until,
        severity=severity,
        model=model,
        permission_mode=permission_mode,
        thresholds=thresholds,
    )

    out.write(_render_table(filtered))
    return 0


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_HEADERS = [
    "session_id",
    "project",
    "started_at",
    "duration",
    "severity",
    "model",
    "permission_mode",
    "metrics_summary",
]


def _render_table(reports: list[Any]) -> str:
    """Render a Markdown table from the filtered report list."""
    lines: list[str] = []
    lines.append("| " + " | ".join(_HEADERS) + " |")
    lines.append("| " + " | ".join("---" for _ in _HEADERS) + " |")

    for report in reports:
        project_display = report.project_name or report.project_hash or "—"
        started = format_started_at(report.started_at)
        duration = format_duration(report.duration_seconds)
        sev = severity_of_report(report)
        model_display = report.model or "—"
        pmode_display = report.permission_mode or "—"
        metrics_col = top_metrics_summary(report.metrics, n=2)

        row = [
            short_id(report.session_id),
            project_display,
            started,
            duration,
            sev,
            model_display,
            pmode_display,
            metrics_col,
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    return "\n".join(lines)


__all__ = ["run_list"]
