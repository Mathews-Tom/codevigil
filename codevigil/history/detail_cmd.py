"""``codevigil history <SESSION_ID>`` renderer.

Renders a single ``SessionReport`` with:

1. Header block — session id, project, model, permission_mode, started_at,
   duration, final severity.
2. Metric trajectory — for each metric, shows the final value and severity.
3. Stop-phrase context snippets — reads ``recent_hits[].context_snippet``
   from the ``stop_phrase`` metric detail when present.

Uses ``rich`` throughout: panels for sections, a table for the metric rows.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import rich.console
import rich.panel
import rich.table

from codevigil.analysis.store import SessionReport, SessionStore
from codevigil.history.filters import (
    classify_metric_severity,
    format_duration,
    format_started_at,
    severity_of_report,
)

_SEV_STYLE: dict[str, str] = {"ok": "green", "warn": "yellow", "crit": "red"}


def run_detail(
    session_id: str,
    *,
    store_dir: Path | None = None,
    out: Any = None,
) -> int:
    """Render a single session from the store.

    Parameters:
        session_id: Full or partial session id. Exact match required
            against the file name in the store.
        store_dir: Override the default ``SessionStore`` directory.
        out: Output stream. Defaults to ``sys.stdout``.

    Returns:
        ``0`` on success, ``1`` when the session is not found.
    """
    if out is None:
        out = sys.stdout

    store = SessionStore(base_dir=store_dir)
    report = store.get_report(session_id)
    if report is None:
        out.write(f"session not found: {session_id!r}\n")
        return 1

    _render(report, out=out)
    return 0


def _render(report: SessionReport, *, out: Any) -> None:
    console = rich.console.Console(file=out, highlight=False)

    # --- header panel ---
    console.print(
        rich.panel.Panel(
            "\n".join(_header_lines(report)),
            title=f"[bold]Session: {report.session_id}[/bold]",
            expand=False,
        )
    )

    # --- metrics table ---
    tbl = rich.table.Table(title="Metrics", show_header=True)
    tbl.add_column("metric", style="cyan")
    tbl.add_column("value", justify="right")
    tbl.add_column("severity", justify="center")

    for name, value in sorted(report.metrics.items()):
        sev = classify_metric_severity(name, value)
        sev_style = _SEV_STYLE.get(sev, "white")
        tbl.add_row(name, f"{value:.4f}", f"[{sev_style}]{sev}[/{sev_style}]")

    console.print(tbl)

    # --- stop-phrase snippets ---
    snippets = _extract_stop_phrase_snippets()
    if snippets:
        snip_text = "\n".join(f"  [{i + 1}] {s}" for i, s in enumerate(snippets))
        console.print(
            rich.panel.Panel(
                snip_text,
                title="[bold]Stop-Phrase Context Snippets[/bold]",
                expand=False,
            )
        )


def _header_lines(report: SessionReport) -> list[str]:
    project_display = report.project_name or report.project_hash or "—"
    worst_sev = severity_of_report(report)
    return [
        f"project: {project_display}",
        f"model: {report.model or '—'}",
        f"permission_mode: {report.permission_mode or '—'}",
        f"started_at: {format_started_at(report.started_at)}",
        f"duration: {format_duration(report.duration_seconds)}",
        f"events: {report.event_count}",
        f"parse_confidence: {report.parse_confidence:.4f}",
        f"severity: {worst_sev}",
    ]


def _extract_stop_phrase_snippets() -> list[str]:
    """Extract ``context_snippet`` strings from the stop_phrase detail.

    Returns an empty list: ``SessionReport`` stores only the final float
    scalar; the context_snippet data lives in the collector's
    ``MetricSnapshot.detail``, which is not persisted to the store.
    When the store gains richer snapshot persistence, update here.
    """
    return []


__all__ = ["run_detail"]
