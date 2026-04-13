"""Stop-phrase collector.

Scans assistant message text for phrases that flag specific anti-patterns:
ownership dodging, permission seeking, premature stopping, and known
limitation hedges. Hits are bucketed by category, the most recent five
are kept for ``--explain``-style introspection, and the primary scalar
is the per-message hit rate.

Matching defaults to word-boundary mode so phrases like ``"should I"``
do not fire on ``"shoulder"``. Users opt into substring or regex per
phrase via the table form in config (see :mod:`codevigil.config`).

The phrase table is small by default (~16 phrases) so the naive regex
matcher is plenty. When user-supplied custom phrases push the total
past 32 the shared :func:`compile_phrase_table` escalates to an
Aho-Corasick automaton transparently. See ``_text_match`` for details.
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
from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.types import Event, EventKind, MetricSnapshot, Severity

# Default phrase table. Each entry carries an ``intent`` annotation so
# users can audit why a phrase is flagged via the (future) ``--explain``
# CLI flag. Keep the list small but realistic - a dozen or so high-signal
# phrases is enough to demonstrate the categories without false positives.
DEFAULT_PHRASES: tuple[PhraseSpec, ...] = (
    # ownership_dodging - blames pre-existing state instead of owning the change.
    PhraseSpec(
        text="not caused by my changes",
        category="ownership_dodging",
        intent="deflects responsibility for breakage onto pre-existing state",
    ),
    PhraseSpec(
        text="pre-existing",
        category="ownership_dodging",
        intent="hedges by labelling a problem pre-existing",
    ),
    PhraseSpec(
        text="existing issue",
        category="ownership_dodging",
        intent="frames a fresh failure as an existing issue",
    ),
    PhraseSpec(
        text="outside the scope",
        category="ownership_dodging",
        intent="punts a problem as out of scope without justification",
    ),
    # permission_seeking - asks the user to make a call the agent should make.
    PhraseSpec(
        text="should I continue",
        category="permission_seeking",
        intent="hands the next decision to the user mid-task",
    ),
    PhraseSpec(
        text="want me to keep going",
        category="permission_seeking",
        intent="invites the user to greenlight already-implied work",
    ),
    PhraseSpec(
        text="shall I proceed",
        category="permission_seeking",
        intent="asks for permission instead of executing",
    ),
    PhraseSpec(
        text="would you like me to",
        category="permission_seeking",
        intent="opens a yes/no instead of acting",
    ),
    # premature_stopping - declares a stopping point before the goal is met.
    PhraseSpec(
        text="good stopping point",
        category="premature_stopping",
        intent="declares premature completion",
    ),
    PhraseSpec(
        text="natural checkpoint",
        category="premature_stopping",
        intent="frames a halt as a natural checkpoint",
    ),
    PhraseSpec(
        text="let's pause here",
        category="premature_stopping",
        intent="invites a halt without finishing the work",
    ),
    PhraseSpec(
        text="this should work for now",
        category="premature_stopping",
        intent="hedges with a temporary should-work claim",
    ),
    # known_limitation - flags work as out of reach without trying.
    PhraseSpec(
        text="known limitation",
        category="known_limitation",
        intent="labels a fixable problem as a known limitation",
    ),
    PhraseSpec(
        text="future work",
        category="known_limitation",
        intent="defers the open issue to vague future work",
    ),
    PhraseSpec(
        text="out of scope",
        category="known_limitation",
        intent="punts work without scope justification",
    ),
    PhraseSpec(
        text="beyond the scope",
        category="known_limitation",
        intent="similar punt phrasing",
    ),
)

_RECENT_HITS_CAP: int = 5
_CONTEXT_WINDOW: int = 40  # characters on each side of the matched span


def _record_skipped_phrase(message: str, context: dict[str, Any]) -> None:
    """Emit a WARN on the error channel for a dropped custom phrase.

    Shared by the two skip paths in :func:`_coerce_custom_phrases`
    (missing ``text`` field and unsupported entry type) so the error
    shape stays consistent.
    """

    record(
        CodevigilError(
            level=ErrorLevel.WARN,
            source=ErrorSource.COLLECTOR,
            code="stop_phrase.custom_phrase_skipped",
            message=message,
            context=context,
        )
    )


def _coerce_custom_phrases(raw: list[Any]) -> list[PhraseSpec]:
    """Translate the config list into typed :class:`PhraseSpec` entries.

    Accepts both the plain-string form and the table form. Plain strings
    default to ``word`` mode and the ``custom`` category; tables may set
    any of ``text``, ``mode``, ``category``, ``intent``. Unknown modes
    fall back to ``word`` rather than raising - the validator already
    caught bad shapes at config load time. Malformed entries that slip
    past the validator (e.g. an in-memory config passed around it) are
    logged via :func:`_record_skipped_phrase` and dropped.
    """

    out: list[PhraseSpec] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(PhraseSpec(text=entry, category="custom", mode="word"))
            continue
        if isinstance(entry, dict):
            text = entry.get("text")
            if not isinstance(text, str):
                _record_skipped_phrase(
                    "dropped custom phrase entry with missing or non-string 'text' field",
                    {"entry_keys": sorted(entry.keys())},
                )
                continue
            mode_raw = entry.get("mode", "word")
            mode = mode_raw if mode_raw in {"word", "regex", "substring"} else "word"
            category = entry.get("category", "custom")
            intent = entry.get("intent")
            out.append(
                PhraseSpec(
                    text=text,
                    mode=mode,  # type: ignore[arg-type]
                    category=str(category),
                    intent=intent if isinstance(intent, str) else None,
                )
            )
            continue
        _record_skipped_phrase(
            f"dropped custom phrase entry of unsupported type {type(entry).__name__!r}",
            {"entry_type": type(entry).__name__},
        )
    return out


class StopPhraseCollector:
    """Counts stop-phrase hits in assistant messages.

    The collector keeps:

    * A monotonic ``_messages`` counter for the rate denominator.
    * A monotonic ``_hits`` counter and a per-category breakdown.
    * A bounded list of the five most recent hits, capped at
      :data:`_RECENT_HITS_CAP`. Each entry carries the matched phrase,
      the matched substring, and the message index so renderers (and
      the future ``--explain`` flag) can surface context.

    Severity rises with cumulative hits: WARN at ``warn_threshold``
    (default 1), CRITICAL at ``critical_threshold`` (default 3).
    """

    name: str = "stop_phrase"
    complexity: str = "O(P*L) per message; escalates to O(L + matches) above 32 phrases"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._warn_threshold: int = int(cfg["warn_threshold"])
        self._critical_threshold: int = int(cfg["critical_threshold"])
        self._experimental: bool = bool(cfg["experimental"])
        custom = _coerce_custom_phrases(list(cfg.get("custom_phrases", [])))
        self._phrases: list[PhraseSpec] = [*DEFAULT_PHRASES, *custom]
        self._matcher: Matcher = compile_phrase_table(self._phrases)
        self._messages: int = 0
        self._hits: int = 0
        # Messages that contained at least one stop-phrase hit. The
        # snapshot value is this / messages so the metric stays bounded
        # in [0, 1]; the absolute hit count is surfaced in detail.
        self._messages_with_hit: int = 0
        self._hits_by_category: dict[str, int] = {}
        self._recent_hits: list[dict[str, Any]] = []

    def ingest(self, event: Event) -> None:
        try:
            self._ingest_unchecked(event)
        except Exception:
            return

    def _ingest_unchecked(self, event: Event) -> None:
        if event.kind is not EventKind.ASSISTANT_MESSAGE:
            return
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            return
        self._messages += 1
        message_index = self._messages
        message_had_hit = False
        for hit in self._matcher.match(text):
            self._hits += 1
            message_had_hit = True
            self._hits_by_category[hit.spec.category] = (
                self._hits_by_category.get(hit.spec.category, 0) + 1
            )
            # Extract a 40-char window centred on the match, trimming at
            # string boundaries. ctx_start..ctx_end is at most
            # _CONTEXT_WINDOW chars before the match and _CONTEXT_WINDOW
            # chars after it, capped by len(text).
            ctx_start = max(0, hit.start - _CONTEXT_WINDOW)
            ctx_end = min(len(text), hit.end + _CONTEXT_WINDOW)
            context_snippet = text[ctx_start:ctx_end]
            self._recent_hits.append(
                {
                    "category": hit.spec.category,
                    "phrase": hit.spec.text,
                    "matched_substring": hit.matched,
                    "context_snippet": context_snippet,
                    "intent": hit.spec.intent,
                    "message_index": message_index,
                }
            )
            if len(self._recent_hits) > _RECENT_HITS_CAP:
                # Drop the oldest entry - we only keep the most recent
                # five, which is what the design table calls for.
                self._recent_hits.pop(0)
        if message_had_hit:
            self._messages_with_hit += 1

    def snapshot(self) -> MetricSnapshot:
        # Value semantic: fraction of observed assistant messages that
        # contained at least one stop-phrase hit. Bounded in [0, 1], so
        # the renderer can threshold or percentile it without worrying
        # about values exceeding 1.0 when a single message has multiple
        # hits. Absolute hit count stays in detail for drill-down.
        hit_rate = self._messages_with_hit / max(self._messages, 1)
        if self._hits >= self._critical_threshold:
            severity = Severity.CRITICAL
        elif self._hits >= self._warn_threshold:
            severity = Severity.WARN
        else:
            severity = Severity.OK
        label = f"{self._hits} stop-phrase hit(s)"
        detail: dict[str, Any] = {
            "hits": self._hits,
            "messages": self._messages,
            "messages_with_hit": self._messages_with_hit,
            "hits_by_category": dict(self._hits_by_category),
            "recent_hits": list(self._recent_hits),
            "matcher_mode": self._matcher.mode,
            "phrase_count": self._matcher.phrase_count,
        }
        if self._experimental:
            detail["experimental"] = True
        return MetricSnapshot(
            name=self.name,
            value=hit_rate,
            label=label,
            severity=severity,
            detail=detail,
        )

    def reset(self) -> None:
        self._messages = 0
        self._hits = 0
        self._messages_with_hit = 0
        self._hits_by_category.clear()
        self._recent_hits.clear()


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["stop_phrase"])


register_collector(COLLECTORS, StopPhraseCollector)


__all__ = ["DEFAULT_PHRASES", "StopPhraseCollector"]
