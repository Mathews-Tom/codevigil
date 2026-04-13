"""Reasoning-loop collector.

Counts self-correction phrases in assistant messages and reports the
rate per 1000 observed tool calls. The signal is the assistant talking
itself out of a previous step - when the rate spikes the model is
likely thrashing instead of making forward progress.

Shares the word-boundary matcher implementation with
:mod:`codevigil.collectors.stop_phrase` via
:mod:`codevigil.collectors._text_match`. Patterns use word-boundary
mode by default so ``"actually"`` does not fire on ``"factually"``.
"""

from __future__ import annotations

from typing import Any

from codevigil.collectors import COLLECTORS, register_collector
from codevigil.collectors._text_match import (
    Matcher,
    PhraseSpec,
    compile_phrase_table,
)
from codevigil.config import CONFIG_DEFAULTS
from codevigil.types import Event, EventKind, MetricSnapshot, Severity

# Self-correction patterns from design.md section 3. Each entry uses
# word-boundary matching so partial-word collisions stay silent.
DEFAULT_PATTERNS: tuple[PhraseSpec, ...] = (
    PhraseSpec(text="actually", category="reasoning_loop", intent="self-correction marker"),
    PhraseSpec(
        text="oh wait", category="reasoning_loop", intent="explicit reversal of previous step"
    ),
    PhraseSpec(text="no wait", category="reasoning_loop", intent="immediate course correction"),
    PhraseSpec(
        text="let me reconsider",
        category="reasoning_loop",
        intent="opens a re-evaluation of the prior plan",
    ),
    PhraseSpec(
        text="let me rethink",
        category="reasoning_loop",
        intent="opens a re-evaluation of the prior plan",
    ),
    PhraseSpec(
        text="hmm actually",
        category="reasoning_loop",
        intent="hedging plus self-correction",
    ),
    PhraseSpec(
        text="i was wrong",
        category="reasoning_loop",
        intent="acknowledged prior error",
    ),
    PhraseSpec(
        text="on second thought",
        category="reasoning_loop",
        intent="explicit reconsideration",
    ),
    PhraseSpec(
        text="i made an error",
        category="reasoning_loop",
        intent="acknowledged prior error",
    ),
    PhraseSpec(
        text="correction:",
        category="reasoning_loop",
        intent="formal correction marker",
        mode="substring",
    ),
)


class ReasoningLoopCollector:
    """Tracks self-correction phrase rate and burst length.

    ``loop_rate`` is matches per 1000 observed ``TOOL_CALL`` events,
    which is how the design table specifies the metric. The collector
    therefore listens to two event kinds: ``TOOL_CALL`` (denominator)
    and ``ASSISTANT_MESSAGE`` (numerator).

    ``max_burst`` is the longest run of consecutive assistant messages
    where at least one match fired. A clean message resets the running
    burst counter to zero. The metric is cumulative across the session
    by design - it is the total number of consecutive flailing turns
    we have observed, and resetting it mid-session would mask the
    behaviour the metric exists to detect.
    """

    name: str = "reasoning_loop"
    complexity: str = "O(P*L) per message; escalates to O(L + matches) above 32 phrases"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._warn_threshold: float = float(cfg["warn_threshold"])
        self._critical_threshold: float = float(cfg["critical_threshold"])
        self._experimental: bool = bool(cfg["experimental"])
        # Minimum observed tool calls before severity can escalate beyond
        # OK. Without this gate a single early "actually" on the first
        # tool call computes to 1000/1k and trips CRITICAL immediately.
        self._min_tool_calls_for_severity: int = int(cfg["min_tool_calls_for_severity"])

        self._matcher: Matcher = compile_phrase_table(list(DEFAULT_PATTERNS))
        self._tool_calls: int = 0
        self._matches: int = 0
        self._max_burst: int = 0
        self._current_burst: int = 0

    def ingest(self, event: Event) -> None:
        try:
            self._ingest_unchecked(event)
        except Exception:
            return

    def _ingest_unchecked(self, event: Event) -> None:
        if event.kind is EventKind.TOOL_CALL:
            self._tool_calls += 1
            return
        if event.kind is not EventKind.ASSISTANT_MESSAGE:
            return
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            return
        hit_count = sum(1 for _ in self._matcher.match(text))
        if hit_count > 0:
            self._matches += hit_count
            self._current_burst += 1
            if self._current_burst > self._max_burst:
                self._max_burst = self._current_burst
        else:
            self._current_burst = 0

    def snapshot(self) -> MetricSnapshot:
        loop_rate = self._matches * 1000.0 / max(self._tool_calls, 1)
        warming_up = self._tool_calls < self._min_tool_calls_for_severity
        if warming_up:
            severity = Severity.OK
            label = f"{loop_rate:.1f}/1k warming up"
        elif loop_rate >= self._critical_threshold:
            severity = Severity.CRITICAL
            label = f"{loop_rate:.1f}/1k tool calls"
        elif loop_rate >= self._warn_threshold:
            severity = Severity.WARN
            label = f"{loop_rate:.1f}/1k tool calls"
        else:
            severity = Severity.OK
            label = f"{loop_rate:.1f}/1k tool calls"
        detail: dict[str, Any] = {
            "matches": self._matches,
            "tool_calls": self._tool_calls,
            "max_burst": self._max_burst,
            "min_tool_calls_for_severity": self._min_tool_calls_for_severity,
        }
        if self._experimental:
            detail["experimental"] = True
        return MetricSnapshot(
            name=self.name,
            value=loop_rate,
            label=label,
            severity=severity,
            detail=detail,
        )

    def reset(self) -> None:
        self._tool_calls = 0
        self._matches = 0
        self._max_burst = 0
        self._current_burst = 0


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["reasoning_loop"])


register_collector(COLLECTORS, ReasoningLoopCollector)


__all__ = ["DEFAULT_PATTERNS", "ReasoningLoopCollector"]
