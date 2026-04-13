"""Built-in ``parse_health`` collector — drift detector for the parser.

Reads the shared :class:`~codevigil.parser.ParseStats` instance the parser
updates and computes a CRITICAL severity whenever ``parse_confidence``
drops below 0.9 inside the trailing 50-event window the collector itself
maintains. The collector is registered at import time and marked
un-disableable in ``codevigil.config``.

Wiring rationale: the parser stamps drift counts on a shared
:class:`ParseStats` object, which the collector receives via constructor
injection. The aggregator owns the lifetime of both — when it instantiates
the per-session parser it constructs the collector with the same
``ParseStats`` handle, so ``snapshot()`` can read the live ratio without
either subsystem reaching across module boundaries or going through the
error channel.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from codevigil.collectors import COLLECTORS, register_collector
from codevigil.config import CONFIG_DEFAULTS
from codevigil.parser import ParseStats
from codevigil.types import Event, MetricSnapshot, Severity

_WINDOW_SIZE: int = 50


class ParseHealthCollector:
    """Always-on collector that surfaces parser drift as a metric.

    ``ingest`` records every event into a fixed-length window so the
    collector knows when the parser has accumulated enough data to make a
    drift judgement; ``snapshot()`` reads ``parse_confidence`` off the
    shared :class:`ParseStats` and flags CRITICAL when it dips below the
    configured threshold (default ``0.9``) once the window has filled.
    """

    name: str = "parse_health"
    complexity: str = "O(1)"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        stats: ParseStats | None = None,
    ) -> None:
        cfg = config if config is not None else _default_config()
        self._critical_threshold: float = float(cfg["critical_threshold"])
        self._stats: ParseStats = stats if stats is not None else ParseStats()
        self._window: deque[Event] = deque(maxlen=_WINDOW_SIZE)

    @property
    def stats(self) -> ParseStats:
        return self._stats

    def bind_stats(self, stats: ParseStats) -> None:
        """Replace the shared ParseStats handle (used by the aggregator)."""

        self._stats = stats

    def ingest(self, event: Event) -> None:
        self._window.append(event)

    def snapshot(self) -> MetricSnapshot:
        confidence = self._stats.parse_confidence
        # The window is "full" once the parser has *seen* WINDOW_SIZE lines.
        # Counting raw lines rather than successfully-ingested events is
        # deliberate: a session where 30 % of lines are malformed must
        # still trip CRITICAL even though the deque only holds the
        # successful events.
        window_full = self._stats.total_lines >= _WINDOW_SIZE or len(self._window) >= _WINDOW_SIZE
        is_critical = window_full and confidence < self._critical_threshold
        severity = Severity.CRITICAL if is_critical else Severity.OK
        detail: dict[str, Any] = {
            "window_size": len(self._window),
            "total_lines": self._stats.total_lines,
            "parsed_events": self._stats.parsed_events,
        }
        if is_critical:
            detail["missing_fields"] = dict(self._stats.missing_fields)
        label = "schema drift detected" if is_critical else "parse healthy"
        return MetricSnapshot(
            name=self.name,
            value=float(confidence),
            label=label,
            severity=severity,
            detail=detail,
        )

    def reset(self) -> None:
        self._window.clear()
        self._stats = ParseStats()


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["parse_health"])


register_collector(COLLECTORS, ParseHealthCollector)


__all__ = ["ParseHealthCollector"]
