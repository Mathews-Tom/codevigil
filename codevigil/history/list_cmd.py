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
- ``task_type`` — session task label from classifier (hidden when no session
  in the result set has a task type; shown with ``[experimental]`` badge when
  present). The column is absent entirely — not just empty — when no session
  carries a task type, preserving backward compatibility with pre-classifier
  history.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import rich.console
import rich.markup
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

# Badge appended to task label values when the classifier is experimental.
_EXPERIMENTAL_BADGE: str = "[experimental]"


def run_list(
    *,
    store_dir: Path | None = None,
    project: str | None = None,
    since: date | None = None,
    until: date | None = None,
    severity: SeverityLabel | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    task_type: str | None = None,
    classifier_experimental: bool = True,
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
        task_type: Filter by session task type label (exact match). Sessions
            with no task type are excluded when this is set.
        classifier_experimental: When ``True``, task type values are
            annotated with ``[experimental]``. Set from
            ``classifier.experimental`` config key.
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
        task_type=task_type,
        thresholds=thresholds,
    )

    console = rich.console.Console(file=out, highlight=False)
    console.print(_build_table(filtered, classifier_experimental=classifier_experimental))
    return 0


def _build_table(
    reports: list[SessionReport],
    *,
    classifier_experimental: bool = True,
) -> rich.table.Table:
    # Determine whether the task_type column should be shown: only when at
    # least one session in the result set has a non-None session_task_type.
    # "Hidden" means the column header itself is absent, not just empty cells.
    show_task_col = any(r.session_task_type is not None for r in reports)

    tbl = rich.table.Table(show_header=True, header_style="bold")
    tbl.add_column("session_id")
    tbl.add_column("project")
    tbl.add_column("started_at")
    tbl.add_column("duration", justify="right")
    tbl.add_column("severity", justify="center")
    tbl.add_column("model")
    tbl.add_column("permission_mode")
    tbl.add_column("metrics_summary")
    if show_task_col:
        # Column header carries the [experimental] badge when the flag is set.
        # rich.markup.escape prevents Rich from interpreting the brackets as a
        # markup tag and silently swallowing the text.
        badge = " " + rich.markup.escape("[experimental]") if classifier_experimental else ""
        tbl.add_column(f"task_type{badge}")

    for report in reports:
        project_display = report.project_name or report.project_hash or "—"
        started = format_started_at(report.started_at)
        duration = format_duration(report.duration_seconds)
        sev = severity_of_report(report)
        sev_style = _SEV_STYLE.get(sev, "default")
        model_display = report.model or "—"
        pmode_display = report.permission_mode or "—"
        metrics_col = top_metrics_summary(report.metrics, n=2)

        row: tuple[str, ...] = (
            short_id(report.session_id),
            project_display,
            started,
            duration,
            f"[{sev_style}]{sev}[/{sev_style}]",
            model_display,
            pmode_display,
            metrics_col,
        )

        if show_task_col:
            task_val = report.session_task_type or "—"
            row = (*row, task_val)

        tbl.add_row(*row)

    return tbl


__all__ = ["run_list"]
