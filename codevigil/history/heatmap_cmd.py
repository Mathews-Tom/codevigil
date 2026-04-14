"""``codevigil history heatmap <SESSION_ID>`` -- tool x severity matrix.

The heatmap renders a ``rich.table.Table`` where:
- Rows are metric names from the session report.
- Columns are severity labels (``ok``, ``warn``, ``crit``).
- Cells show the metric value for that (metric, severity) intersection, or
  ``—`` when the metric falls in a different severity bucket.

Color: ok=green, warn=yellow, crit=red cells.

``--axis task_type`` (experimental):
  Cross-tabulates metrics across all sessions grouped by their
  ``session_task_type`` label. Requires ``classifier.enabled = True`` in the
  config; exits 1 with a descriptive error when the classifier is disabled.
  The cross-tab is marked ``[experimental]`` because it depends on classifier
  output. The default single-session axis is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import rich.console
import rich.markup
import rich.table

from codevigil.analysis.store import SessionReport, SessionStore
from codevigil.history.filters import classify_metric_severity
from codevigil.renderers._bars import render_gradient_bar

# Valid axis choices for --axis.
_VALID_AXES: frozenset[str] = frozenset({"severity", "task_type"})

# Badge string for experimental surfaces (escaped for Rich markup).
_EXPERIMENTAL_BADGE: str = rich.markup.escape("[experimental]")


def run_heatmap(
    session_id: str,
    *,
    store_dir: Path | None = None,
    axis: str = "severity",
    classifier_enabled: bool = True,
    classifier_experimental: bool = True,
    out: Any = None,
    err: Any = None,
) -> int:
    """Render a heatmap for one session (default) or a task-type cross-tab.

    Parameters:
        session_id: Session id to render (used when ``axis="severity"``).
        store_dir: Override the default ``SessionStore`` directory.
        axis: Heatmap axis. ``"severity"`` (default) renders the single-session
            metric x severity matrix. ``"task_type"`` renders a cross-tab of
            metric means by task type across all stored sessions.
        classifier_enabled: When ``False`` and ``axis="task_type"``, exits 1
            with a descriptive error instead of rendering. Set from
            ``classifier.enabled`` config key.
        classifier_experimental: When ``True``, adds ``[experimental]`` badge
            to task-type axis output. Set from ``classifier.experimental``.
        out: Output stream. Defaults to ``sys.stdout``.
        err: Error stream. Defaults to ``sys.stderr``.

    Returns:
        ``0`` on success, ``1`` when session not found or when
        ``axis="task_type"`` is requested but the classifier is disabled.
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    if axis == "task_type":
        if not classifier_enabled:
            out.write(
                "error: --axis task_type requires classifier.enabled = true in config; "
                "the classifier is currently disabled. Enable it or use --axis severity.\n"
            )
            return 1
        store = SessionStore(base_dir=store_dir)
        reports = store.list_reports()
        _render_task_type_crosstab(
            reports, classifier_experimental=classifier_experimental, out=out
        )
        return 0

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

    # Pre-compute per-column maximums for proportional bars.
    ok_max = max(
        (v for n, v in report.metrics.items() if classify_metric_severity(n, v) == "ok"),
        default=1.0,
    )
    warn_max = max(
        (v for n, v in report.metrics.items() if classify_metric_severity(n, v) == "warn"),
        default=1.0,
    )
    crit_max = max(
        (v for n, v in report.metrics.items() if classify_metric_severity(n, v) == "crit"),
        default=1.0,
    )

    for name, value in sorted(report.metrics.items()):
        sev = classify_metric_severity(name, value)
        ok_val = render_gradient_bar(value, ok_max) if sev == "ok" else "—"
        warn_val = render_gradient_bar(value, warn_max) if sev == "warn" else "—"
        crit_val = render_gradient_bar(value, crit_max) if sev == "crit" else "—"
        tbl.add_row(name, ok_val, warn_val, crit_val)

    console.print(tbl)


def _render_task_type_crosstab(
    reports: list[SessionReport],
    *,
    classifier_experimental: bool,
    out: Any,
) -> None:
    """Render a metric x task_type cross-tab across all sessions.

    Groups sessions by ``session_task_type``. For each (task_type, metric)
    cell, shows the mean metric value across all sessions with that task type.
    Sessions whose ``session_task_type`` is ``None`` are grouped under the
    label ``(unclassified)``. Cells with no observations show ``—``.
    """
    console = rich.console.Console(file=out, highlight=False)

    # Collect unique task types and metric names.
    task_types: list[str] = []
    seen_types: set[str] = set()
    metric_names: set[str] = set()

    for report in reports:
        label = report.session_task_type or "(unclassified)"
        if label not in seen_types:
            seen_types.add(label)
            task_types.append(label)
        metric_names.update(report.metrics.keys())

    sorted_metrics = sorted(metric_names)
    # Build accumulator: task_type → metric → list[float]
    accum: dict[str, dict[str, list[float]]] = {tt: {} for tt in task_types}
    for report in reports:
        label = report.session_task_type or "(unclassified)"
        for mname, mval in report.metrics.items():
            accum[label].setdefault(mname, []).append(mval)

    badge = f" {_EXPERIMENTAL_BADGE}" if classifier_experimental else ""
    tbl = rich.table.Table(
        title=f"Heatmap by task_type{badge}",
        show_header=True,
    )
    tbl.add_column("metric", style="bold")
    for tt in task_types:
        tbl.add_column(tt, justify="right")

    # Pre-compute per task_type column maximums for proportional bars.
    col_max: dict[str, float] = {}
    for tt in task_types:
        vals_across_metrics = [sum(vs) / len(vs) for vs in accum[tt].values() if vs]
        col_max[tt] = max(vals_across_metrics, default=1.0)

    for mname in sorted_metrics:
        row_vals: list[str] = [mname]
        for tt in task_types:
            vals = accum[tt].get(mname, [])
            if vals:
                mean_val = sum(vals) / len(vals)
                row_vals.append(render_gradient_bar(mean_val, col_max[tt]))
            else:
                row_vals.append("—")
        tbl.add_row(*row_vals)

    console.print(tbl)


__all__ = ["run_heatmap"]
