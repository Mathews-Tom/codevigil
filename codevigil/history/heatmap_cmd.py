"""``codevigil history heatmap <SESSION_ID>`` -- tool x severity matrix.

The heatmap renders a ``rich.table.Table`` where:
- Rows are metric names from the session report.
- Columns are severity labels (``ok``, ``warn``, ``crit``).
- Cells show the metric value for that (metric, severity) intersection, or
  ``—`` when the metric falls in a different severity bucket.

Color: ok=green, warn=yellow, crit=red cells.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import rich.console
import rich.table

from codevigil.analysis.store import SessionReport, SessionStore
from codevigil.history.filters import classify_metric_severity


def run_heatmap(
    session_id: str,
    *,
    store_dir: Path | None = None,
    out: Any = None,
    err: Any = None,
) -> int:
    """Render a tool x severity heatmap for a single session.

    Parameters:
        session_id: Session id to render.
        store_dir: Override the default ``SessionStore`` directory.
        out: Output stream. Defaults to ``sys.stdout``.
        err: Error stream. Defaults to ``sys.stderr``.

    Returns:
        ``0`` on success, ``1`` when session not found.
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    store = SessionStore(base_dir=store_dir)
    report = store.get_report(session_id)
    if report is None:
        out.write(f"session not found: {session_id!r}\n")
        return 1

    _render_heatmap(report, out=out)
    return 0


def _render_heatmap(report: SessionReport, *, out: Any) -> None:
    console = rich.console.Console(file=out, highlight=False)

    tbl = rich.table.Table(
        title=f"Heatmap: {report.session_id}",
        show_header=True,
    )
    tbl.add_column("metric", style="bold")
    tbl.add_column("ok (value)", justify="right", style="green")
    tbl.add_column("warn (value)", justify="right", style="yellow")
    tbl.add_column("crit (value)", justify="right", style="red")

    for name, value in sorted(report.metrics.items()):
        sev = classify_metric_severity(name, value)
        ok_val = f"{value:.4f}" if sev == "ok" else "—"
        warn_val = f"{value:.4f}" if sev == "warn" else "—"
        crit_val = f"{value:.4f}" if sev == "crit" else "—"
        tbl.add_row(name, ok_val, warn_val, crit_val)

    console.print(tbl)


__all__ = ["run_heatmap"]
