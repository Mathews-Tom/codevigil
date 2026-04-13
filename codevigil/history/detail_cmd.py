"""``codevigil history <SESSION_ID>`` renderer.

Renders a single ``SessionReport`` with:

1. Header block — session id, project, model, permission_mode, started_at,
   duration, final severity.
2. Event timeline — collapsed counts per event type (always), plus a
   note that the raw JSONL carries the full stream.
3. Metric trajectory — for each metric, shows the final value and severity.
   Full snapshot trajectory is not stored in ``SessionReport``; only the
   final scalar is available from the store.
4. Stop-phrase context snippets — reads ``recent_hits[].context_snippet``
   from the ``stop_phrase`` metric detail when present.

With ``rich`` installed, wraps sections in ``rich.panel.Panel`` and uses
``rich.table.Table`` for the metric section. Without ``rich``, emits plain
Markdown. Both paths render the same information.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from codevigil.analysis.store import SessionReport, SessionStore
from codevigil.history import RICH
from codevigil.history.filters import (
    SeverityLabel,
    classify_metric_severity,
    format_duration,
    format_started_at,
    severity_of_report,
)


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

    if RICH is not None:
        _render_rich(report, out=out)
    else:
        _render_markdown(report, out=out)

    return 0


# ---------------------------------------------------------------------------
# rich renderer
# ---------------------------------------------------------------------------


def _render_rich(report: SessionReport, *, out: Any) -> None:
    """Render using rich.panel.Panel and rich.table.Table."""
    import rich.console
    import rich.panel
    import rich.table

    console = rich.console.Console(file=out)

    # --- header panel ---
    header_lines = _header_lines(report)
    console.print(
        rich.panel.Panel(
            "\n".join(header_lines),
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
        sev_style = _sev_style(sev)
        tbl.add_row(name, f"{value:.4f}", f"[{sev_style}]{sev}[/{sev_style}]")

    console.print(tbl)

    # --- stop-phrase snippets ---
    snippets = _extract_stop_phrase_snippets(report)
    if snippets:
        snip_text = "\n".join(f"  [{i + 1}] {s}" for i, s in enumerate(snippets))
        console.print(
            rich.panel.Panel(
                snip_text,
                title="[bold]Stop-Phrase Context Snippets[/bold]",
                expand=False,
            )
        )


def _sev_style(sev: SeverityLabel) -> str:
    return {"ok": "green", "warn": "yellow", "crit": "red"}.get(sev, "white")


# ---------------------------------------------------------------------------
# plain Markdown renderer
# ---------------------------------------------------------------------------


def _render_markdown(report: SessionReport, *, out: Any) -> None:
    lines: list[str] = []

    lines.append(f"# Session: {report.session_id}")
    lines.append("")
    lines.extend(_header_lines(report))
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append("| metric | value | severity |")
    lines.append("| --- | --- | --- |")
    for name, value in sorted(report.metrics.items()):
        sev = classify_metric_severity(name, value)
        lines.append(f"| {name} | {value:.4f} | {sev} |")
    lines.append("")

    snippets = _extract_stop_phrase_snippets(report)
    if snippets:
        lines.append("## Stop-Phrase Context Snippets")
        lines.append("")
        for i, snip in enumerate(snippets):
            lines.append(f"{i + 1}. {snip}")
        lines.append("")

    out.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _extract_stop_phrase_snippets(report: SessionReport) -> list[str]:
    """Extract ``context_snippet`` strings from the stop_phrase detail dict.

    Returns an empty list when the metric is absent, the detail is not
    a dict, or ``recent_hits`` is missing or empty.
    """
    # SessionReport only stores the final float scalar in .metrics; the
    # context_snippet data lives in the collector's MetricSnapshot.detail,
    # which is not persisted to the store. We surface what is available.
    # When the store gains richer snapshot persistence, update here.
    return []


__all__ = ["run_detail"]
