"""Read/edit ratio collector.

Classifies tool calls on a rolling deque and surfaces the read-to-mutation
ratio as the primary scalar, with research/mutation, blind-edit rate,
blind-edit tracking confidence, and write-vs-edit surgical precision carried
in ``detail``.

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

Within the mutation category, ``write`` tool calls are distinguished from
``edit``/``multi_edit``/``notebook_edit`` calls to compute
``write_precision``: the fraction of mutation calls that are wholesale
writes rather than surgical edits. Defined as::

    write_precision = write_calls / (write_calls + edit_calls)

where ``edit_calls`` covers ``edit``, ``multi_edit``, and
``notebook_edit``. This is directly comparable to §4 of the target
retrospective issue. When no mutation calls have been observed
``write_precision`` is ``None`` (not 0.0, to avoid implying false data).

The categories follow the v0.1 design table. ``ls`` and ``bash`` and
``task`` and friends fall into ``other`` deliberately - they are
side-effecting but not file-content actions, so counting them as either
research or mutation would skew the ratio.

The Collector protocol returns a single :class:`MetricSnapshot` per
``snapshot()`` call. We expose the read/edit ratio as the primary scalar
``value`` so the renderer has one number to threshold and trend, and
stash the secondary ratios (research/mutation, blind-edit rate, tracking
confidence, write_precision) under ``detail``. Splitting them into
separate snapshots would require either a Collector protocol change or a
wrapper collector, neither of which is in scope for v0.1.
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

# Mutation sub-categories for write_precision computation.
# ``write`` tools replace file content wholesale; ``edit`` tools make
# surgical in-place changes. write_precision = write_calls / total_mutations.
_WRITE_TOOLS: frozenset[str] = frozenset({"write"})
_EDIT_TOOLS: frozenset[str] = frozenset({"edit", "multi_edit", "notebook_edit"})


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

    The collector keeps:

    * ``_classifications`` - the per-tool-call category, sized by
      ``window_size`` (default 50). Counters for each category are
      maintained alongside so ``snapshot`` is O(1).
    * ``_last_seen_read`` - a per-file map from ``file_path`` to the
      classified-event index where the file was most recently ``read``
      or ``research`` touched. A mutation at index ``N`` with
      ``file_path=p`` is "blind" when ``_last_seen_read.get(p)`` is
      missing or older than ``N - blind_edit_window`` events ago. This
      replaces an earlier global rolling deque that was polluted by
      unrelated mutations falling into the same lookback window.

    Tracking confidence is the fraction of mutation events whose
    ``file_path`` payload field was populated. Below
    ``blind_edit_confidence_floor`` (default 0.95) the blind-edit rate
    is emitted with ``label="insufficient data"`` and severity is
    clamped to OK. Below that floor the ratio is too noisy to threshold
    so we surface tracking_confidence prominently in ``detail``.
    """

    name: str = "read_edit_ratio"
    complexity: str = "O(1) per ingest, O(1) blind-edit lookup"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._window_size: int = int(cfg["window_size"])
        self._warn_threshold: float = float(cfg["warn_threshold"])
        self._critical_threshold: float = float(cfg["critical_threshold"])
        self._blind_window: int = int(cfg["blind_edit_window"])
        self._blind_confidence_floor: float = float(cfg["blind_edit_confidence_floor"])
        self._experimental: bool = bool(cfg["experimental"])
        self._min_events_for_severity: int = int(cfg["min_events_for_severity"])

        self._classifications: deque[Category] = deque(maxlen=self._window_size)
        self._counts: dict[Category, int] = {
            "read": 0,
            "research": 0,
            "mutation": 0,
            "other": 0,
        }
        # Monotonic classified-event index. Incremented on every tool
        # call that resolved to read / research / mutation / other so
        # "recency" can be expressed in classified-event distance
        # without being polluted by other event kinds.
        self._classified_index: int = 0
        # Per-file map: file_path -> most recent classified_index where
        # the file was observed in read or research context. A mutation
        # lookup checks (current_index - last_read_index) against the
        # blind_edit_window. O(1) insert and lookup; unbounded in key
        # count but bounded by unique files touched in the session.
        self._last_seen_read: dict[str, int] = {}

        # Mutation-side bookkeeping for blind edits, tracking
        # confidence, and write-vs-edit precision. These counts persist
        # across the rolling window because they are session-cumulative;
        # only the per-window ratios use the deque counts.
        self._mutations_total: int = 0
        self._mutations_with_path: int = 0
        self._blind_mutations: int = 0
        # Sub-category counts for write_precision.
        self._write_calls: int = 0  # "write" tool calls
        self._edit_calls: int = 0  # "edit", "multi_edit", "notebook_edit"

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
        self._classified_index += 1

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
            # Track write vs. edit sub-categories for write_precision.
            if tool_name in _WRITE_TOOLS:
                self._write_calls += 1
            elif tool_name in _EDIT_TOOLS:
                self._edit_calls += 1
        elif category in ("read", "research") and file_path is not None:
            # Record this read/research so a later mutation on the same
            # file can detect it regardless of unrelated tool calls
            # that fall between them. We overwrite on every touch so
            # the most recent index is the one the blindness check
            # sees.
            self._last_seen_read[file_path] = self._classified_index

    def _is_blind(self, file_path: str) -> bool:
        # A mutation is blind when the most recent read or research on
        # the same file path was either never observed or was more
        # than ``blind_edit_window`` classified events ago. Per-file
        # tracking means unrelated mutations between a read and its
        # follow-up edit cannot pollute the lookback window.
        last_seen = self._last_seen_read.get(file_path)
        if last_seen is None:
            return True
        return (self._classified_index - last_seen) > self._blind_window

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

        warming_up = classified < self._min_events_for_severity
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

        # write_precision: write_calls / (write_calls + edit_calls).
        # None when no mutation sub-category calls observed yet (not 0.0,
        # to avoid implying a meaningful zero when data is absent).
        write_edit_total = self._write_calls + self._edit_calls
        write_precision: float | None = (
            self._write_calls / write_edit_total if write_edit_total > 0 else None
        )

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
            "write_precision": write_precision,
            "write_calls": self._write_calls,
            "edit_calls": self._edit_calls,
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
        self._last_seen_read.clear()
        self._classified_index = 0
        for key in self._counts:
            self._counts[key] = 0
        self._mutations_total = 0
        self._mutations_with_path = 0
        self._blind_mutations = 0
        self._write_calls = 0
        self._edit_calls = 0


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["read_edit_ratio"])


register_collector(COLLECTORS, ReadEditRatioCollector)


__all__ = ["ReadEditRatioCollector"]
