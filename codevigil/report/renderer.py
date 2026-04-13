"""Cohort report Markdown renderer.

Renders three report types on top of the :mod:`codevigil.analysis` substrate:

1. **Group-by trend table** — rows are cohort dimension values (e.g. ISO
   weeks), columns are metric names, cells are ``mean ± stdev (n)`` or the
   sentinel ``n<5`` when the sample-size guard fires.

2. **Period-comparison table** — signed delta table between two date ranges,
   plus a prose one-liner per metric in the form::

       read_edit_ratio: fell from 6.6 to 2.0 over 2026-03-01..2026-03-15;
       n=42, n=39

3. **Methodology section** — source corpus size, date range, group-by
   dimension, redacted-cell count, and mandatory correlation-not-causation
   language including the explicit unvalidated-metrics disclaimer.

4. **Appendix section** — behavioral catalog with per-metric definitions and
   severity thresholds, threshold table from config, schema version, and
   sample-size distribution across cohort cells.

Claim discipline rules enforced here:
- Causal words "caused", "drove", "led to" are BANNED from all rendered
  output. Only "correlates with", "coincides with", "was observed alongside"
  are used.
- Cells with ``n < 5`` are rendered as ``n<5`` (never as headline numbers).
- The methodology section MUST include the phrase
  "metrics have not been validated against labeled outcomes".

This module is a critical-path renderer per test-standards.md and requires
≥ 95% coverage.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from codevigil.analysis.cohort import (
    CohortCell,
    CohortSlice,
    GroupByDimension,
    filter_by_period,
    reduce_by,
)
from codevigil.analysis.compare import ComparisonResult, MetricComparison, compare_periods
from codevigil.analysis.guards import CellTooSmall, guard_cell
from codevigil.analysis.store import CURRENT_SCHEMA_VERSION, SessionReport
from codevigil.config import CONFIG_DEFAULTS

# ---------------------------------------------------------------------------
# Banned causal words — tested by the banned-word guard test.
# These words must NEVER appear in any rendered output from this module.
# ---------------------------------------------------------------------------
BANNED_CAUSAL_WORDS: frozenset[str] = frozenset({"caused", "drove", "led to"})

# Metric display names for the appendix catalog.
_METRIC_DISPLAY_NAMES: dict[str, str] = {
    "read_edit_ratio": "Read:Edit Ratio",
    "stop_phrase": "Stop Phrase Rate",
    "reasoning_loop": "Reasoning Loop Rate",
    "parse_health": "Parse Health",
    "write_precision": "Write Precision",
}

# Per-metric definitions for the appendix behavioral catalog.
_METRIC_DEFINITIONS: dict[str, str] = {
    "read_edit_ratio": (
        "Rolling ratio of read tool calls to mutation tool calls over the last "
        "window_size events. A high ratio indicates more reading relative to writing; "
        "a low ratio indicates heavy mutation with less upfront reading. "
        "Observed alongside session quality in retrospective cohort analyses."
    ),
    "stop_phrase": (
        "Rate of matched stop phrases per 1,000 tool calls. Stop phrases are "
        "assistant messages that indicate scope limitation or deference. "
        "Higher rates coincide with sessions where the assistant declined to act."
    ),
    "reasoning_loop": (
        "Rate of reasoning-loop events per 1,000 tool calls. A reasoning loop "
        "is detected when the assistant repeats a similar tool call sequence. "
        "Elevated rates were observed alongside sessions requiring external "
        "intervention."
    ),
    "parse_health": (
        "Rolling parse-confidence score. Values below the critical threshold "
        "indicate that a significant fraction of lines in the session file could "
        "not be parsed as valid events. Low parse health coincides with schema "
        "drift between the Claude Code version and this collector."
    ),
    "write_precision": (
        "Fraction of mutation tool calls that are wholesale writes rather than "
        "surgical edits: write_calls / (write_calls + edit_calls). A value of 1.0 "
        "means all mutations were full-file writes; 0.0 means all mutations were "
        "surgical edits. Directly comparable to §4 of the target retrospective "
        "analysis. Null when no write or edit tool calls were observed."
    ),
}


# ---------------------------------------------------------------------------
# Public API: group-by report
# ---------------------------------------------------------------------------


def render_group_by_report(
    reports: list[SessionReport],
    *,
    dimension: GroupByDimension,
    since: date | None = None,
    until: date | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Render a Markdown cohort trend report grouped by ``dimension``.

    Parameters:
        reports: Session reports to include. Pre-filtering by date is
            optional; pass ``since`` / ``until`` to filter inside the renderer.
        dimension: One of the five supported group-by dimensions.
        since: Inclusive start date filter applied before reduction.
        until: Inclusive end date filter applied before reduction.
        cfg: Effective config dict. Defaults to built-in defaults.

    Returns:
        A Markdown string with trend table, methodology section, and appendix.
    """
    effective_cfg = cfg if cfg is not None else CONFIG_DEFAULTS
    filtered = filter_by_period(reports, since=since, until=until)
    cohort = reduce_by(filtered, dimension)
    lines: list[str] = []

    _render_trend_table(lines, cohort)
    _render_methodology_group_by(lines, cohort, reports=filtered, dimension=dimension)
    _render_appendix(lines, cohort=cohort, cfg=effective_cfg)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API: compare-periods report
# ---------------------------------------------------------------------------


def render_compare_periods_report(
    reports: list[SessionReport],
    *,
    period_a_since: date,
    period_a_until: date,
    period_b_since: date,
    period_b_until: date,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Render a Markdown period-comparison report.

    Filters ``reports`` into two non-overlapping date ranges, runs
    :func:`~codevigil.analysis.compare.compare_periods`, and emits a signed
    delta table plus a prose one-liner per metric.

    Sample-size guards are applied: any period with fewer than 5 sessions for
    a given metric will not be rendered as a headline number in the one-liner.

    Parameters:
        reports: All available session reports (both periods are filtered from
            this list).
        period_a_since: Inclusive start of period A.
        period_a_until: Inclusive end of period A.
        period_b_since: Inclusive start of period B.
        period_b_until: Inclusive end of period B.
        cfg: Effective config dict. Defaults to built-in defaults.

    Returns:
        A Markdown string with the comparison table, one-liners, methodology
        section, and appendix.
    """
    effective_cfg = cfg if cfg is not None else CONFIG_DEFAULTS
    period_a = filter_by_period(reports, since=period_a_since, until=period_a_until)
    period_b = filter_by_period(reports, since=period_b_since, until=period_b_until)
    result = compare_periods(period_a, period_b)

    lines: list[str] = []
    _render_comparison_table(
        lines,
        result,
        period_a_since=period_a_since,
        period_a_until=period_a_until,
        period_b_since=period_b_since,
        period_b_until=period_b_until,
    )
    _render_comparison_one_liners(
        lines,
        result,
        period_b_since=period_b_since,
        period_b_until=period_b_until,
    )
    _render_methodology_compare(
        lines,
        result,
        reports=reports,
        period_a_since=period_a_since,
        period_a_until=period_a_until,
        period_b_since=period_b_since,
        period_b_until=period_b_until,
    )
    _render_appendix_compare(lines, result=result, cfg=effective_cfg)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Trend table renderer
# ---------------------------------------------------------------------------


def _render_trend_table(lines: list[str], cohort: CohortSlice) -> None:
    """Render the cohort trend table as a Markdown table."""
    lines.append(f"# Cohort Trend Report — by {cohort.dimension}")
    lines.append("")

    if not cohort.cells:
        lines.append("_No data available for the selected period._")
        lines.append("")
        return

    # Collect all dimension values and metric names from cells.
    dim_values: list[str] = sorted({c.dimension_value for c in cohort.cells})
    metric_names: list[str] = sorted({c.metric_name for c in cohort.cells})

    # Index cells by (dimension_value, metric_name) for lookup.
    cell_index: dict[tuple[str, str], CohortCell] = {
        (c.dimension_value, c.metric_name): c for c in cohort.cells
    }

    # Table header.
    header_cols = [cohort.dimension] + [_col_header(m) for m in metric_names]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("| " + " | ".join("---" for _ in header_cols) + " |")

    # One row per dimension value.
    for dim_val in dim_values:
        row: list[str] = [dim_val]
        for metric in metric_names:
            cell = cell_index.get((dim_val, metric))
            row.append(_format_cell(cell))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")


def _col_header(metric_name: str) -> str:
    """Short display name for a metric column header."""
    display = _METRIC_DISPLAY_NAMES.get(metric_name, metric_name)
    return display


def _format_cell(cell: CohortCell | None) -> str:
    """Format a cohort cell as ``mean ± stdev (n)`` or the guard sentinel."""
    if cell is None:  # pragma: no cover
        return "—"
    try:
        guard_cell(cell.mean, cell.n)
    except CellTooSmall as exc:
        return exc.sentinel
    if cell.n == 1:  # pragma: no cover
        # Single observation: no meaningful stdev (guard normally blocks n<5,
        # so this branch fires only when MIN_CELL_N is set to 1 in config).
        return f"{cell.mean:.2f} (n=1)"
    return f"{cell.mean:.2f} ± {cell.stdev:.2f} (n={cell.n})"


# ---------------------------------------------------------------------------
# Comparison table and one-liners
# ---------------------------------------------------------------------------


def _render_comparison_table(
    lines: list[str],
    result: ComparisonResult,
    *,
    period_a_since: date,
    period_a_until: date,
    period_b_since: date,
    period_b_until: date,
) -> None:
    """Render the period-comparison signed delta table."""
    a_label = f"{period_a_since}..{period_a_until}"
    b_label = f"{period_b_since}..{period_b_until}"
    lines.append(f"# Period Comparison: {a_label} vs {b_label}")
    lines.append("")
    lines.append(
        f"Sessions in period A: {result.n_sessions_a} — Sessions in period B: {result.n_sessions_b}"
    )
    lines.append("")

    if not result.metrics:
        lines.append("_No metrics shared between the two periods._")
        lines.append("")
    else:
        lines.append("| Metric | Period A mean | Period B mean | Delta | Δ% | Significant |")
        lines.append("| --- | --- | --- | --- | --- | --- |")

        for mc in result.metrics:
            a_cell = _format_period_mean(mc.mean_a, mc.n_a)
            b_cell = _format_period_mean(mc.mean_b, mc.n_b)
            delta_cell = _format_delta(mc)
            sig_cell = "yes" if mc.significant else "no"
            lines.append(
                f"| {mc.metric_name} | {a_cell} | {b_cell} | "
                f"{delta_cell} | {_format_pct(mc.delta_pct)} | {sig_cell} |"
            )

        lines.append("")

    if result.metrics_only_in_a:
        lines.append(
            f"_Metrics only in period A (not compared): {', '.join(result.metrics_only_in_a)}_"
        )
        lines.append("")
    if result.metrics_only_in_b:
        lines.append(
            f"_Metrics only in period B (not compared): {', '.join(result.metrics_only_in_b)}_"
        )
        lines.append("")


def _format_period_mean(mean: float, n: int) -> str:
    """Format a period mean with sample-size guard."""
    try:
        guard_cell(mean, n)
    except CellTooSmall as exc:
        return exc.sentinel
    return f"{mean:.2f} (n={n})"


def _format_delta(mc: MetricComparison) -> str:
    direction = "+" if mc.delta >= 0 else ""
    return f"{direction}{mc.delta:.2f}"


def _format_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    direction = "+" if pct >= 0 else ""
    return f"{direction}{pct:.1f}%"


def _render_comparison_one_liners(
    lines: list[str],
    result: ComparisonResult,
    *,
    period_b_since: date,
    period_b_until: date,
) -> None:
    """Render one prose summary line per shared metric."""
    if not result.metrics:
        return
    lines.append("## Summary")
    lines.append("")
    window = f"{period_b_since}..{period_b_until}"
    for mc in result.metrics:
        # Skip metrics where either period is below the guard threshold.
        a_guarded = mc.n_a >= 5
        b_guarded = mc.n_b >= 5
        if not a_guarded or not b_guarded:
            lines.append(
                f"- **{mc.metric_name}**: insufficient data in one period "
                f"(n_a={mc.n_a}, n_b={mc.n_b}) — not reported."
            )
            continue
        direction = _direction_word(mc.delta)
        lines.append(
            f"- **{mc.metric_name}**: {direction} from {mc.mean_a:.1f} to {mc.mean_b:.1f} "
            f"over the {window} window; n={mc.n_a}, n={mc.n_b}"
        )
    lines.append("")


def _direction_word(delta: float) -> str:
    """Describe the direction of change without causal language."""
    if delta > 0:
        return "rose"
    if delta < 0:
        return "fell"
    return "held steady"


# ---------------------------------------------------------------------------
# Methodology section (shared between group-by and compare-periods)
# ---------------------------------------------------------------------------


def _render_methodology_group_by(
    lines: list[str],
    cohort: CohortSlice,
    *,
    reports: list[SessionReport],
    dimension: str,
) -> None:
    """Render the ## Methodology section for a group-by report."""
    redacted = _count_redacted_cells(cohort)
    date_range = _compute_date_range(reports)
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        f"Source corpus: {len(reports)} session(s) "
        f"spanning {date_range}. "
        f"Group-by dimension: `{dimension}`. "
        f"Cohort cells: {len(cohort.cells)} total; "
        f"{redacted} redacted as `n<5` per sample-size guard."
    )
    lines.append("")
    _render_methodology_disclaimer(lines)


def _render_methodology_compare(
    lines: list[str],
    result: ComparisonResult,
    *,
    reports: list[SessionReport],
    period_a_since: date,
    period_a_until: date,
    period_b_since: date,
    period_b_until: date,
) -> None:
    """Render the ## Methodology section for a compare-periods report."""
    lines.append("## Methodology")
    lines.append("")
    n_below_guard = sum(1 for mc in result.metrics if mc.n_a < 5 or mc.n_b < 5)
    lines.append(
        f"Source corpus: {len(reports)} session(s) total. "
        f"Period A: {period_a_since}..{period_a_until} "
        f"({result.n_sessions_a} session(s)). "
        f"Period B: {period_b_since}..{period_b_until} "
        f"({result.n_sessions_b} session(s)). "
        f"{n_below_guard} metric(s) had at least one period with n<5 and were "
        f"excluded from headline reporting."
    )
    lines.append("")
    _render_methodology_disclaimer(lines)


def _render_methodology_disclaimer(lines: list[str]) -> None:
    """Append the mandatory claim-discipline disclaimer block.

    This block MUST include the phrase
    "metrics have not been validated against labeled outcomes"
    and MUST NOT use causal language ("caused", "drove", "led to").
    """
    lines.append(
        "**Important:** These metrics have not been validated against labeled "
        "outcomes. This report reflects distributions and trends in "
        "_observed behavior_, not validated quality signals. All relationships "
        "described are correlational — they coincide with or were observed "
        "alongside patterns in session data. No causal inference is made."
    )
    lines.append("")
    lines.append(
        "Cells with fewer than 5 observations are redacted and displayed as `n<5`. "
        "Significance flags are based on Welch's t-test at a=0.05 and should be "
        "interpreted as a signal for further investigation, not as a conclusion."
    )
    lines.append("")


# ---------------------------------------------------------------------------
# Appendix section
# ---------------------------------------------------------------------------


def _render_appendix(
    lines: list[str],
    *,
    cohort: CohortSlice,
    cfg: dict[str, Any],
) -> None:
    """Render the ## Appendix section for a group-by report."""
    lines.append("## Appendix")
    lines.append("")
    metric_names = sorted({c.metric_name for c in cohort.cells})
    _render_behavioral_catalog(lines, metric_names)
    _render_threshold_table(lines, cfg)
    _render_schema_version(lines)
    _render_cell_distribution(lines, cohort.cells)


def _render_appendix_compare(
    lines: list[str],
    *,
    result: ComparisonResult,
    cfg: dict[str, Any],
) -> None:
    """Render the ## Appendix section for a compare-periods report."""
    lines.append("## Appendix")
    lines.append("")
    metric_names = sorted({mc.metric_name for mc in result.metrics})
    _render_behavioral_catalog(lines, metric_names)
    _render_threshold_table(lines, cfg)
    _render_schema_version(lines)
    _render_period_n_distribution(lines, result)


def _render_behavioral_catalog(lines: list[str], metric_names: list[str]) -> None:
    """Render per-metric definitions."""
    lines.append("### Behavioral Catalog")
    lines.append("")
    for metric in metric_names:
        display = _METRIC_DISPLAY_NAMES.get(metric, metric)
        definition = _METRIC_DEFINITIONS.get(metric, f"No definition available for `{metric}`.")
        lines.append(f"**{display}** (`{metric}`)")
        lines.append("")
        lines.append(definition)
        lines.append("")


def _render_threshold_table(lines: list[str], cfg: dict[str, Any]) -> None:
    """Render the threshold table from config."""
    lines.append("### Threshold Table")
    lines.append("")
    lines.append("| Metric | Warn threshold | Critical threshold |")
    lines.append("| --- | --- | --- |")

    collectors_cfg = cfg.get("collectors", {})
    collector_order = ["read_edit_ratio", "stop_phrase", "reasoning_loop", "parse_health"]
    for name in collector_order:
        section = collectors_cfg.get(name)
        if not isinstance(section, dict):
            continue
        warn = section.get("warn_threshold", "—")
        crit = section.get("critical_threshold", "—")
        display = _METRIC_DISPLAY_NAMES.get(name, name)
        lines.append(f"| {display} | {warn} | {crit} |")

    lines.append("")
    lines.append(
        "_Thresholds are sourced from the resolved configuration. "
        "Override via `~/.config/codevigil/config.toml` or `--config`._"
    )
    lines.append("")


def _render_schema_version(lines: list[str]) -> None:
    """Render the schema version line."""
    lines.append(f"Schema version: {CURRENT_SCHEMA_VERSION}")
    lines.append("")


def _render_cell_distribution(lines: list[str], cells: list[CohortCell]) -> None:
    """Render sample-size distribution across cohort cells."""
    lines.append("### Sample-Size Distribution")
    lines.append("")
    if not cells:
        lines.append("_No cohort cells to display._")
        lines.append("")
        return

    # Count cells by n-bucket: n<5, 5-9, 10-24, 25+
    buckets: dict[str, int] = defaultdict(int)
    for cell in cells:
        buckets[_n_bucket(cell.n)] += 1

    lines.append("| n-range | cell count |")
    lines.append("| --- | --- |")
    for bucket in ("n<5", "5-9", "10-24", "25+"):
        count = buckets.get(bucket, 0)
        lines.append(f"| {bucket} | {count} |")
    lines.append("")


def _render_period_n_distribution(lines: list[str], result: ComparisonResult) -> None:
    """Render sample-size distribution for the comparison result."""
    lines.append("### Sample-Size Distribution")
    lines.append("")
    if not result.metrics:
        lines.append("_No shared metrics to display._")
        lines.append("")
        return

    lines.append("| Metric | n (period A) | n (period B) |")
    lines.append("| --- | --- | --- |")
    for mc in result.metrics:
        lines.append(f"| {mc.metric_name} | {mc.n_a} | {mc.n_b} |")
    lines.append("")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_redacted_cells(cohort: CohortSlice) -> int:
    """Count cohort cells that will be redacted as n<5."""
    return sum(1 for c in cohort.cells if c.n < 5)


def _compute_date_range(reports: list[SessionReport]) -> str:
    """Return a human-readable date range string for the corpus."""
    if not reports:
        return "no sessions"
    dates = [r.started_at.date() for r in reports]
    lo = min(dates)
    hi = max(dates)
    if lo == hi:
        return str(lo)
    return f"{lo}..{hi}"


def _n_bucket(n: int) -> str:
    if n < 5:
        return "n<5"
    if n < 10:
        return "5-9"
    if n < 25:
        return "10-24"
    return "25+"


__all__ = [
    "BANNED_CAUSAL_WORDS",
    "render_compare_periods_report",
    "render_group_by_report",
]
