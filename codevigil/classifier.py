"""Turn-level task classifier for codevigil.

Two-stage cascade:
  Stage 1 — tool-presence heuristics (structural evidence).
  Stage 2 — keyword regex on the user message text (lexical evidence).

Stage 1 takes precedence. Stage 2 runs only when Stage 1 is ambiguous (no
rule matched). The classifier is intentionally a pure function of a
:class:`~codevigil.turns.Turn` snapshot so it can be tested in isolation
without the aggregator stack.

Category definitions and cascade algorithm are documented in
``docs/classifier.md``. Rule changes that affect calibration must
be accompanied by a re-run of ``scripts/calibrate_classifier.py`` and an
updated ``docs/classifier-calibration.md``.

**Zero new runtime dependencies.** This module uses ``stdlib re`` only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codevigil.turns import Turn

# ---------------------------------------------------------------------------
# Category set
# ---------------------------------------------------------------------------

TASK_CATEGORIES: tuple[str, ...] = (
    "exploration",
    "mutation_heavy",
    "debug_loop",
    "planning",
    "mixed",
)

# Canonical tool names that count as "mutation" (write-family).
_MUTATION_TOOLS: frozenset[str] = frozenset({"edit", "write", "multi_edit"})

# Canonical tool names that count as "read/glob" for the exploration heuristic.
_READ_GLOB_TOOLS: frozenset[str] = frozenset({"read", "glob"})


def _mutation_count(turn: Turn) -> int:
    return sum(1 for t in turn.tool_calls if t in _MUTATION_TOOLS)


def _bash_count(turn: Turn) -> int:
    return sum(1 for t in turn.tool_calls if t == "bash")


def _read_glob_count(turn: Turn) -> int:
    return sum(1 for t in turn.tool_calls if t in _READ_GLOB_TOOLS)


# ---------------------------------------------------------------------------
# Stage 1 — tool-presence heuristics
# ---------------------------------------------------------------------------

# Each predicate returns True when its category is the clear winner. They are
# evaluated in the priority order defined by ``_STAGE1_ORDER``.


def _is_mutation_heavy(turn: Turn) -> bool:
    """Three or more mutation calls (edit/write/multi_edit) with no bash in the turn.

    Bash absence distinguishes pure mechanical annotation work (mutation_heavy)
    from a debug-edit-run loop (debug_loop, caught by rule 2).
    """
    return _mutation_count(turn) >= 3 and _bash_count(turn) == 0


def _is_debug_loop(turn: Turn) -> bool:
    """At least one bash call and at least one mutation call in the same turn.

    The co-presence of execution (bash) and mutation (edit/write) within a
    single turn is the structural signature of a debug-fix-run loop, regardless
    of strict alternation order.
    """
    return _bash_count(turn) >= 1 and _mutation_count(turn) >= 1


def _is_planning(turn: Turn) -> bool:
    """Absolutely no tool calls in this turn — a text-dominant turn.

    When a turn issues zero tool calls the assistant responded with pure text
    (a plan, explanation, or design document). Any tool call, including read,
    glob, grep, or bash, disqualifies the turn from the planning heuristic;
    those turns fall through to the exploration or debug_loop rules instead.
    """
    return len(turn.tool_calls) == 0


def _is_exploration(turn: Turn) -> bool:
    """Read/glob calls dominate, no bash, fewer than 2 mutation calls.

    Dominance: read/glob count > 2 * mutation count. This allows a single
    incidental edit (e.g., a quick note during investigation) without
    reclassifying the turn as mutation_heavy.
    """
    mc = _mutation_count(turn)
    rgc = _read_glob_count(turn)
    return _bash_count(turn) == 0 and mc < 2 and rgc > 2 * mc


# Ordered list of (category, predicate) pairs. First match wins.
TOOL_SIGNATURES: dict[str, Callable[[Turn], bool]] = {
    "mutation_heavy": _is_mutation_heavy,
    "debug_loop": _is_debug_loop,
    "planning": _is_planning,
    "exploration": _is_exploration,
}

# Evaluation order for stage 1. More-specific rules come first so they take
# precedence over broader ones.
_STAGE1_ORDER: tuple[str, ...] = (
    "mutation_heavy",
    "debug_loop",
    "planning",
    "exploration",
)

# ---------------------------------------------------------------------------
# Stage 2 — keyword regex on user message text
# ---------------------------------------------------------------------------

# Regexes are compiled at import time. re.IGNORECASE is used throughout so
# that natural-language capitalisation does not affect matching.

KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {
    "debug_loop": re.compile(
        r"\b(fix|debug|broken|failing|error|exception|crash|traceback|"
        r"why is|not working|failing test|reproduce|regression)\b",
        re.IGNORECASE,
    ),
    "mutation_heavy": re.compile(
        r"\b(implement|add|create|build|write|generate|scaffold|port|migrate|"
        r"convert|refactor|rename|move|delete|remove|update)\b",
        re.IGNORECASE,
    ),
    "exploration": re.compile(
        r"\b(why|investigate|explore|understand|look at|walk me through|"
        r"show me|what does|how does|explain|trace|find where|where is|"
        r"which file|map out)\b",
        re.IGNORECASE,
    ),
    "planning": re.compile(
        r"\b(plan|design|architect|outline|propose|think through|approach|"
        r"strategy|how should|what would|should we|tradeoff|option|compare|"
        r"consider)\b",
        re.IGNORECASE,
    ),
}

# Evaluation order for stage 2. More-specific categories are checked first.
# debug_loop is first because debug-phrased prompts often also contain
# mutation verbs ("fix this", "implement the fix") and the debug label is
# more specific. mutation_heavy before exploration for the same reason.
_STAGE2_ORDER: tuple[str, ...] = (
    "debug_loop",
    "mutation_heavy",
    "exploration",
    "planning",
)

# ---------------------------------------------------------------------------
# Classification functions
# ---------------------------------------------------------------------------


def classify_turn(turn: Turn) -> str:
    """Classify a single completed turn into one of :data:`TASK_CATEGORIES`.

    Stage 1 (tool-presence) is evaluated first. When no rule matches,
    Stage 2 (keyword regex) provides a tiebreaker. When neither stage
    produces a match the turn is labelled ``"mixed"``.

    Parameters
    ----------
    turn:
        A completed :class:`~codevigil.turns.Turn` snapshot from the aggregator.

    Returns
    -------
    str
        One of the values in :data:`TASK_CATEGORIES`.
    """
    # Stage 1 — structural evidence.
    for category in _STAGE1_ORDER:
        predicate = TOOL_SIGNATURES[category]
        if predicate(turn):
            return category

    # Stage 2 — lexical evidence on user message text.
    text = turn.user_message_text
    for category in _STAGE2_ORDER:
        pattern = KEYWORD_PATTERNS[category]
        if pattern.search(text):
            return category

    return "mixed"


def aggregate_session_task_type(turns: Sequence[Turn]) -> str:
    """Return the session-level majority task type across *turns*.

    Each turn's ``task_type`` attribute is consulted. Turns with
    ``task_type=None`` are skipped (they contribute nothing to the vote).

    Rules:

    - Sessions with fewer than two turns (after skipping unclassified turns)
      default to ``"mixed"``; the signal is too weak for a confident
      session-level label.
    - When a single category holds more than 50% of the labelled turns it
      wins.
    - When no category exceeds 50% (including ties) the session is
      ``"mixed"``.

    Parameters
    ----------
    turns:
        The completed turn sequence from ``_SessionContext.completed_turns``,
        each already classified via :func:`classify_turn`.

    Returns
    -------
    str
        One of the values in :data:`TASK_CATEGORIES`.
    """
    labelled: list[str] = [t.task_type for t in turns if t.task_type is not None]

    if len(labelled) < 2:
        return "mixed"

    counts: dict[str, int] = {}
    for label in labelled:
        counts[label] = counts.get(label, 0) + 1

    total = len(labelled)
    for label, count in counts.items():
        if count / total > 0.5:
            return label

    return "mixed"


__all__ = [
    "KEYWORD_PATTERNS",
    "TASK_CATEGORIES",
    "TOOL_SIGNATURES",
    "aggregate_session_task_type",
    "classify_turn",
]
