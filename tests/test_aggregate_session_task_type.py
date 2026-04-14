"""Unit tests for aggregate_session_task_type.

Covers majority rules, mixed default conditions, and the <2-turn minimum.
"""

from __future__ import annotations

from datetime import UTC, datetime

from codevigil.classifier import aggregate_session_task_type
from codevigil.turns import Turn

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_END = datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC)


def _turn(task_type: str | None) -> Turn:
    return Turn(
        session_id="test",
        started_at=_BASE,
        ended_at=_END,
        user_message_text="",
        tool_calls=(),
        event_count=1,
        task_type=task_type,
    )


# ---------------------------------------------------------------------------
# Fewer than 2 labelled turns → mixed
# ---------------------------------------------------------------------------


def test_empty_turns_returns_mixed() -> None:
    assert aggregate_session_task_type([]) == "mixed"


def test_single_turn_returns_mixed() -> None:
    turns = [_turn("exploration")]
    assert aggregate_session_task_type(turns) == "mixed"


def test_single_unclassified_turn_returns_mixed() -> None:
    turns = [_turn(None)]
    assert aggregate_session_task_type(turns) == "mixed"


def test_two_turns_one_unclassified_returns_mixed() -> None:
    # Only 1 labelled turn after skipping None → below threshold.
    turns = [_turn(None), _turn("exploration")]
    assert aggregate_session_task_type(turns) == "mixed"


# ---------------------------------------------------------------------------
# Majority rules
# ---------------------------------------------------------------------------


def test_two_identical_labels_wins() -> None:
    turns = [_turn("exploration"), _turn("exploration")]
    assert aggregate_session_task_type(turns) == "exploration"


def test_majority_over_50_percent_wins() -> None:
    turns = [
        _turn("debug_loop"),
        _turn("debug_loop"),
        _turn("planning"),
    ]
    # 2/3 = 66.7% > 50%.
    assert aggregate_session_task_type(turns) == "debug_loop"


def test_no_majority_returns_mixed() -> None:
    turns = [
        _turn("exploration"),
        _turn("debug_loop"),
        _turn("planning"),
    ]
    # Each at 1/3 = 33.3%.
    assert aggregate_session_task_type(turns) == "mixed"


def test_exact_tie_returns_mixed() -> None:
    # 2/4 = 50% exactly — must not exceed 50%.
    turns = [
        _turn("exploration"),
        _turn("exploration"),
        _turn("debug_loop"),
        _turn("debug_loop"),
    ]
    assert aggregate_session_task_type(turns) == "mixed"


def test_majority_with_unclassified_turns() -> None:
    # Unclassified turns are excluded from denominator.
    turns = [
        _turn("mutation_heavy"),
        _turn("mutation_heavy"),
        _turn(None),
        _turn("planning"),
    ]
    # 2/3 labelled = 66.7% → mutation_heavy.
    assert aggregate_session_task_type(turns) == "mutation_heavy"


def test_three_turns_exact_majority_boundary() -> None:
    # 2/3 = 66.7% > 50% → wins.
    turns = [
        _turn("planning"),
        _turn("planning"),
        _turn("exploration"),
    ]
    assert aggregate_session_task_type(turns) == "planning"


def test_four_turn_session_no_majority() -> None:
    turns = [
        _turn("exploration"),
        _turn("debug_loop"),
        _turn("mixed"),
        _turn("planning"),
    ]
    assert aggregate_session_task_type(turns) == "mixed"


def test_all_mixed_labels_return_mixed() -> None:
    turns = [_turn("mixed"), _turn("mixed"), _turn("mixed")]
    # 3/3 = 100% → mixed wins via majority.
    assert aggregate_session_task_type(turns) == "mixed"
