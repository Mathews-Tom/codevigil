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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TextIO

import rich.box
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

# String-keyed rank lookup used when ranks must be rebuilt from the
# processed-store severity column (which is a lowercase string, not a
# ``Severity`` enum).
_SEVERITY_RANK_BY_STRING: dict[str, int] = {
    Severity.CRITICAL.value: _SEVERITY_RANK[Severity.CRITICAL],
    Severity.WARN.value: _SEVERITY_RANK[Severity.WARN],
    Severity.OK.value: _SEVERITY_RANK[Severity.OK],
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

# Human-readable column headers for the project-row view. Keys are the
# internal metric names emitted by each collector; values are the
# spaced, capitalised labels shown in the TUI. Unknown metric names
# fall through to a title-cased + underscore-replaced default.
_METRIC_DISPLAY_NAMES: dict[str, str] = {
    "parse_health": "Parse Health",
    "read_edit_ratio": "Read/Edit",
    "reasoning_loop": "Reasoning Loop",
    "stop_phrase": "Stop Phrases",
}


def _metric_display_name(raw: str) -> str:
    if raw in _METRIC_DISPLAY_NAMES:
        return _METRIC_DISPLAY_NAMES[raw]
    return raw.replace("_", " ").title()


_MIN_PREFIX: int = 8
# Store refresh interval: re-read baseline percentiles every 60 ticks.
_STORE_REFRESH_TICKS: int = 60

# ---------------------------------------------------------------------------
# Session block
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _MetricRollup:
    """One project-row metric cell after roll-up across sessions."""

    value: float
    severity_rank: int


@dataclass
class _ProjectAggregate:
    """Buffered project-row state accumulated across session blocks."""

    project_key: str
    sessions: int = 0
    severity_rank: int = _SEVERITY_RANK[Severity.OK]
    updated_at_ts: float = 0.0
    updated_dt: datetime | None = None
    metrics: dict[str, _MetricRollup] = field(default_factory=dict)


def _severity_label(rank: int) -> str:
    if rank == _SEVERITY_RANK[Severity.CRITICAL]:
        return _SEVERITY_WORD[Severity.CRITICAL]
    if rank == _SEVERITY_RANK[Severity.WARN]:
        return _SEVERITY_WORD[Severity.WARN]
    return _SEVERITY_WORD[Severity.OK]


def _severity_style_for_rank(rank: int) -> str:
    if rank == _SEVERITY_RANK[Severity.CRITICAL]:
        return _CRITICAL_STYLE
    if rank == _SEVERITY_RANK[Severity.WARN]:
        return _WARN_STYLE
    return _OK_STYLE


def _format_short_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


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
    # Raw snapshots retained so the project-row view can roll up
    # per-metric values across multiple sessions in the same project
    # without re-parsing the metric tables.
    snapshots: list[MetricSnapshot] = field(default_factory=list)


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
        display_limit: int = 20,
        display_mode: str = "session",
        display_project_limit: int = 10,
        store_project_reader: Callable[[int], list[Any]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._use_color = use_color
        self._show_experimental_badge = show_experimental_badge
        self._display_limit: int = display_limit
        self._display_mode: str = display_mode
        self._display_project_limit: int = max(1, int(display_project_limit))
        self._store_project_reader: Callable[[int], list[Any]] | None = store_project_reader
        self._clock: Callable[[], datetime] = clock
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
        # Alternate-screen buffer state. When watching in an interactive
        # TTY the renderer switches to the terminal's alt screen so the
        # full-redraw clears do not clobber the user's shell scrollback;
        # on ``close`` the alt screen is exited and the original shell
        # content (including the command that launched ``codevigil
        # watch``) is restored intact.
        self._alt_screen_entered: bool = False

    def _make_console(self) -> rich.console.Console:
        # force_terminal mirrors use_color so the color-mode test emits ANSI
        # even when the stream is a StringIO. Tests feed StringIO streams
        # that report zero width; pin ``width`` wide enough so the project
        # table (~140 cols with all columns) renders without soft-wrap.
        width: int | None = None
        if not self._stream_is_tty():
            width = 200
        return rich.console.Console(
            file=self._stream,
            force_terminal=self._use_color,
            highlight=False,
            width=width,
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

        self._update_fleet_counters()

        sorted_ids = sorted(
            self._order,
            key=lambda sid: (
                self._blocks[sid].severity_rank,
                -self._blocks[sid].updated_at_ts,
                sid,
            ),
        )

        # Cap the rendered set to display_limit; track totals for the footer.
        total_active = len(sorted_ids)
        sorted_ids = sorted_ids[: self._display_limit]
        shown = len(sorted_ids)

        # Wall-clock render time — set unconditionally so the header ticks
        # forward every frame even when no events arrived.
        self._fleet_updated = self._clock()

        # Rebuild Console from stream each tick so external stream swaps
        # in tests are respected (self._stream is always the current target).
        self._console = self._make_console()

        if self._use_color and self._stream_is_tty():
            # Enter the alternate screen once, on the first live frame.
            # DECSET 1049 preserves the shell's main-screen content and
            # cursor position so the caller's terminal returns exactly as
            # it was when ``close()`` emits the matching reset.
            if not self._alt_screen_entered:
                self._stream.write("\x1b[?1049h")
                self._alt_screen_entered = True
            self._stream.write("\x1b[2J\x1b[H")
            self._stream.flush()

        renderables: list[Any] = [self._header_text()]

        if self._display_mode == "project":
            project_renderables, total_projects, shown_projects = self._build_project_rows()
            renderables.extend(project_renderables)
            if total_projects == 0 and total_active == 0:
                renderables.append(self._no_active_sessions_text())
            if total_projects > shown_projects:
                renderables.append(
                    self._truncation_text(
                        shown=shown_projects,
                        total=total_projects,
                        noun="active projects",
                        hint=(
                            "Increase watch.display_project_limit to see more,"
                            " or pass --by-session for per-session rows."
                        ),
                    )
                )
        else:
            for session_id in sorted_ids:
                renderables.extend(self._block_renderables(self._blocks[session_id]))

            if total_active == 0:
                renderables.append(self._no_active_sessions_text())

            if total_active > shown:
                renderables.append(
                    self._truncation_text(
                        shown=shown,
                        total=total_active,
                        noun="active sessions",
                        hint="Increase watch.display_limit to see more.",
                    )
                )

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

        block.snapshots = list(snapshots)
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
            if self._alt_screen_entered:
                # DECRST 1049: leave the alternate screen and restore the
                # pre-watch shell content. Paired exactly with the DECSET
                # emitted on the first end_tick().
                self._stream.write("\x1b[?1049l")
                self._alt_screen_entered = False
            self._stream.flush()

    # --------------------------------------------------------------- helpers

    def _update_fleet_counters(self) -> None:
        """Recompute fleet-level severity counts and project set from buffered blocks."""
        projects: set[str] = set()
        crit = warn = ok = 0
        crit_rank = _SEVERITY_RANK[Severity.CRITICAL]
        warn_rank = _SEVERITY_RANK[Severity.WARN]
        for block in self._blocks.values():
            if block.severity_rank == crit_rank:
                crit += 1
            elif block.severity_rank == warn_rank:
                warn += 1
            else:
                ok += 1
            if block.project_key:
                projects.add(block.project_key)
        self._fleet_sessions = len(self._blocks)
        self._fleet_crit = crit
        self._fleet_warn = warn
        self._fleet_ok = ok
        self._fleet_projects = len(projects)

    def _build_project_rows(self) -> tuple[list[Any], int, int]:
        """Aggregate buffered session blocks by project and render one row
        per project as a single compact rich Table.

        Live in-memory session blocks are aggregated first; when a
        ``store_project_reader`` callable is configured, top-N recent
        projects from the persistent memory (the processed-session
        store) are then merged in as a retrospective overlay so users
        see their top-N most recent projects even when every live
        session was evicted by the cold-start lifecycle pass. Live
        aggregates take precedence for overlapping project keys.

        Returns ``(renderables, total_project_count, shown_project_count)``
        so the caller can append a truncation footer when the number of
        active projects exceeds ``display_project_limit``.
        """

        projects = self._live_project_aggregates()
        self._overlay_store_projects(projects)

        if not projects:
            return ([], 0, 0)

        ordered = sorted(
            projects.values(),
            key=lambda p: (p.severity_rank, -p.updated_at_ts, p.project_key),
        )
        total_projects = len(ordered)
        shown = ordered[: self._display_project_limit]

        metric_names: list[str] = []
        seen: set[str] = set()
        for agg in shown:
            for name in agg.metrics:
                if name not in seen:
                    seen.add(name)
                    metric_names.append(name)

        table = rich.table.Table(
            title="Active Projects",
            title_style="bold cyan",
            show_header=True,
            header_style="bold white on grey19",
            box=rich.box.ROUNDED,
            border_style="grey37",
            row_styles=["", "on grey11"],
            expand=False,
            pad_edge=False,
            collapse_padding=False,
        )
        table.add_column("Project", style="bold", no_wrap=True, min_width=14)
        table.add_column("Sessions", justify="right", no_wrap=True)
        table.add_column("Status", justify="center", no_wrap=True, min_width=6)
        for name in metric_names:
            table.add_column(_metric_display_name(name), justify="right", no_wrap=True)
        table.add_column("Last Active", justify="right", no_wrap=True)

        now = self._clock()
        for agg in shown:
            state_word = _severity_label(agg.severity_rank)
            state_style = _severity_style_for_rank(agg.severity_rank)
            row: list[Any] = [
                rich.text.Text(agg.project_key, style=f"bold {state_style}"),
                rich.text.Text(str(agg.sessions), style="white"),
                rich.text.Text(f"{state_word:^6}", style=f"bold {state_style}"),
            ]
            for name in metric_names:
                rollup = agg.metrics.get(name)
                if rollup is None:
                    row.append(rich.text.Text("—", style=_DIM_STYLE))
                else:
                    metric_style = _severity_style_for_rank(rollup.severity_rank)
                    row.append(rich.text.Text(f"{rollup.value:.2f}", style=metric_style))
            if agg.updated_dt is not None:
                delta = (now - agg.updated_dt).total_seconds()
                row.append(rich.text.Text(_format_short_duration(delta), style=_DIM_STYLE))
            else:
                row.append(rich.text.Text("—", style=_DIM_STYLE))
            table.add_row(*row)

        return ([table], total_projects, len(shown))

    def _live_project_aggregates(self) -> dict[str, _ProjectAggregate]:
        projects: dict[str, _ProjectAggregate] = {}
        for block in self._blocks.values():
            key = block.project_key or "(unknown)"
            agg = projects.get(key)
            if agg is None:
                agg = _ProjectAggregate(project_key=key)
                projects[key] = agg
            agg.sessions += 1
            agg.severity_rank = min(agg.severity_rank, block.severity_rank)
            if block.updated_at_ts > agg.updated_at_ts:
                agg.updated_at_ts = block.updated_at_ts
                agg.updated_dt = block.updated_dt
            self._merge_live_project_metrics(agg, block.snapshots)
        return projects

    def _merge_live_project_metrics(
        self,
        agg: _ProjectAggregate,
        snapshots: list[MetricSnapshot],
    ) -> None:
        for snap in snapshots:
            slot = agg.metrics.get(snap.name)
            severity_rank = _SEVERITY_RANK.get(snap.severity, _SEVERITY_RANK[Severity.OK])
            if slot is None:
                agg.metrics[snap.name] = _MetricRollup(
                    value=float(snap.value),
                    severity_rank=severity_rank,
                )
                continue
            if severity_rank < slot.severity_rank:
                slot.severity_rank = severity_rank
                slot.value = float(snap.value)

    def _overlay_store_projects(self, projects: dict[str, _ProjectAggregate]) -> None:
        if self._store_project_reader is None:
            return
        try:
            store_aggregates = self._store_project_reader(self._display_project_limit)
        except (OSError, RuntimeError):
            return
        for stored in store_aggregates:
            key = str(getattr(stored, "project_key", "") or "(unknown)")
            if key in projects:
                continue
            projects[key] = self._stored_project_aggregate(key, stored)

    def _stored_project_aggregate(self, key: str, stored: Any) -> _ProjectAggregate:
        agg = _ProjectAggregate(project_key=key)
        agg.sessions = int(getattr(stored, "session_count", 0) or 0)
        stored_dt = getattr(stored, "last_event_time", None)
        if isinstance(stored_dt, datetime):
            agg.updated_at_ts = stored_dt.timestamp()
            agg.updated_dt = stored_dt
        worst_rank = _SEVERITY_RANK[Severity.OK]
        for metric in getattr(stored, "metrics", []) or []:
            name = str(getattr(metric, "metric_name", ""))
            if not name:
                continue
            severity_rank = _SEVERITY_RANK_BY_STRING.get(
                str(getattr(metric, "severity", "ok")),
                _SEVERITY_RANK[Severity.OK],
            )
            agg.metrics[name] = _MetricRollup(
                value=float(getattr(metric, "value", 0.0) or 0.0),
                severity_rank=severity_rank,
            )
            worst_rank = min(worst_rank, severity_rank)
        agg.severity_rank = worst_rank
        return agg

    def _block_renderables(self, block: _SessionBlock) -> list[Any]:
        """Return the ordered list of Rich renderables for one session block."""
        parts: list[Any] = list(block.banner_items)
        if block.session_header is not None:
            parts.append(block.session_header)
        parts.append(rich.rule.Rule(style="dim"))
        if block.metric_table is not None:
            parts.append(block.metric_table)
        parts.append(rich.rule.Rule(style="dim"))
        parts.extend(block.footer_items)
        return parts

    def _no_active_sessions_text(self) -> rich.text.Text:
        return rich.text.Text(
            "no active sessions \u2014 watching for new events"
            " (sessions idle for 35+ minutes are evicted).",
            style=_DIM_STYLE,
        )

    def _truncation_text(self, *, shown: int, total: int, noun: str, hint: str) -> rich.text.Text:
        return rich.text.Text(
            f"\u2026 showing {shown} of {total} {noun}. {hint}",
            style=_DIM_STYLE,
        )

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

        # Append task type tag right-aligned when the classifier is enabled
        # and a task type is available. Suppressed when session_task_type is
        # None (classifier disabled or no turns classified yet).
        if meta.session_task_type is not None:
            badge = " [experimental]" if self._show_experimental_badge else ""
            t.append(
                f"  [task: {meta.session_task_type}]{badge}",
                style=_DIM_STYLE,
            )

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
        if not snap.detail:
            return ""
        if snap.name == "stop_phrase":
            return self._hint_stop_phrase(snap.detail)
        if snap.name == "reasoning_loop":
            return self._hint_reasoning_loop(snap.detail)
        if snap.name == "read_edit_ratio":
            return self._hint_read_edit_ratio(snap.detail)
        if snap.name == "parse_health":
            return self._hint_parse_health(snap.detail)
        return ""

    def _hint_stop_phrase(self, detail: dict[str, Any]) -> str:
        recent = detail.get("recent_hits")
        if not (isinstance(recent, list) and recent):
            return ""
        latest = recent[-1]
        if not isinstance(latest, dict):
            return ""
        phrase = latest.get("phrase")
        if not isinstance(phrase, str):
            return ""
        parts: list[str] = [f"last: {phrase!r}"]
        category = latest.get("category")
        if isinstance(category, str):
            parts.append(f"({category})")
        snippet = latest.get("context_snippet")
        if isinstance(snippet, str) and snippet:
            parts.append(f"ctx: {snippet[:40].replace(chr(10), ' ')!r}")
        return " ".join(parts)

    def _hint_reasoning_loop(self, detail: dict[str, Any]) -> str:
        burst = detail.get("max_burst")
        calls = detail.get("tool_calls")
        if isinstance(burst, int) and isinstance(calls, int):
            return f"burst {burst}, {calls} tool calls"
        return ""

    def _hint_read_edit_ratio(self, detail: dict[str, Any]) -> str:
        blind = detail.get("blind_edit_rate")
        if not isinstance(blind, dict):
            return ""
        rate = blind.get("value")
        if isinstance(rate, (int, float)):
            return f"blind {rate * 100:.0f}%"
        return ""

    def _hint_parse_health(self, detail: dict[str, Any]) -> str:
        missing = detail.get("missing_fields")
        if not (isinstance(missing, dict) and missing):
            return ""
        top = sorted(missing.items(), key=lambda kv: -kv[1])[:2]
        return "missing " + ", ".join(f"{k}x{v}" for k, v in top)


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
