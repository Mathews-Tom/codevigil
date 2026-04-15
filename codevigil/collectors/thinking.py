"""Thinking-block collector.

Observes ``EventKind.THINKING`` events emitted by the parser and tracks
their length, redaction status, and signature length. Surfaces the
fraction of thinking blocks that arrived visible (non-redacted) as the
primary scalar so the cohort reducer can trend the redaction-rollout
curve from issue #42796 over time.

Per #42796 §2 ("Thinking depth decline") the median visible thinking
character length is the headline figure. We expose three secondary
signals via ``detail`` for downstream analysis:

* ``thinking_blocks`` — total thinking events observed.
* ``visible_blocks`` — events whose ``redacted`` flag was false.
* ``visible_chars_median`` — median character length of visible blocks
  (None when no visible blocks observed).
* ``signature_chars_median`` — median signature length across all
  blocks (None when no signatures present); used as a proxy for
  redacted thinking depth per the #42796 r=0.971 correlation.

Severity is intentionally always OK. The collector is a descriptive
counter, not a quality gate — there is no validated threshold for
"too little thinking" and we refuse to invent one. The cohort reducer
and renderer surface the trend; correlation-not-causation discipline is
enforced upstream.
"""

from __future__ import annotations

import statistics
from typing import Any

from codevigil.collectors import COLLECTORS, register_collector
from codevigil.config import CONFIG_DEFAULTS
from codevigil.types import Event, EventKind, MetricSnapshot, Severity


class ThinkingCollector:
    """Counts thinking blocks and tracks visible-vs-redacted depth.

    The primary scalar is the *visible block ratio*:
    ``visible_blocks / thinking_blocks``. A value of 1.0 means every
    thinking block carried inline text the model emitted; 0.0 means
    every block was redacted to a signature-only stub.

    Median character lengths are computed lazily on ``snapshot()`` so
    ingest stays O(1). The lengths buffers are unbounded by design — a
    single session never holds enough thinking blocks to matter, and
    bounding them would silently distort the median.
    """

    name: str = "thinking"
    complexity: str = "O(1) per ingest, O(n log n) per snapshot for median"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._experimental: bool = bool(cfg["experimental"])

        self._thinking_blocks: int = 0
        self._visible_blocks: int = 0
        self._visible_lengths: list[int] = []
        self._signature_lengths: list[int] = []

    def ingest(self, event: Event) -> None:
        try:
            self._ingest_unchecked(event)
        except Exception:
            return

    def _ingest_unchecked(self, event: Event) -> None:
        if event.kind is not EventKind.THINKING:
            return
        payload = event.payload
        self._thinking_blocks += 1

        redacted = bool(payload.get("redacted", False))
        if not redacted:
            self._visible_blocks += 1
            length_raw = payload.get("length")
            if isinstance(length_raw, int) and length_raw > 0:
                self._visible_lengths.append(length_raw)

        signature = payload.get("signature")
        if isinstance(signature, str) and signature:
            self._signature_lengths.append(len(signature))

    def snapshot(self) -> MetricSnapshot:
        total = self._thinking_blocks
        visible_ratio = self._visible_blocks / total if total > 0 else 0.0

        visible_median: float | None = (
            float(statistics.median(self._visible_lengths)) if self._visible_lengths else None
        )
        signature_median: float | None = (
            float(statistics.median(self._signature_lengths)) if self._signature_lengths else None
        )

        label = "no thinking blocks" if total == 0 else f"{self._visible_blocks}/{total} visible"

        detail: dict[str, Any] = {
            "thinking_blocks": total,
            "visible_blocks": self._visible_blocks,
            "redacted_blocks": total - self._visible_blocks,
            "visible_chars_median": visible_median,
            "signature_chars_median": signature_median,
        }
        if self._experimental:
            detail["experimental"] = True

        return MetricSnapshot(
            name=self.name,
            value=visible_ratio,
            label=label,
            severity=Severity.OK,
            detail=detail,
        )

    def reset(self) -> None:
        self._thinking_blocks = 0
        self._visible_blocks = 0
        self._visible_lengths.clear()
        self._signature_lengths.clear()

    def serialize_state(self) -> dict[str, Any]:
        return {
            "thinking_blocks": self._thinking_blocks,
            "visible_blocks": self._visible_blocks,
            "visible_lengths": list(self._visible_lengths),
            "signature_lengths": list(self._signature_lengths),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._thinking_blocks = int(state.get("thinking_blocks", 0))
        self._visible_blocks = int(state.get("visible_blocks", 0))
        raw_visible = state.get("visible_lengths", [])
        self._visible_lengths = (
            [int(v) for v in raw_visible if isinstance(v, int)]
            if isinstance(raw_visible, list)
            else []
        )
        raw_sigs = state.get("signature_lengths", [])
        self._signature_lengths = (
            [int(v) for v in raw_sigs if isinstance(v, int)] if isinstance(raw_sigs, list) else []
        )


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["thinking"])


register_collector(COLLECTORS, ThinkingCollector)


__all__ = ["ThinkingCollector"]
