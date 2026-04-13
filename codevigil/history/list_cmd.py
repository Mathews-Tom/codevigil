"""``codevigil history list`` renderer.

Reads all sessions from the ``SessionStore``, applies the caller-supplied
filters, and renders a rich table to stdout. No per-row disk reads after
the initial store enumeration — the entire filtered list is built in memory
before any rendering occurs.

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

import rich.console
import rich.table

from codevigil.analysis.store import SessionReport, SessionStore
from codevigil.history.filters import (
    SeverityLabel,
    apply_filters,
    format_duration,
    format_started_at,
    severity_of_report,
    short_id,
    top_metrics_summary,
)

_SEV_STYLE: dict[str, str] = {"ok": "green", "warn": "yellow", "crit": "red"}


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
    """Enumerate the store, apply filters, render rich table.

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

    console = rich.console.Console(file=out, highlight=False)
    console.print(_build_table(filtered))
    return 0


def _build_table(reports: list[SessionReport]) -> rich.table.Table:
    tbl = rich.table.Table(show_header=True, header_style="bold")
    tbl.add_column("session_id")
    tbl.add_column("project")
    tbl.add_column("started_at")
    tbl.add_column("duration", justify="right")
    tbl.add_column("severity", justify="center")
    tbl.add_column("model")
    tbl.add_column("permission_mode")
    tbl.add_column("metrics_summary")

    for report in reports:
        project_display = report.project_name or report.project_hash or "—"
        started = format_started_at(report.started_at)
        duration = format_duration(report.duration_seconds)
        sev = severity_of_report(report)
        sev_style = _SEV_STYLE.get(sev, "default")
        model_display = report.model or "—"
        pmode_display = report.permission_mode or "—"
        metrics_col = top_metrics_summary(report.metrics, n=2)

        tbl.add_row(
            short_id(report.session_id),
            project_display,
            started,
            duration,
            f"[{sev_style}]{sev}[/{sev_style}]",
            model_display,
            pmode_display,
            metrics_col,
        )

    return tbl


__all__ = ["run_list"]
