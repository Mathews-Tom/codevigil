"""Filter predicates for the history subcommand.

All filter logic lives here so it can be unit-tested in isolation from
the CLI argument-parsing layer and from disk I/O.

Severity classification maps the final metric dictionary to one of three
severity labels (``ok``, ``warn``, ``crit``) by mirroring the same
thresholds used by the watch-mode collectors.

Two scale directions are used:
- Normal scale (higher = worse): ``stop_phrase``, ``reasoning_loop``.
  crit when ``value >= crit_threshold``, warn when ``value >= warn_threshold``.
- Inverted scale (lower = worse): ``read_edit_ratio``, ``parse_health``.
  crit when ``value < crit_threshold``, warn when ``value < warn_threshold``.

The ``classify_metric_severity`` function accepts a ``thresholds`` dict that
maps metric name to ``(warn_threshold, crit_threshold)`` tuples. Callers that
pass ``None`` get the built-in defaults matching the config defaults in
``codevigil.config``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from codevigil.analysis.store import SessionReport

# Severity labels used by the history filter layer.
SeverityLabel = Literal["ok", "warn", "crit"]

# Default per-metric thresholds (warn, crit) mirroring the built-in config defaults.
# Source: codevigil/config.py CONFIG_DEFAULTS["collectors"]
# read_edit_ratio: warn=4.0, crit=2.0 (inverted: lower is worse)
# stop_phrase: warn=1.0, crit=3.0 (normal: higher is worse)
# reasoning_loop: warn=10.0, crit=20.0 (normal: higher is worse)
# parse_health: crit=0.9 (inverted: lower is worse — no separate warn threshold)
_DEFAULT_THRESHOLDS: dict[str, tuple[float, float]] = {
    "read_edit_ratio": (4.0, 2.0),
    "stop_phrase": (1.0, 3.0),
    "reasoning_loop": (10.0, 20.0),
    "parse_health": (0.9, 0.9),  # single threshold; warn==crit is intentional
}

# Metrics where a lower value is worse (inverted scale).
_INVERTED_METRICS: frozenset[str] = frozenset({"read_edit_ratio", "parse_health"})


def classify_metric_severity(
    metric_name: str,
    value: float,
    *,
    thresholds: dict[str, tuple[float, float]] | None = None,
    inverted_metrics: frozenset[str] | None = None,
) -> SeverityLabel:
    """Return the severity label for a single metric value.

    Uses ``thresholds`` if provided, otherwise falls back to
    ``_DEFAULT_THRESHOLDS``. For unknown metrics with no threshold entry,
    always returns ``"ok"``.

    Metrics in ``inverted_metrics`` (default: ``_INVERTED_METRICS``) use
    inverted scale semantics: crit when ``value < crit_threshold``, warn
    when ``value < warn_threshold``. All other metrics use normal scale:
    crit when ``value >= crit_threshold``, warn when ``value >= warn_threshold``.
    """
    effective = thresholds if thresholds is not None else _DEFAULT_THRESHOLDS
    entry = effective.get(metric_name)
    if entry is None:
        return "ok"
    warn_t, crit_t = entry
    inv = inverted_metrics if inverted_metrics is not None else _INVERTED_METRICS
    if metric_name in inv:
        if value < crit_t:
            return "crit"
        if value < warn_t:
            return "warn"
        return "ok"
    if value >= crit_t:
        return "crit"
    if value >= warn_t:
        return "warn"
    return "ok"


def severity_of_report(
    report: SessionReport,
    *,
    thresholds: dict[str, tuple[float, float]] | None = None,
) -> SeverityLabel:
    """Return the worst severity across all metrics in ``report``.

    Returns ``"ok"`` when the report has no metrics.
    """
    worst: SeverityLabel = "ok"
    for name, value in report.metrics.items():
        sev = classify_metric_severity(name, value, thresholds=thresholds)
        if sev == "crit":
            return "crit"
        if sev == "warn":
            worst = "warn"
    return worst


def apply_filters(
    reports: list[SessionReport],
    *,
    project: str | None = None,
    since: date | None = None,
    until: date | None = None,
    severity: SeverityLabel | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    thresholds: dict[str, tuple[float, float]] | None = None,
) -> list[SessionReport]:
    """Return the subset of ``reports`` matching all supplied predicates.

    All predicates are ANDed together. Omitting a predicate (``None``)
    means no filtering on that dimension. Date comparisons are calendar-date
    only (timezone is stripped from ``started_at``).
    """
    result: list[SessionReport] = []
    for report in reports:
        if project is not None and (
            report.project_name != project and report.project_hash != project
        ):
            continue
        if since is not None and report.started_at.date() < since:
            continue
        if until is not None and report.started_at.date() > until:
            continue
        if model is not None and report.model != model:
            continue
        if permission_mode is not None and report.permission_mode != permission_mode:
            continue
        if severity is not None and severity_of_report(report, thresholds=thresholds) != severity:
            continue
        result.append(report)
    return result


def parse_date_arg(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` string to a :class:`date`.

    Raises :exc:`ValueError` on invalid format so the CLI can emit a clear
    error message.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def short_id(session_id: str, length: int = 12) -> str:
    """Return a display-safe truncated session id.

    Trims the ``agent-`` prefix common in Claude session ids and returns
    at most ``length`` characters of the remainder, or the full id if it
    is shorter.
    """
    trimmed = session_id.removeprefix("agent-")
    return trimmed[:length]


def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string.

    Examples:
        0 → ``"0s"``
        90 → ``"1m30s"``
        3661 → ``"1h01m"``
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def top_metrics_summary(metrics: dict[str, float], n: int = 2) -> str:
    """Return a compact summary of the top ``n`` metrics by absolute value.

    Formats as ``"metric=val, metric=val"`` for the list table's
    ``metrics_summary`` column.
    """
    if not metrics:
        return "—"
    ranked = sorted(metrics.items(), key=lambda kv: abs(kv[1]), reverse=True)[:n]
    return ", ".join(f"{k}={v:.2f}" for k, v in ranked)


def format_started_at(dt: datetime) -> str:
    """Format ``started_at`` as ``YYYY-MM-DD HH:MM`` (UTC-ish, no seconds)."""
    return dt.strftime("%Y-%m-%d %H:%M")


__all__ = [
    "SeverityLabel",
    "apply_filters",
    "classify_metric_severity",
    "format_duration",
    "format_started_at",
    "parse_date_arg",
    "severity_of_report",
    "short_id",
    "top_metrics_summary",
]
