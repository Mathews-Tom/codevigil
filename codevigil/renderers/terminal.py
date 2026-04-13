"""Terminal renderer — rich-based full-redraw watch-mode output.

The ``Renderer`` protocol exposes ``render``, ``render_error``, and
``close``. For the 1 Hz tick-based model we extend with ``begin_tick()``
and ``end_tick()``.

``render()`` buffers a per-session block; ``end_tick()`` flushes all
buffered blocks in a single ``Console.print(Group(*renderables))`` call,
preceded by a TTY-only clear-screen so each tick replaces the previous one.

Severity sort: ``(severity_rank, -updated_at, session_id)`` — CRITICAL
sessions always appear first.

Unique session labels: adaptively extend the hex-prefix length until all
labels in the current tick are distinct.

Mini-trends: inline arrow + last-3 values, e.g. ``[↗3.2→4.1→5.2]``.

Percentile anchors: ``[p92 of your baseline]`` when the store is loaded;
``[n/a]`` when the store is empty or persistence is disabled.
"""

from __future__ import annotations

import contextlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, TextIO

import rich.console
import rich.rule
import rich.table
import rich.text

from codevigil.errors import CodevigilError, ErrorLevel
from codevigil.types import MetricSnapshot, SessionMeta, SessionState, Severity

if TYPE_CHECKING:
    from codevigil.analysis.store import SessionStore

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

_OK_STYLE = "green"
_WARN_STYLE = "yellow"
_CRITICAL_STYLE = "red"
_DIM_STYLE = "dim"
_BOLD_STYLE = "bold"

_SEVERITY_WORD: dict[Severity, str] = {
    Severity.OK: "OK",
    Severity.WARN: "WARN",
    Severity.CRITICAL: "CRIT",
}

_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.OK: _OK_STYLE,
    Severity.WARN: _WARN_STYLE,
    Severity.CRITICAL: _CRITICAL_STYLE,
}

# Numeric rank — CRITICAL sorts first (lowest rank).
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

_STATE_STYLE: dict[SessionState, str] = {
    SessionState.ACTIVE: _OK_STYLE,
    SessionState.STALE: _DIM_STYLE,
    SessionState.EVICTED: _CRITICAL_STYLE,
}

_PARSE_HEALTH_METRIC: str = "parse_health"

_MIN_PREFIX: int = 8
# Store refresh interval: re-read baseline percentiles every 60 ticks.
_STORE_REFRESH_TICKS: int = 60

# ---------------------------------------------------------------------------
# Session block
# ---------------------------------------------------------------------------


@dataclass
class _SessionBlock:
    """Buffered render output for one session in the current tick."""

    # CRITICAL banners — appear above the session header.
    banner_items: list[rich.text.Text] = field(default_factory=list)
    # Session header line and metric table built during render().
    session_header: rich.text.Text | None = None
    metric_table: rich.table.Table | None = None
    # WARN/ERROR footers — appear below the metric table.
    footer_items: list[rich.text.Text] = field(default_factory=list)

    severity_rank: int = _SEVERITY_RANK[Severity.OK]
    updated_at_ts: float = 0.0
    session_id: str = ""
    updated_dt: datetime | None = None
    project_key: str = ""


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TerminalRenderer:
    """Rich-based full-redraw renderer for ``codevigil watch``."""

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
        self._use_color = use_color
        self._show_experimental_badge = show_experimental_badge
        self._console = self._make_console()

        self._blocks: dict[str, _SessionBlock] = {}
        self._order: list[str] = []
        self._parse_confidence: float = 1.0

        self._label_map: dict[str, str] = {}
        self._label_fleet: frozenset[str] = frozenset()

        self._baseline_store: SessionStore | None = baseline_store
        self._baseline: dict[str, list[float]] = {}
        self._ticks_since_store_refresh: int = _STORE_REFRESH_TICKS

        self._fleet_sessions: int = 0
        self._fleet_crit: int = 0
        self._fleet_warn: int = 0
        self._fleet_ok: int = 0
        self._fleet_projects: int = 0
        self._fleet_updated: datetime | None = None

    def _make_console(self) -> rich.console.Console:
        # force_terminal mirrors use_color so the color-mode test emits ANSI
        # even when the stream is a StringIO.
        return rich.console.Console(
            file=self._stream,
            force_terminal=self._use_color,
            highlight=False,
        )

    # ---------------------------------------------------------- tick lifecycle

    def begin_tick(self) -> None:
        """Start a new tick — drop any previously buffered blocks."""
        self._blocks = {}
        self._order = []
        self._ticks_since_store_refresh += 1
        if self._ticks_since_store_refresh >= _STORE_REFRESH_TICKS:
            self._refresh_baseline()
            self._ticks_since_store_refresh = 0

    def end_tick(self) -> None:
        """Flush buffered blocks to the output stream in a single write."""
        # Rebuild label map when fleet composition changes.
        current_ids = frozenset(self._blocks)
        if current_ids != self._label_fleet:
            self._label_map = _build_label_map(list(current_ids))
            self._label_fleet = current_ids

        # Compute fleet-level counters.
        projects: set[str] = set()
        crit = warn = ok = 0
        latest_ts: float = 0.0
        latest_dt: datetime | None = None

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

        sorted_ids = sorted(
            self._order,
            key=lambda sid: (
                self._blocks[sid].severity_rank,
                -self._blocks[sid].updated_at_ts,
                sid,
            ),
        )

        # Rebuild Console from stream each tick so external stream swaps
        # in tests are respected (self._stream is always the current target).
        self._console = self._make_console()

        if self._use_color and self._stream_is_tty():
            self._stream.write("\x1b[2J\x1b[H")
            self._stream.flush()

        renderables: list[Any] = [self._header_text()]
        for session_id in sorted_ids:
            block = self._blocks[session_id]
            renderables.extend(block.banner_items)
            if block.session_header is not None:
                renderables.append(block.session_header)
            renderables.append(rich.rule.Rule(style="dim"))
            if block.metric_table is not None:
                renderables.append(block.metric_table)
            renderables.append(rich.rule.Rule(style="dim"))
            renderables.extend(block.footer_items)

        self._console.print(rich.console.Group(*renderables))
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

        ok_rank = _SEVERITY_RANK[Severity.OK]
        block.severity_rank = min(
            (_SEVERITY_RANK.get(s.severity, ok_rank) for s in snapshots),
            default=ok_rank,
        )
        block.updated_at_ts = meta.last_event_time.timestamp()
        block.updated_dt = meta.last_event_time
        block.project_key = meta.project_name or meta.project_hash[:8]

        self._parse_confidence = next(
            (s.value for s in snapshots if s.name == _PARSE_HEALTH_METRIC),
            meta.parse_confidence,
        )

        block.session_header = self._session_header_text(meta)
        block.metric_table = self._build_metric_table(snapshots, meta)

    def render_error(self, err: CodevigilError, meta: SessionMeta | None) -> None:
        """Route errors by level per design.md §Error Taxonomy.

        INFO → silent. WARN → dim footer. ERROR → bold footer.
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

        text_content = f"{err.code}: {err.message}"
        if err.level is ErrorLevel.WARN:
            block.footer_items.append(rich.text.Text(f"  ! {text_content}", style="dim"))
        elif err.level is ErrorLevel.ERROR:
            block.footer_items.append(rich.text.Text(f"  !! {text_content}", style="bold yellow"))
        elif err.level is ErrorLevel.CRITICAL:
            block.banner_items.append(
                rich.text.Text(f"!!! CRITICAL {text_content}", style="bold red")
            )

    def close(self) -> None:
        with contextlib.suppress(ValueError):
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

    def _header_text(self) -> rich.text.Text:
        """Build the top-line fleet summary as a rich Text object."""
        t = rich.text.Text()
        t.append("codevigil", style=_BOLD_STYLE)
        if self._show_experimental_badge:
            t.append(" [experimental thresholds]", style=f"{_DIM_STYLE} {_WARN_STYLE}")
        ts = self._fleet_updated.isoformat(timespec="seconds") if self._fleet_updated else "—"
        t.append(
            f" | sessions={self._fleet_sessions}"
            f" crit={self._fleet_crit}"
            f" warn={self._fleet_warn}"
            f" ok={self._fleet_ok}"
            f" projects={self._fleet_projects}"
            f" updated={ts}"
            f" | parse_confidence: {self._parse_confidence:.2f}"
        )
        return t

    def _session_header_text(self, meta: SessionMeta) -> rich.text.Text:
        label = self._label_map.get(meta.session_id, meta.session_id[:_MIN_PREFIX])
        project = meta.project_name or meta.project_hash[:8]
        duration = _format_duration((meta.last_event_time - meta.start_time).total_seconds())
        state_word = _STATE_WORD[meta.state]
        state_style = _STATE_STYLE[meta.state]

        t = rich.text.Text()
        t.append(f"session: {label} | project: {project} | {duration} ")
        t.append(state_word, style=state_style)
        return t

    def _build_metric_table(
        self, snapshots: list[MetricSnapshot], meta: SessionMeta
    ) -> rich.table.Table:
        tbl = rich.table.Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            show_edge=False,
        )
        tbl.add_column(min_width=20, no_wrap=True)  # metric name (padded)
        tbl.add_column(justify="right", style="dim")  # value
        tbl.add_column(justify="center")  # severity (styled)
        tbl.add_column(style="dim")  # annotations

        for snap in snapshots:
            sev_style = _SEVERITY_STYLE[snap.severity]
            sev_word = _SEVERITY_WORD[snap.severity]
            sev_text = rich.text.Text(sev_word, style=sev_style)

            ann = rich.text.Text()
            if snap.label:
                ann.append(f"[{snap.label}]")

            history = meta.snapshot_history.get(snap.name)
            if history and len(history) >= 2:
                ann.append(f" {_format_trend(history)}")

            pct_label = self._percentile_label(snap.name, snap.value)
            if pct_label:
                ann.append(f" {pct_label}")

            hint = self._actionable_hint(snap)
            if hint:
                ann.append(f" {hint}")

            tbl.add_row(f"  {snap.name}", f"{snap.value:.1f}", sev_text, ann)

        return tbl

    def _percentile_label(self, metric_name: str, value: float) -> str:
        baseline = self._baseline.get(metric_name)
        if not baseline:
            return "[n/a]"
        n = len(baseline)
        count_le = sum(1 for v in baseline if v <= value)
        pct = round(count_le / n * 100)
        return f"[p{pct} of your baseline]"

    def _refresh_baseline(self) -> None:
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
        detail = snap.detail
        if not detail:
            return ""
        name = snap.name
        if name == "stop_phrase":
            recent = detail.get("recent_hits")
            if not (isinstance(recent, list) and recent):
                return ""
            latest = recent[-1]
            if not isinstance(latest, dict):
                return ""
            phrase = latest.get("phrase")
            if not isinstance(phrase, str):
                return ""
            hint_parts: list[str] = [f"last: {phrase!r}"]
            category = latest.get("category")
            if isinstance(category, str):
                hint_parts.append(f"({category})")
            snippet = latest.get("context_snippet")
            if isinstance(snippet, str) and snippet:
                trunc = snippet[:40].replace("\n", " ")
                hint_parts.append(f"ctx: {trunc!r}")
            return " ".join(hint_parts)
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
    """Build a stable label map with adaptive prefix length."""
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
    result: dict[str, str] = {}
    seen: dict[str, int] = {}
    for sid in sorted(session_ids):
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
