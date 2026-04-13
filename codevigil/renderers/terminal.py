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
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from typing import TextIO

from codevigil.errors import CodevigilError, ErrorLevel
from codevigil.types import MetricSnapshot, SessionMeta, SessionState, Severity

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


@dataclass
class _SessionBlock:
    """Buffered render output for one session in the current tick."""

    banner_lines: list[str] = field(default_factory=list)  # CRITICAL → above header
    body_lines: list[str] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)  # WARN/ERROR → below body


class TerminalRenderer:
    """ANSI full-redraw renderer for ``codevigil watch``."""

    name: str = "terminal"

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        show_experimental_badge: bool = True,
        use_color: bool = True,
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._show_experimental_badge: bool = show_experimental_badge
        self._use_color: bool = use_color
        self._blocks: dict[str, _SessionBlock] = {}
        self._order: list[str] = []
        self._parse_confidence: float = 1.0

    # ---------------------------------------------------------- tick lifecycle

    def begin_tick(self) -> None:
        """Start a new tick — drop any previously buffered blocks."""

        self._blocks = {}
        self._order = []

    def end_tick(self) -> None:
        """Flush buffered blocks to the output stream in a single write."""

        parts: list[str] = []
        if self._use_color and self._stream_is_tty():
            parts.append(_CLEAR_SCREEN)
        parts.append(self._header_line())
        parts.append("\n")
        for session_id in self._order:
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
        parts: list[str] = [self._paint("codevigil", _BOLD)]
        if self._show_experimental_badge:
            parts.append(self._paint("[experimental thresholds]", _DIM + _WARN_COLOR))
        parts.append(f"| parse_confidence: {self._parse_confidence:.2f}")
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
            lines.append(self._metric_line(snap))
        lines.append(self._paint(_RULE, _DIM))
        return lines

    def _session_line(self, meta: SessionMeta) -> str:
        sid = meta.session_id[:8]
        project = meta.project_name or meta.project_hash[:8]
        duration = _format_duration((meta.last_event_time - meta.start_time).total_seconds())
        state_word = _STATE_WORD[meta.state]
        state_colored = self._paint(state_word, _STATE_COLOR[meta.state])
        return f"session: {sid} | project: {project} | {duration} {state_colored}"

    def _metric_line(self, snap: MetricSnapshot) -> str:
        name = f"{snap.name:<18}"
        value = f"{snap.value:>6.1f}"
        sev_word = _SEVERITY_WORD[snap.severity]
        sev_colored = self._paint(sev_word, _SEVERITY_COLOR[snap.severity])
        label = f"[{snap.label}]" if snap.label else ""
        hint = self._actionable_hint(snap)
        if hint:
            return f"  {name} {value}   {sev_colored}   {label} {self._paint(hint, _DIM)}"
        return f"  {name} {value}   {sev_colored}   {label}"

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
                if isinstance(phrase, str):
                    if isinstance(category, str):
                        return f"last: {phrase!r} ({category})"
                    return f"last: {phrase!r}"
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


def _format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


__all__ = ["TerminalRenderer"]
