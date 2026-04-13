"""``codevigil history diff A B`` renderer.

Aligns two sessions by LCS over their event-type sequences (conceptual —
since ``SessionReport`` stores only the final aggregates, not the raw event
stream, alignment is performed over the sorted metric name sequence as a
proxy). Renders a side-by-side Markdown comparison table.

The diff is stdlib-only — ``rich`` adds nothing here and is NOT used.

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
import sys
from pathlib import Path
from typing import Any

from codevigil.analysis.store import SessionReport, SessionStore
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
    report_a = store.get_report(session_id_a)
    report_b = store.get_report(session_id_b)

    missing: list[str] = []
    if report_a is None:
        missing.append(session_id_a)
    if report_b is None:
        missing.append(session_id_b)
    if missing:
        for sid in missing:
            out.write(f"session not found: {sid!r}\n")
        return 1

    out.write(_render_diff(report_a, report_b))  # type: ignore[arg-type]
    return 0


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _render_diff(a: SessionReport, b: SessionReport) -> str:
    lines: list[str] = []

    lines.append("# Session Diff")
    lines.append("")

    # Header comparison table
    lines.append("## Header Comparison")
    lines.append("")
    lines.append("| field | session A | session B |")
    lines.append("| --- | --- | --- |")

    def _row(label: str, val_a: str, val_b: str) -> None:
        lines.append(f"| {label} | {val_a} | {val_b} |")

    _row("session_id", a.session_id, b.session_id)
    _row(
        "project",
        a.project_name or a.project_hash or "—",
        b.project_name or b.project_hash or "—",
    )
    _row("model", a.model or "—", b.model or "—")
    _row("permission_mode", a.permission_mode or "—", b.permission_mode or "—")
    _row("started_at", format_started_at(a.started_at), format_started_at(b.started_at))

    dur_a = a.duration_seconds
    dur_b = b.duration_seconds
    delta_dur = dur_b - dur_a
    sign = "+" if delta_dur >= 0 else ""
    _row(
        "duration",
        format_duration(dur_a),
        f"{format_duration(dur_b)} ({sign}{delta_dur:.0f}s)",
    )

    _row("events", str(a.event_count), str(b.event_count))
    _row("severity", severity_of_report(a), severity_of_report(b))
    lines.append("")

    # Metric diff table aligned via LCS over sorted metric names
    lines.append("## Metric Diff")
    lines.append("")
    lines.append("| metric | value A | value B | delta (B-A) |")
    lines.append("| --- | --- | --- | --- |")

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
                lines.append(f"| {ka} | {va:.4f} | {vb:.4f} | {dsign}{delta:.4f} |")
        elif tag in ("replace",):
            for k in keys_a[i1:i2]:
                lines.append(f"| {k} | {metrics_a[k]:.4f} | _(absent)_ | — |")
            for k in keys_b[j1:j2]:
                lines.append(f"| {k} | _(absent)_ | {metrics_b[k]:.4f} | — |")
        elif tag == "delete":
            for k in keys_a[i1:i2]:
                lines.append(f"| {k} | {metrics_a[k]:.4f} | _(absent)_ | — |")
        elif tag == "insert":
            for k in keys_b[j1:j2]:
                lines.append(f"| {k} | _(absent)_ | {metrics_b[k]:.4f} | — |")

    lines.append("")
    return "\n".join(lines)


__all__ = ["run_diff"]
