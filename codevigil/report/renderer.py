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

import io
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal

import rich.console
import rich.panel
import rich.text

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
BANNED_CAUSAL_WORDS: frozenset[str] = frozenset(
    {
        "caused",
        "drove",
        "led to",
        "because of",
        "due to",
        "results in",
        "responsible for",
    }
)

# Metric display names for the appendix catalog.
_METRIC_DISPLAY_NAMES: dict[str, str] = {
    "read_edit_ratio": "Read:Edit Ratio",
    "stop_phrase": "Stop Phrase Rate",
    "reasoning_loop": "Reasoning Loop Rate",
    "parse_health": "Parse Health",
    "write_precision": "Write Precision",
    "blind_edit_rate": "Blind Edit Rate",
    "thinking_visible_ratio": "Thinking Visible %",
    "thinking_visible_chars_median": "Thinking Median Chars",
    "thinking_signature_chars_median": "Signature Median Chars",
    "user_turns": "Prompts/Session",
    "research_mutation_ratio": "Research:Mutation",
    "frustration_rate": "Frustration Rate",
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
    "blind_edit_rate": (
        "Fraction of mutation tool calls (Edit/Write) that targeted a file the "
        "session had not previously read or grepped within the blind-edit window. "
        "Per-file tracked, so unrelated mutations between a Read and its follow-up "
        "Edit do not pollute the count. Suppressed when tracking confidence "
        "(fraction of mutations carrying a file_path payload) falls below the "
        "configured floor. Comparable to issue #42796 §A 'edits without prior read'."
    ),
    "thinking_visible_ratio": (
        "Fraction of thinking blocks that arrived with inline text rather than "
        "as a redacted signature-only stub. A value of 1.0 means every thinking "
        "block was visible; 0.0 means every block was redacted. Tracks the "
        "redact-thinking rollout described in issue #42796 §1."
    ),
    "thinking_visible_chars_median": (
        "Median character length of visible (non-redacted) thinking blocks within "
        "a session. Comparable to the issue #42796 §2 'thinking depth decline' "
        "headline figure (~2,200 → ~600 chars). Null when no visible blocks "
        "were observed."
    ),
    "thinking_signature_chars_median": (
        "Median character length of thinking-block signatures across all blocks. "
        "Per issue #42796, signature length correlates with redacted thinking "
        "depth (Pearson r ≈ 0.971), so this serves as a proxy for the depth of "
        "thinking the model performed even when the inline content was redacted."
    ),
    "user_turns": (
        "Count of user-message turns observed in the session. Comparable to the "
        "issue #42796 'prompts per session' figure (35.9 → 27.9)."
    ),
    "research_mutation_ratio": (
        "Ratio of (read + research) tool calls to mutation tool calls within the "
        "rolling window. Higher values mean more upfront investigation relative "
        "to file modification."
    ),
    "frustration_rate": (
        "Rate of user-message frustration phrases per 1,000 user turns. Counts "
        "phrases like 'you're not listening', 'stop', and explicit corrections "
        "issued back to the assistant. Lexicon is configurable via "
        "collectors.frustration.custom_phrases."
    ),
}


# ---------------------------------------------------------------------------
# Public API: multi-period summary (today / 7d / 30d default view)
# ---------------------------------------------------------------------------

# Label display names for the multi-period panels.
_PERIOD_DISPLAY: dict[str, str] = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
}

# Canonical panel order.
_PERIOD_ORDER: tuple[str, ...] = ("today", "7d", "30d")


def render_multi_period(
    reports: Mapping[str, Sequence[SessionReport]],
) -> str:
    """Render three stacked rich panels for the multi-period default view.

    Each key in *reports* maps a period label (e.g. ``"today"``, ``"7d"``,
    ``"30d"``) to a sequence of :class:`~codevigil.analysis.store.SessionReport`
    objects. Any label present in *reports* that is not in the canonical order
    ``("today", "7d", "30d")`` is appended after them in insertion order.

    Panels are rendered top-to-bottom. Empty sequences (no sessions in the
    period) render as a short "no sessions in period" line rather than an
    empty panel, so the user always sees three labelled sections.

    The output is captured from a Rich console into a string and returned.
    The caller is responsible for writing to stdout.

    Parameters:
        reports: Mapping from period label to a sequence of session reports.

    Returns:
        A string with three stacked Rich panels separated by newlines.
    """
    buf = io.StringIO()
    console = rich.console.Console(file=buf, highlight=False)

    # Determine panel order: canonical labels first, then any extras.
    extra_labels = [k for k in reports if k not in _PERIOD_ORDER]
    ordered_labels = [k for k in _PERIOD_ORDER if k in reports] + extra_labels

    for label in ordered_labels:
        period_reports = reports[label]
        display_name = _PERIOD_DISPLAY.get(label, label)
        panel_content = _render_period_panel_content(period_reports)
        panel = rich.panel.Panel(
            panel_content,
            title=f"[bold]{display_name}[/bold]",
            expand=True,
        )
        console.print(panel)

    return buf.getvalue()


def _render_period_panel_content(
    period_reports: Sequence[SessionReport],
) -> rich.text.Text | str:
    """Render the body content for a single period panel.

    Returns a :class:`rich.text.Text` with one line per session showing
    session id, event count, and key metrics. When the sequence is empty,
    returns the sentinel string "no sessions in period".
    """
    if not period_reports:
        return "no sessions in period"

    text = rich.text.Text()
    for i, report in enumerate(period_reports):
        if i > 0:
            text.append("\n")
        # Session id and event count.
        text.append(report.session_id, style="bold cyan")
        text.append(f"  events: {report.event_count}")
        # Surface a few key metrics when available.
        metric_parts: list[str] = []
        for metric_name in ("read_edit_ratio", "stop_phrase", "reasoning_loop", "parse_health"):
            value = report.metrics.get(metric_name)
            if value is not None:
                metric_parts.append(f"{metric_name}: {value:.2f}")
        if metric_parts:
            text.append("  " + "  ".join(metric_parts))
    return text


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

    _render_methodology_header(lines, cohort, reports=filtered, dimension=dimension)
    _render_trend_table(lines, cohort, cfg=effective_cfg)
    _render_methodology_group_by(lines, cohort, reports=filtered, dimension=dimension)
    _render_appendix(lines, cohort=cohort, cfg=effective_cfg)

    return "\n".join(lines) + "\n"


def _render_methodology_header(
    lines: list[str],
    cohort: CohortSlice,
    *,
    reports: list[SessionReport],
    dimension: str,
) -> None:
    """Top-of-report header block: corpus size, date range, schema version.

    Mirrors the issue #42796 methodology lead-in: state the data shape
    up front so the reader knows what they are looking at before the
    table. Strictly descriptive — no causal language, no claims about
    quality.
    """
    date_range = _compute_date_range(reports)
    lines.append(f"# Cohort Trend Report — by {dimension}")
    lines.append("")
    lines.append(
        f"**Corpus:** {len(reports)} session(s) — **Range:** {date_range} — "
        f"**Cells:** {len(cohort.cells)} — **Schema:** v{CURRENT_SCHEMA_VERSION}"
    )
    lines.append("")


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


def _render_trend_table(
    lines: list[str],
    cohort: CohortSlice,
    *,
    cfg: dict[str, Any] | None = None,
) -> None:
    """Render the cohort trend table as a Markdown table.

    For chronological dimensions (``day``/``week``) the formatter
    annotates each cell with ``[Δ±0.05]`` showing the change from the
    prior row's mean for the same metric. Threshold-crossing cells are
    bolded (warn) or asterisked (critical) based on the resolved config.
    """
    if not cohort.cells:
        lines.append("_No data available for the selected period._")
        lines.append("")
        return

    chronological = cohort.dimension in {"day", "week"}
    effective_cfg = cfg if cfg is not None else CONFIG_DEFAULTS
    thresholds = _build_threshold_index(effective_cfg)

    dim_values: list[str] = sorted({c.dimension_value for c in cohort.cells})
    metric_names: list[str] = sorted({c.metric_name for c in cohort.cells})

    cell_index: dict[tuple[str, str], CohortCell] = {
        (c.dimension_value, c.metric_name): c for c in cohort.cells
    }

    header_cols = [cohort.dimension] + [_col_header(m) for m in metric_names]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("| " + " | ".join("---" for _ in header_cols) + " |")

    prior_means: dict[str, float] = {}
    for dim_val in dim_values:
        row: list[str] = [dim_val]
        for metric in metric_names:
            cell = cell_index.get((dim_val, metric))
            prior = prior_means.get(metric) if chronological else None
            row.append(_format_cell(cell, prior_mean=prior, thresholds=thresholds.get(metric)))
            if cell is not None and cell.n >= 5:
                prior_means[metric] = cell.mean
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")


def _col_header(metric_name: str) -> str:
    """Short display name for a metric column header."""
    display = _METRIC_DISPLAY_NAMES.get(metric_name, metric_name)
    return display


def _format_cell(
    cell: CohortCell | None,
    *,
    prior_mean: float | None = None,
    thresholds: tuple[float | None, float | None, _ThresholdDirection] | None = None,
) -> str:
    """Format a cohort cell as ``mean ± stdev (n)`` plus optional Δ and severity.

    Parameters:
        cell: The cohort cell, or ``None`` for a missing intersection.
        prior_mean: The previous chronological row's mean for the same
            metric. When supplied and non-None, an inline ``[Δ±X.XX]``
            annotation is appended.
        thresholds: ``(warn, critical, direction)`` tuple where ``direction``
            is ``"high"`` (cross when value >= threshold) or ``"low"`` (cross
            when value < threshold). Cells crossing warn are bolded,
            crossing critical are bolded with a leading asterisk.

    Cells with ``n < 5`` always render as the guard sentinel and never
    receive delta or threshold decoration — drawing attention to a
    sample-too-small cell would defeat the privacy guard.
    """
    if cell is None:  # pragma: no cover
        return "—"
    try:
        guard_cell(cell.mean, cell.n)
    except CellTooSmall as exc:
        return exc.sentinel
    if cell.n == 1:  # pragma: no cover
        base = f"{cell.mean:.2f} (n=1)"
    else:
        base = f"{cell.mean:.2f} ± {cell.stdev:.2f} (n={cell.n})"

    if prior_mean is not None:
        delta = cell.mean - prior_mean
        sign = "+" if delta >= 0 else ""
        base += f" [Δ{sign}{delta:.2f}]"

    if thresholds is not None:
        warn, critical, direction = thresholds
        severity = _classify_threshold(cell.mean, warn, critical, direction)
        if severity == "critical":
            base = f"**\\*{base}**"
        elif severity == "warn":
            base = f"**{base}**"

    return base


_ThresholdDirection = Literal["high", "low"]
_Severity = Literal["critical", "warn", "ok"]


def _classify_threshold(
    value: float,
    warn: float | None,
    critical: float | None,
    direction: _ThresholdDirection,
) -> _Severity:
    """Return ``"critical"``, ``"warn"``, or ``"ok"`` for a metric value.

    ``direction="high"`` flags values at or above the threshold (used by
    metrics where larger is worse, e.g. reasoning_loop). ``direction="low"``
    flags values strictly below the threshold (used by parse_health where
    smaller is worse).
    """
    if direction == "high":
        if critical is not None and value >= critical:
            return "critical"
        if warn is not None and value >= warn:
            return "warn"
        return "ok"
    # direction == "low"
    if critical is not None and value < critical:
        return "critical"
    if warn is not None and value < warn:
        return "warn"
    return "ok"


# Per-metric threshold orientation. Metrics not in this map receive no
# threshold decoration in the rendered table — their thresholds may exist
# in config but are ambiguous in direction (e.g. read_edit_ratio is a
# 'low is bad' metric in the loader's severity model, but the cohort
# table presents the raw mean which is not directly thresholded).
_THRESHOLD_DIRECTIONS: dict[str, _ThresholdDirection] = {
    "parse_health": "low",
    "reasoning_loop": "high",
    "stop_phrase": "high",
}


def _build_threshold_index(
    cfg: dict[str, Any],
) -> dict[str, tuple[float | None, float | None, _ThresholdDirection]]:
    """Pull warn/critical thresholds out of the resolved config.

    Returns a mapping from metric name to ``(warn, critical, direction)``.
    Only metrics with a registered direction in
    :data:`_THRESHOLD_DIRECTIONS` are included.
    """
    out: dict[str, tuple[float | None, float | None, _ThresholdDirection]] = {}
    collectors_cfg = cfg.get("collectors", {})
    for metric, direction in _THRESHOLD_DIRECTIONS.items():
        section = collectors_cfg.get(metric)
        if not isinstance(section, dict):
            continue
        warn_raw = section.get("warn_threshold")
        crit_raw = section.get("critical_threshold")
        warn = float(warn_raw) if isinstance(warn_raw, (int, float)) else None
        critical = float(crit_raw) if isinstance(crit_raw, (int, float)) else None
        if warn is None and critical is None:
            continue
        out[metric] = (warn, critical, direction)
    return out


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


def render_correlations_section(reports: list[SessionReport]) -> str:
    """Render the experimental correlation appendix as Markdown.

    Calls :func:`~codevigil.analysis.correlations.compute_correlations`
    and emits a sorted table of metric pairs. Always prefixed with the
    experimental disclaimer so the section cannot be quoted out of
    context as a quality claim.
    """
    from codevigil.analysis.correlations import MIN_PAIRS, compute_correlations

    correlations = compute_correlations(reports)
    lines: list[str] = []
    lines.append("### Experimental Correlations")
    lines.append("")
    lines.append(
        "**Experimental — exploratory signal only.** Pearson correlation across "
        "per-session metric columns. Pearson assumes normality; per-session "
        "metric values are heteroscedastic so these numbers are not statistically "
        "calibrated. Pairs with fewer than "
        f"{MIN_PAIRS} joint observations are omitted. A high correlation "
        "coincides with co-movement in the data — it is not causal evidence."
    )
    lines.append("")
    if not correlations:
        lines.append("_No metric pairs met the minimum sample threshold._")
        lines.append("")
        return "\n".join(lines)
    lines.append("| Metric A | Metric B | Pearson r | n |")
    lines.append("| --- | --- | --- | --- |")
    for mc in correlations:
        lines.append(f"| {mc.metric_a} | {mc.metric_b} | {mc.r:+.3f} | {mc.n} |")
    lines.append("")
    return "\n".join(lines)


def render_group_by_csv(cohort: CohortSlice) -> str:
    """Render a cohort slice as CSV.

    Schema: ``dimension_value,metric_name,mean,stdev,n,min,max``. One
    row per cell. ``n<5`` cells are emitted with the raw mean/stdev so
    downstream tooling can apply its own privacy guard if desired —
    suppressing them here would silently drop data the user explicitly
    asked for in machine-readable form.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([cohort.dimension, "metric_name", "mean", "stdev", "n", "min", "max"])
    for cell in cohort.cells:
        writer.writerow(
            [
                cell.dimension_value,
                cell.metric_name,
                f"{cell.mean:.6f}",
                f"{cell.stdev:.6f}",
                cell.n,
                f"{cell.min_value:.6f}",
                f"{cell.max_value:.6f}",
            ]
        )
    return buf.getvalue()


def render_group_by_json(
    cohort: CohortSlice,
    *,
    reports: list[SessionReport] | None = None,
) -> str:
    """Render a cohort slice as a versioned JSON document.

    The JSON shape is intended for downstream notebook consumption:

    .. code-block:: json

        {
          "schema_version": 1,
          "dimension": "week",
          "session_count": 7549,
          "excluded_null_count": 0,
          "date_range": "2026-01-09..2026-04-15",
          "cells": [
            {
              "dimension_value": "2026-W14",
              "metric_name": "thinking_visible_chars_median",
              "mean": 317.61,
              "stdev": 201.65,
              "n": 37,
              "min": 50.0,
              "max": 920.0
            }
          ]
        }
    """
    import json

    payload: dict[str, Any] = {
        "schema_version": 1,
        "dimension": cohort.dimension,
        "session_count": cohort.session_count,
        "excluded_null_count": cohort.excluded_null_count,
        "date_range": _compute_date_range(reports) if reports else None,
        "cells": [
            {
                "dimension_value": c.dimension_value,
                "metric_name": c.metric_name,
                "mean": c.mean,
                "stdev": c.stdev,
                "n": c.n,
                "min": c.min_value,
                "max": c.max_value,
            }
            for c in cohort.cells
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


__all__ = [
    "BANNED_CAUSAL_WORDS",
    "render_compare_periods_report",
    "render_correlations_section",
    "render_group_by_csv",
    "render_group_by_json",
    "render_group_by_report",
    "render_multi_period",
]
