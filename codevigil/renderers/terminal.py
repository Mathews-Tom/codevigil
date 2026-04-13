"""Terminal renderer — ANSI-coloured full-redraw watch-mode output.

The ``Renderer`` protocol (``codevigil.types.Renderer``) exposes only
``render``, ``render_error``, and ``close``. For the terminal output shape
documented in ``docs/design.md`` §CLI Modes → ``codevigil watch`` we need to
know when a 1 Hz aggregator tick starts and ends: the aggregator iterates
``(meta, snapshots)`` pairs and calls ``render()`` once per session, but the
screen should be cleared exactly once per tick, not once per session.

This module extends the frozen protocol with two optional methods,
``begin_tick()`` and ``end_tick()``, that the CLI layer (next phase) will
call around each tick's render loop. ``render()`` buffers the per-session
block internally; ``end_tick()`` flushes all buffered blocks to the output
stream in a single write, optionally preceded by an ANSI clear-screen
sequence when writing to a TTY with colour enabled.

Coloring uses raw ANSI escapes — no ``rich`` dependency — and is fully
stripped when ``use_color=False`` so tests can capture plain text by
routing through ``io.StringIO``. The clear-screen sequence is only emitted
when the stream is an actual TTY (``stream.isatty()``) *and* ``use_color``
is true; tests therefore receive clean append output even with colour on.

Deliverables implemented in this module:

* Stable severity sort: ``(severity_rank, -updated_at, session_id)``.
* Summary header: ``sessions=N crit=C warn=W ok=O projects=P updated=TS``.
* Unique session labels: adaptively extend the hex-prefix length until all
  labels in the current tick are distinct; label is stable within a session's
  lifetime and only recomputed on fleet-composition change.
* Mini-trends: inline arrow + last-3 values, e.g. ``5.2 [↗3.2→4.1→5.2]``.
* Percentile anchors: ``21.0 [p92 of your baseline]`` when store is loaded;
  ``[n/a]`` when store is empty or persistence is disabled.
* Stop-phrase context snippets: ``context_snippet`` from the detail payload.
"""

from __future__ import annotations

import contextlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, TextIO

from codevigil.errors import CodevigilError, ErrorLevel
from codevigil.types import MetricSnapshot, SessionMeta, SessionState, Severity

if TYPE_CHECKING:
    from codevigil.analysis.store import SessionStore

_OK_COLOR: str = "\x1b[32m"
_WARN_COLOR: str = "\x1b[33m"
_CRITICAL_COLOR: str = "\x1b[31m"
_RESET: str = "\x1b[0m"
_DIM: str = "\x1b[2m"
_BOLD: str = "\x1b[1m"
_CLEAR_SCREEN: str = "\x1b[2J\x1b[H"

_SEVERITY_WORD: dict[Severity, str] = {
    Severity.OK: "OK",
    Severity.WARN: "WARN",
    Severity.CRITICAL: "CRIT",
}

_SEVERITY_COLOR: dict[Severity, str] = {
    Severity.OK: _OK_COLOR,
    Severity.WARN: _WARN_COLOR,
    Severity.CRITICAL: _CRITICAL_COLOR,
}

# Numeric rank for severity sort: CRITICAL sorts first (lowest rank number).
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.WARN: 1,
    Severity.OK: 2,
}

_STATE_WORD: dict[SessionState, str] = {
    SessionState.ACTIVE: "ACTIVE",
    SessionState.STALE: "STALE",
    SessionState.EVICTED: "EVICTED",
}

_STATE_COLOR: dict[SessionState, str] = {
    SessionState.ACTIVE: _OK_COLOR,
    SessionState.STALE: _DIM,
    SessionState.EVICTED: _CRITICAL_COLOR,
}

_PARSE_HEALTH_METRIC: str = "parse_health"
_RULE: str = "─" * 70

# Minimum prefix length for session label disambiguation.
_MIN_PREFIX: int = 8
# Store refresh interval in ticks: re-read baseline percentiles every 60 ticks
# rather than every tick. Prevents O(N) store reads on every 1 Hz tick.
_STORE_REFRESH_TICKS: int = 60


@dataclass
class _SessionBlock:
    """Buffered render output for one session in the current tick."""

    banner_lines: list[str] = field(default_factory=list)  # CRITICAL → above header
    body_lines: list[str] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)  # WARN/ERROR → below body
    # Sort key: (severity_rank, -updated_at_ts, session_id)
    severity_rank: int = _SEVERITY_RANK[Severity.OK]
    updated_at_ts: float = 0.0
    session_id: str = ""
    # Fleet-counter metadata populated by render().
    updated_dt: datetime | None = None
    project_key: str = ""


class TerminalRenderer:
    """ANSI full-redraw renderer for ``codevigil watch``."""

    name: str = "terminal"

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        show_experimental_badge: bool = True,
        use_color: bool = True,
        baseline_store: SessionStore | None = None,
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._show_experimental_badge: bool = show_experimental_badge
        self._use_color: bool = use_color
        self._blocks: dict[str, _SessionBlock] = {}
        self._order: list[str] = []
        self._parse_confidence: float = 1.0
        # Stable label map: maps session_id → display label. Rebuilt only
        # when the set of session IDs changes (fleet-composition change).
        self._label_map: dict[str, str] = {}
        self._label_fleet: frozenset[str] = frozenset()
        # Percentile baseline: metric_name → sorted list of historical values
        # loaded from the store at startup and refreshed every
        # _STORE_REFRESH_TICKS ticks.
        self._baseline_store: SessionStore | None = baseline_store
        self._baseline: dict[str, list[float]] = {}
        self._ticks_since_store_refresh: int = _STORE_REFRESH_TICKS  # load on first tick
        # Fleet-level counters updated each tick by end_tick().
        self._fleet_sessions: int = 0
        self._fleet_crit: int = 0
        self._fleet_warn: int = 0
        self._fleet_ok: int = 0
        self._fleet_projects: int = 0
        self._fleet_updated: datetime | None = None

    # ---------------------------------------------------------- tick lifecycle

    def begin_tick(self) -> None:
        """Start a new tick — drop any previously buffered blocks."""

        self._blocks = {}
        self._order = []
        # Refresh the percentile baseline on a long interval.
        self._ticks_since_store_refresh += 1
        if self._ticks_since_store_refresh >= _STORE_REFRESH_TICKS:
            self._refresh_baseline()
            self._ticks_since_store_refresh = 0

    def end_tick(self) -> None:
        """Flush buffered blocks to the output stream in a single write."""

        # Compute fleet-level counters before sorting.
        projects: set[str] = set()
        crit = warn = ok = 0
        latest_ts: float = 0.0
        latest_dt: datetime | None = None

        # Rebuild the stable label map when fleet composition changes.
        current_ids = frozenset(self._blocks)
        if current_ids != self._label_fleet:
            self._label_map = _build_label_map(list(current_ids))
            self._label_fleet = current_ids

        for block in self._blocks.values():
            if block.severity_rank == _SEVERITY_RANK[Severity.CRITICAL]:
                crit += 1
            elif block.severity_rank == _SEVERITY_RANK[Severity.WARN]:
                warn += 1
            else:
                ok += 1
            if block.updated_at_ts > latest_ts:
                latest_ts = block.updated_at_ts
                latest_dt = block.updated_dt
            if block.project_key:
                projects.add(block.project_key)

        self._fleet_sessions = len(self._blocks)
        self._fleet_crit = crit
        self._fleet_warn = warn
        self._fleet_ok = ok
        self._fleet_projects = len(projects)
        self._fleet_updated = latest_dt

        # Stable sort: (severity_rank ASC, -updated_at_ts DESC, session_id ASC)
        sorted_ids = sorted(
            self._order,
            key=lambda sid: (
                self._blocks[sid].severity_rank,
                -self._blocks[sid].updated_at_ts,
                sid,
            ),
        )

        parts: list[str] = []
        if self._use_color and self._stream_is_tty():
            parts.append(_CLEAR_SCREEN)
        parts.append(self._header_line())
        parts.append("\n")
        for session_id in sorted_ids:
            block = self._blocks[session_id]
            for line in block.banner_lines:
                parts.append(line)
                parts.append("\n")
            for line in block.body_lines:
                parts.append(line)
                parts.append("\n")
            for line in block.footer_lines:
                parts.append(line)
                parts.append("\n")
        self._stream.write("".join(parts))
        self._stream.flush()
        self._blocks = {}
        self._order = []

    # ---------------------------------------------------------------- render

    def render(self, snapshots: list[MetricSnapshot], meta: SessionMeta) -> None:
        """Buffer one session's block for the current tick."""

        block = self._blocks.get(meta.session_id)
        if block is None:
            block = _SessionBlock()
            self._blocks[meta.session_id] = block
            self._order.append(meta.session_id)
        block.session_id = meta.session_id

        # Compute the worst severity across all snapshots.
        worst = _SEVERITY_RANK[Severity.OK]
        for snap in snapshots:
            rank = _SEVERITY_RANK.get(snap.severity, _SEVERITY_RANK[Severity.OK])
            if rank < worst:
                worst = rank
        block.severity_rank = worst
        block.updated_at_ts = meta.last_event_time.timestamp()
        block.updated_dt = meta.last_event_time
        block.project_key = meta.project_name or meta.project_hash[:8]

        block.body_lines.extend(self._session_body(snapshots, meta))

    def render_error(self, err: CodevigilError, meta: SessionMeta | None) -> None:
        """Route errors by level per design.md §Error Taxonomy → Levels and Routes.

        INFO → silent (log file only). WARN → dim footer under the session
        block. ERROR → bright (non-dim) footer under the session block.
        CRITICAL → red banner above the session header.
        """

        if err.level is ErrorLevel.INFO:
            return
        session_id = meta.session_id if meta is not None else ""
        block = self._blocks.get(session_id)
        if block is None:
            block = _SessionBlock()
            self._blocks[session_id] = block
            self._order.append(session_id)
        text = f"{err.code}: {err.message}"
        if err.level is ErrorLevel.WARN:
            block.footer_lines.append(self._paint(f"  ! {text}", _DIM))
        elif err.level is ErrorLevel.ERROR:
            block.footer_lines.append(self._paint(f"  !! {text}", _WARN_COLOR + _BOLD))
        elif err.level is ErrorLevel.CRITICAL:
            block.banner_lines.append(self._paint(f"!!! CRITICAL {text}", _CRITICAL_COLOR + _BOLD))

    def close(self) -> None:
        """Flush the output stream. No persistent handles to release."""

        with contextlib.suppress(ValueError):
            # Stream already closed — nothing to flush.
            self._stream.flush()

    # --------------------------------------------------------------- helpers

    def _stream_is_tty(self) -> bool:
        isatty = getattr(self._stream, "isatty", None)
        if not callable(isatty):
            return False
        try:
            return bool(isatty())
        except ValueError:
            return False

    def _paint(self, text: str, color: str) -> str:
        if not self._use_color:
            return text
        return f"{color}{text}{_RESET}"

    def _header_line(self) -> str:
        """Emit the top-line summary header.

        Format: ``codevigil [experimental thresholds] | sessions=N crit=C
        warn=W ok=O projects=P updated=TS | parse_confidence: X.XX``
        """
        parts: list[str] = [self._paint("codevigil", _BOLD)]
        if self._show_experimental_badge:
            parts.append(self._paint("[experimental thresholds]", _DIM + _WARN_COLOR))
        ts = self._fleet_updated.isoformat(timespec="seconds") if self._fleet_updated else "—"
        summary = (
            f"sessions={self._fleet_sessions} "
            f"crit={self._fleet_crit} "
            f"warn={self._fleet_warn} "
            f"ok={self._fleet_ok} "
            f"projects={self._fleet_projects} "
            f"updated={ts}"
        )
        parts.append(f"| {summary} |")
        parts.append(f"parse_confidence: {self._parse_confidence:.2f}")
        return " ".join(parts)

    def _session_body(self, snapshots: list[MetricSnapshot], meta: SessionMeta) -> list[str]:
        lines: list[str] = []
        # Pick parse_confidence for the header from either meta or a
        # parse_health snapshot if present.
        pc = meta.parse_confidence
        for snap in snapshots:
            if snap.name == _PARSE_HEALTH_METRIC:
                pc = snap.value
                break
        self._parse_confidence = pc
        lines.append(self._session_line(meta))
        lines.append(self._paint(_RULE, _DIM))
        for snap in snapshots:
            lines.append(self._metric_line(snap, meta))
        lines.append(self._paint(_RULE, _DIM))
        return lines

    def _session_line(self, meta: SessionMeta) -> str:
        label = self._label_map.get(meta.session_id, meta.session_id[:_MIN_PREFIX])
        project = meta.project_name or meta.project_hash[:8]
        duration = _format_duration((meta.last_event_time - meta.start_time).total_seconds())
        state_word = _STATE_WORD[meta.state]
        state_colored = self._paint(state_word, _STATE_COLOR[meta.state])
        return f"session: {label} | project: {project} | {duration} {state_colored}"

    def _metric_line(self, snap: MetricSnapshot, meta: SessionMeta) -> str:
        name = f"{snap.name:<18}"
        value_str = f"{snap.value:>6.1f}"
        sev_word = _SEVERITY_WORD[snap.severity]
        sev_colored = self._paint(sev_word, _SEVERITY_COLOR[snap.severity])
        label = f"[{snap.label}]" if snap.label else ""

        # Mini-trend: append [↗v1→v2→v3] when history has ≥2 entries.
        history = meta.snapshot_history.get(snap.name)
        trend_str = ""
        if history and len(history) >= 2:
            trend_str = " " + self._paint(_format_trend(history), _DIM)

        # Percentile anchor: [p92 of your baseline]
        pct_str = ""
        pct_label = self._percentile_label(snap.name, snap.value)
        if pct_label:
            pct_str = " " + self._paint(pct_label, _DIM)

        hint = self._actionable_hint(snap)
        hint_str = ""
        if hint:
            hint_str = " " + self._paint(hint, _DIM)

        return f"  {name} {value_str}   {sev_colored}   {label}{trend_str}{pct_str}{hint_str}"

    def _percentile_label(self, metric_name: str, value: float) -> str:
        """Return ``[pN of your baseline]`` or ``[n/a]`` when no baseline."""
        baseline = self._baseline.get(metric_name)
        if not baseline:
            return "[n/a]"
        # Compute percentile rank: what fraction of baseline values are ≤ value?
        n = len(baseline)
        count_le = sum(1 for v in baseline if v <= value)
        pct = round(count_le / n * 100)
        return f"[p{pct} of your baseline]"

    def _refresh_baseline(self) -> None:
        """Load metric value distributions from the session store (bounded I/O).

        Called at most once per ``_STORE_REFRESH_TICKS`` ticks. When the store
        is None or the directory is empty, sets ``_baseline`` to ``{}`` so
        ``_percentile_label`` returns ``[n/a]`` for every metric. Never raises.
        """
        store = self._baseline_store
        if store is None:
            self._baseline = {}
            return
        try:
            reports = store.list_reports()
        except Exception:
            self._baseline = {}
            return
        if not reports:
            self._baseline = {}
            return
        by_metric: dict[str, list[float]] = defaultdict(list)
        for report in reports:
            try:
                metrics: dict[str, float] = report.metrics
            except Exception:
                continue
            for name, val in metrics.items():
                by_metric[name].append(val)
        self._baseline = {name: sorted(vals) for name, vals in by_metric.items()}

    def _actionable_hint(self, snap: MetricSnapshot) -> str:
        """Build a one-line drill-down hint appended after the label.

        A bare severity badge is not actionable — "CRIT reasoning_loop"
        tells the user something is wrong but not what threshold was
        crossed or what the most recent trigger looked like. This
        helper reads the structured ``detail`` payload every collector
        already emits and turns the relevant fields into a short
        secondary string.
        """

        detail = snap.detail
        if not detail:
            return ""
        name = snap.name
        if name == "stop_phrase":
            recent = detail.get("recent_hits")
            if isinstance(recent, list) and recent:
                latest = recent[-1]
                phrase = latest.get("phrase") if isinstance(latest, dict) else None
                category = latest.get("category") if isinstance(latest, dict) else None
                snippet = latest.get("context_snippet") if isinstance(latest, dict) else None
                if isinstance(phrase, str):
                    hint_parts: list[str] = [f"last: {phrase!r}"]
                    if isinstance(category, str):
                        hint_parts.append(f"({category})")
                    if isinstance(snippet, str) and snippet:
                        # Show up to 40 chars of the context snippet.
                        trunc = snippet[:40].replace("\n", " ")
                        hint_parts.append(f"ctx: {trunc!r}")
                    return " ".join(hint_parts)
            return ""
        if name == "reasoning_loop":
            burst = detail.get("max_burst")
            calls = detail.get("tool_calls")
            if isinstance(burst, int) and isinstance(calls, int):
                return f"burst {burst}, {calls} tool calls"
            return ""
        if name == "read_edit_ratio":
            blind = detail.get("blind_edit_rate")
            if isinstance(blind, dict):
                rate = blind.get("value")
                if isinstance(rate, (int, float)):
                    return f"blind {rate * 100:.0f}%"
            return ""
        if name == "parse_health":
            missing = detail.get("missing_fields")
            if isinstance(missing, dict) and missing:
                top = sorted(missing.items(), key=lambda kv: -kv[1])[:2]
                return "missing " + ", ".join(f"{k}x{v}" for k, v in top)
            return ""
        return ""


# ---------------------------------------------------------------------------
# Session label helpers
# ---------------------------------------------------------------------------


def _build_label_map(session_ids: list[str]) -> dict[str, str]:
    """Build a stable label map with adaptive prefix length.

    Start at ``_MIN_PREFIX`` characters and extend until all labels in the
    batch are unique. If session IDs are fewer than _MIN_PREFIX characters
    long, the full ID is used. The result is stable: the same set of IDs
    always maps to the same labels.
    """
    if not session_ids:
        return {}
    prefix_len = _MIN_PREFIX
    max_len = max(len(sid) for sid in session_ids)
    while prefix_len <= max_len:
        candidate: dict[str, str] = {sid: sid[:prefix_len] for sid in session_ids}
        labels = list(candidate.values())
        if len(labels) == len(set(labels)):
            return candidate
        prefix_len += 1
    # All labels still collide at full length — append a counter to the
    # last few characters to force uniqueness.
    result: dict[str, str] = {}
    seen: dict[str, int] = {}
    for sid in sorted(session_ids):  # sorted for determinism
        base = sid[:max_len]
        count = seen.get(base, 0)
        seen[base] = count + 1
        result[sid] = base if count == 0 else f"{base}~{count}"
    return result


# ---------------------------------------------------------------------------
# Trend helpers
# ---------------------------------------------------------------------------

_TREND_UP: str = "↗"
_TREND_DOWN: str = "↘"
_TREND_FLAT: str = "→"


def _trend_arrow(values: tuple[float, ...]) -> str:
    """Return a single trend arrow based on the last two values."""
    if len(values) < 2:
        return _TREND_FLAT
    delta = values[-1] - values[-2]
    if delta > 0:
        return _TREND_UP
    if delta < 0:
        return _TREND_DOWN
    return _TREND_FLAT


def _format_trend(values: tuple[float, ...]) -> str:
    """Format ``[↗3.2→4.1→5.2]`` from the last-three value tuple."""
    arrow = _trend_arrow(values)
    body = "→".join(f"{v:.1f}" for v in values)
    return f"[{arrow}{body}]"


# ---------------------------------------------------------------------------
# Duration helper
# ---------------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


__all__ = ["TerminalRenderer", "_build_label_map", "_format_duration", "_format_trend"]
