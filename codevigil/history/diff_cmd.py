"""``codevigil history diff A B`` renderer.

Aligns two sessions by LCS over their sorted metric name sequence and
renders a rich side-by-side comparison.

Header block compares:
- project, model, permission_mode
- duration (both values + delta in seconds)
- final severity
- per-metric delta (B - A) for metrics shared between the two sessions

Alignment algorithm:
- Collect the union of metric names from both sessions.
- Use ``difflib.SequenceMatcher`` over the sorted metric name sequences
  to align common metrics and flag metrics present in only one session.
- Render one row per aligned pair, one row per unmatched metric.

Output is deterministic given identical input pairs (both inputs are
sorted before matching, so output order is stable regardless of dict
insertion order in the store).
"""

from __future__ import annotations

import difflib
import io
import sys
from pathlib import Path
from typing import Any

import rich.console
import rich.table

from codevigil.analysis.store import AmbiguousSessionError, SessionReport, SessionStore
from codevigil.history.filters import (
    format_duration,
    format_started_at,
    severity_of_report,
)


def run_diff(
    session_id_a: str,
    session_id_b: str,
    *,
    store_dir: Path | None = None,
    out: Any = None,
) -> int:
    """Render a side-by-side diff of two sessions.

    Parameters:
        session_id_a: Session id for the left (A) column.
        session_id_b: Session id for the right (B) column.
        store_dir: Override the default ``SessionStore`` directory.
        out: Output stream. Defaults to ``sys.stdout``.

    Returns:
        ``0`` on success, ``1`` when either session is not found.
    """
    if out is None:
        out = sys.stdout

    store = SessionStore(base_dir=store_dir)
    try:
        report_a = store.get_report(session_id_a)
        report_b = store.get_report(session_id_b)
    except AmbiguousSessionError as exc:
        out.write(f"{exc}\n")
        return 1

    missing: list[str] = []
    if report_a is None:
        missing.append(session_id_a)
    if report_b is None:
        missing.append(session_id_b)
    if missing:
        for sid in missing:
            out.write(f"session not found: {sid!r}\n")
        return 1

    console = rich.console.Console(file=out, highlight=False)
    _render_diff_to_console(report_a, report_b, console=console)  # type: ignore[arg-type]
    return 0


def _render_diff(a: SessionReport, b: SessionReport) -> str:
    """Return the diff as a plain-text string (used by unit tests)."""
    buf = io.StringIO()
    console = rich.console.Console(file=buf, force_terminal=False, highlight=False)
    _render_diff_to_console(a, b, console=console)
    return buf.getvalue()


def _render_diff_to_console(
    a: SessionReport, b: SessionReport, *, console: rich.console.Console
) -> None:
    console.print(_build_header_table(a, b))
    console.print(_build_metric_table(a, b))


def _build_header_table(a: SessionReport, b: SessionReport) -> rich.table.Table:
    """Build the header comparison table for session metadata fields."""
    tbl = rich.table.Table(title="Session Diff — Header", show_header=True)
    tbl.add_column("field", style="bold")
    tbl.add_column("session A")
    tbl.add_column("session B")

    dur_a = a.duration_seconds
    dur_b = b.duration_seconds
    delta_dur = dur_b - dur_a
    dur_sign = "+" if delta_dur >= 0 else ""

    rows: list[tuple[str, str, str]] = [
        ("session_id", a.session_id, b.session_id),
        (
            "project",
            a.project_name or a.project_hash or "—",
            b.project_name or b.project_hash or "—",
        ),
        ("model", a.model or "—", b.model or "—"),
        ("permission_mode", a.permission_mode or "—", b.permission_mode or "—"),
        ("started_at", format_started_at(a.started_at), format_started_at(b.started_at)),
        (
            "duration",
            format_duration(dur_a),
            f"{format_duration(dur_b)} ({dur_sign}{delta_dur:.0f}s)",
        ),
        ("events", str(a.event_count), str(b.event_count)),
        ("severity", severity_of_report(a), severity_of_report(b)),
    ]
    for label, val_a, val_b in rows:
        tbl.add_row(label, val_a, val_b)
    return tbl


def _build_metric_table(a: SessionReport, b: SessionReport) -> rich.table.Table:
    """Build the metric diff table aligned via LCS over sorted metric names."""
    tbl = rich.table.Table(title="Metric Diff", show_header=True)
    tbl.add_column("metric", style="bold")
    tbl.add_column("value A", justify="right")
    tbl.add_column("value B", justify="right")
    tbl.add_column("delta (B-A)", justify="right")

    metrics_a = a.metrics
    metrics_b = b.metrics
    keys_a = sorted(metrics_a)
    keys_b = sorted(metrics_b)

    matcher = difflib.SequenceMatcher(None, keys_a, keys_b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for ka, kb in zip(keys_a[i1:i2], keys_b[j1:j2], strict=True):
                va = metrics_a[ka]
                vb = metrics_b[kb]
                delta = vb - va
                dsign = "+" if delta >= 0 else ""
                tbl.add_row(ka, f"{va:.4f}", f"{vb:.4f}", f"{dsign}{delta:.4f}")
        elif tag in ("replace", "delete"):
            for k in keys_a[i1:i2]:
                tbl.add_row(k, f"{metrics_a[k]:.4f}", "(absent)", "—")
        if tag in ("replace", "insert"):
            for k in keys_b[j1:j2]:
                tbl.add_row(k, "(absent)", f"{metrics_b[k]:.4f}", "—")

    return tbl


__all__ = ["run_diff"]
