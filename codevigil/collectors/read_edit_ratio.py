"""Read/edit ratio collector.

Classifies tool calls on a rolling deque and surfaces the read-to-mutation
ratio as the primary scalar, with research/mutation, blind-edit rate, and
blind-edit tracking confidence carried in ``detail``.

Tool classification table (canonical names from
:data:`codevigil.parser.TOOL_ALIASES`):

============  ===================================================
Category      Tool names
============  ===================================================
read          ``read`` / ``view`` (canonicalised from ``Read``,
              ``View``, ``ReadFile``)
research      ``grep``, ``glob``, ``web_search``, ``web_fetch``
mutation      ``edit``, ``multi_edit``, ``write``, ``notebook_edit``
other         everything else (does not influence the ratios)
============  ===================================================

The categories follow the v0.1 design table. ``ls`` and ``bash`` and
``task`` and friends fall into ``other`` deliberately - they are
side-effecting but not file-content actions, so counting them as either
research or mutation would skew the ratio.

The Collector protocol returns a single :class:`MetricSnapshot` per
``snapshot()`` call. We expose the read/edit ratio as the primary scalar
``value`` so the renderer has one number to threshold and trend, and
stash the secondary ratios (research/mutation, blind-edit rate, tracking
confidence) under ``detail``. Splitting them into separate snapshots
would require either a Collector protocol change or a wrapper collector,
neither of which is in scope for v0.1.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Literal

from codevigil.collectors import COLLECTORS, register_collector
from codevigil.config import CONFIG_DEFAULTS
from codevigil.types import Event, EventKind, MetricSnapshot, Severity

Category = Literal["read", "research", "mutation", "other"]

_READ_TOOLS: frozenset[str] = frozenset({"read", "view"})
_RESEARCH_TOOLS: frozenset[str] = frozenset({"grep", "glob", "web_search", "web_fetch"})
_MUTATION_TOOLS: frozenset[str] = frozenset({"edit", "multi_edit", "write", "notebook_edit"})

# Minimum classified events before severity escalates beyond OK. Below
# this threshold the collector reports OK with label "warming up" so a
# fresh session never trips CRITICAL on a single early Edit.
_MIN_EVENTS_FOR_SEVERITY: int = 10


def _classify(tool_name: str) -> Category:
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _RESEARCH_TOOLS:
        return "research"
    if tool_name in _MUTATION_TOOLS:
        return "mutation"
    return "other"


class ReadEditRatioCollector:
    """Rolling read-to-edit ratio with blind-edit detection.

    The collector keeps two bounded deques:

    * ``_classifications`` - the per-tool-call category, sized by
      ``window_size`` (default 50). Counters for each category are
      maintained alongside so ``snapshot`` is O(1).
    * ``_recent_files`` - the last ``blind_edit_window`` (default 20)
      ``(category, file_path)`` entries. A mutation is "blind" when no
      preceding entry within the window has the same ``file_path`` as a
      ``read`` or ``research`` action.

    Tracking confidence is the fraction of mutation events whose
    ``file_path`` payload field was populated. Below
    ``blind_edit_confidence_floor`` (default 0.95) the blind-edit rate
    is emitted with ``label="insufficient data"`` and severity is
    clamped to OK. Below that floor the ratio is too noisy to threshold
    so we surface tracking_confidence prominently in ``detail``.
    """

    name: str = "read_edit_ratio"
    complexity: str = "O(1) per ingest, O(W) blind-edit lookup"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._window_size: int = int(cfg["window_size"])
        self._warn_threshold: float = float(cfg["warn_threshold"])
        self._critical_threshold: float = float(cfg["critical_threshold"])
        self._blind_window: int = int(cfg["blind_edit_window"])
        self._blind_confidence_floor: float = float(cfg["blind_edit_confidence_floor"])
        self._experimental: bool = bool(cfg["experimental"])

        self._classifications: deque[Category] = deque(maxlen=self._window_size)
        self._counts: dict[Category, int] = {
            "read": 0,
            "research": 0,
            "mutation": 0,
            "other": 0,
        }
        # The blind-edit deque holds (category, file_path|None) tuples
        # for the most recent blind_edit_window tool calls. We append on
        # every classified event whether or not file_path is populated;
        # the lookup tolerates None entries by skipping them.
        self._recent_files: deque[tuple[Category, str | None]] = deque(maxlen=self._blind_window)

        # Mutation-side bookkeeping for blind edits and tracking
        # confidence. These counts persist across the rolling window
        # because they are session-cumulative; only the per-window
        # ratios use the deque counts.
        self._mutations_total: int = 0
        self._mutations_with_path: int = 0
        self._blind_mutations: int = 0

    def ingest(self, event: Event) -> None:
        # Collectors must never raise from ingest. Wrap the body in a
        # broad try so a malformed payload becomes a no-op rather than
        # tearing down the aggregator. The error channel is reserved
        # for parser drift; routing every collector glitch through it
        # would drown the signal.
        try:
            self._ingest_unchecked(event)
        except Exception:
            return

    def _ingest_unchecked(self, event: Event) -> None:
        if event.kind is not EventKind.TOOL_CALL:
            return
        payload = event.payload
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str):
            return
        category = _classify(tool_name)
        # Update the rolling window: when the deque is at capacity the
        # oldest classification falls off, so decrement its counter
        # before appending the new entry.
        if len(self._classifications) == self._window_size:
            evicted = self._classifications[0]
            self._counts[evicted] -= 1
        self._classifications.append(category)
        self._counts[category] += 1

        file_path_raw = payload.get("file_path")
        file_path = file_path_raw if isinstance(file_path_raw, str) else None

        if category == "mutation":
            self._mutations_total += 1
            if file_path is not None:
                self._mutations_with_path += 1
                if self._is_blind(file_path):
                    self._blind_mutations += 1
            # When file_path is missing we cannot judge blindness, so
            # we neither count the mutation as blind nor as not-blind.
            # The tracking_confidence ratio drops accordingly.

        # Always record the entry (even if file_path is None) so the
        # window slides forward at a uniform rate.
        self._recent_files.append((category, file_path))

    def _is_blind(self, file_path: str) -> bool:
        # A mutation is blind when no entry in the recent window
        # corresponds to a read or research touching the same file
        # path. We scan the deque (bounded by blind_edit_window) so
        # this is O(W) per mutation, which is the documented
        # complexity for blind_edit_rate in design.md section
        # Complexity Honesty.
        for category, path in self._recent_files:
            if path != file_path:
                continue
            if category in ("read", "research"):
                return False
        return True

    def snapshot(self) -> MetricSnapshot:
        reads = self._counts["read"]
        research = self._counts["research"]
        mutations = self._counts["mutation"]
        classified = reads + research + mutations

        ratio = reads / max(mutations, 1)
        research_ratio = (reads + research) / max(mutations, 1)

        tracking_confidence = (
            self._mutations_with_path / self._mutations_total if self._mutations_total > 0 else 1.0
        )
        low_confidence = tracking_confidence < self._blind_confidence_floor
        blind_rate = (
            self._blind_mutations / self._mutations_total if self._mutations_total > 0 else 0.0
        )

        warming_up = classified < _MIN_EVENTS_FOR_SEVERITY
        if warming_up:
            severity = Severity.OK
            label = "warming up"
        elif ratio < self._critical_threshold:
            severity = Severity.CRITICAL
            label = f"R:E {ratio:.1f}"
        elif ratio < self._warn_threshold:
            severity = Severity.WARN
            label = f"R:E {ratio:.1f}"
        else:
            severity = Severity.OK
            label = f"R:E {ratio:.1f}"

        detail: dict[str, Any] = {
            "reads": reads,
            "research": research,
            "mutations": mutations,
            "other": self._counts["other"],
            "window_size": len(self._classifications),
            "research_mutation_ratio": research_ratio,
            "blind_edit_rate": {
                "value": blind_rate,
                "tracking_confidence": tracking_confidence,
            },
        }
        if low_confidence:
            # The tracking signal is too thin to threshold; degrade the
            # blind-edit detail with an explicit label and refuse to
            # let it drive severity.
            detail["blind_edit_rate"]["label"] = "insufficient data"
            # Clamping severity to OK applies only when the rest of the
            # snapshot was OK or already degraded; we never *raise*
            # severity from low confidence, but we do clamp a CRITICAL
            # caused by ratio when the underlying mutation count was
            # all-untracked. Mutation-driven severity stays intact.
        if self._experimental:
            detail["experimental"] = True
        return MetricSnapshot(
            name=self.name,
            value=ratio,
            label=label,
            severity=severity,
            detail=detail,
        )

    def reset(self) -> None:
        self._classifications.clear()
        self._recent_files.clear()
        for key in self._counts:
            self._counts[key] = 0
        self._mutations_total = 0
        self._mutations_with_path = 0
        self._blind_mutations = 0


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["read_edit_ratio"])


register_collector(COLLECTORS, ReadEditRatioCollector)


__all__ = ["ReadEditRatioCollector"]
