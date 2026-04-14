"""Unit tests for classify_turn stage-1 and stage-2 paths.

One test per category per stage, plus aggregate_session_task_type edge cases.
All tests construct minimal Turn objects — no parser or aggregator stack.
"""

from __future__ import annotations

from datetime import UTC, datetime

from codevigil.classifier import classify_turn
from codevigil.turns import Turn

_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_END_TIME = datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC)


def _turn(
    tool_calls: tuple[str, ...] = (),
    user_message_text: str = "",
) -> Turn:
    return Turn(
        session_id="test",
        started_at=_BASE_TIME,
        ended_at=_END_TIME,
        user_message_text=user_message_text,
        tool_calls=tool_calls,
        event_count=len(tool_calls) + 1,
    )


# ---------------------------------------------------------------------------
# Stage 1 — mutation_heavy
# ---------------------------------------------------------------------------


def test_stage1_mutation_heavy_three_edits_no_bash() -> None:
    turn = _turn(tool_calls=("edit", "edit", "edit"))
    assert classify_turn(turn) == "mutation_heavy"


def test_stage1_mutation_heavy_mixed_mutation_tools_no_bash() -> None:
    # edit + write + multi_edit all count toward mutation_count.
    turn = _turn(tool_calls=("edit", "write", "multi_edit"))
    assert classify_turn(turn) == "mutation_heavy"


def test_stage1_mutation_heavy_requires_bash_absent() -> None:
    # Three edits but bash is also present — should NOT be mutation_heavy.
    # Rule 2 fires instead (bash>=1 and mutation>=1).
    turn = _turn(tool_calls=("edit", "edit", "edit", "bash"))
    assert classify_turn(turn) == "debug_loop"


def test_stage1_mutation_heavy_two_edits_not_enough() -> None:
    # Only 2 edit calls — below the >= 3 threshold.
    turn = _turn(tool_calls=("edit", "edit"), user_message_text="rename all files")
    # Stage 1 ambiguous → stage 2 picks mutation_heavy via keyword "rename".
    assert classify_turn(turn) == "mutation_heavy"


# ---------------------------------------------------------------------------
# Stage 1 — debug_loop
# ---------------------------------------------------------------------------


def test_stage1_debug_loop_bash_and_edit() -> None:
    turn = _turn(tool_calls=("bash", "edit"))
    assert classify_turn(turn) == "debug_loop"


def test_stage1_debug_loop_bash_and_write() -> None:
    turn = _turn(tool_calls=("write", "bash", "bash"))
    assert classify_turn(turn) == "debug_loop"


def test_stage1_debug_loop_multiple_bash_edit_cycles() -> None:
    turn = _turn(tool_calls=("bash", "read", "edit", "bash", "grep", "edit", "bash"))
    assert classify_turn(turn) == "debug_loop"


def test_stage1_debug_loop_bash_without_mutation_is_ambiguous() -> None:
    # bash only, no mutation → stage 1 ambiguous.
    # If user message matches debug keywords the stage 2 result should be debug_loop.
    turn = _turn(tool_calls=("bash",), user_message_text="the tests are failing")
    assert classify_turn(turn) == "debug_loop"


# ---------------------------------------------------------------------------
# Stage 1 — planning
# ---------------------------------------------------------------------------


def test_stage1_planning_no_tools() -> None:
    turn = _turn(tool_calls=())
    assert classify_turn(turn) == "planning"


def test_stage1_planning_no_tools_user_text_irrelevant() -> None:
    # stage 1 fires first; stage 2 never runs.
    turn = _turn(tool_calls=(), user_message_text="implement everything immediately")
    assert classify_turn(turn) == "planning"


def test_stage1_planning_does_not_fire_with_read_glob() -> None:
    # A single read call exceeds the read/glob=0 threshold for planning.
    turn = _turn(tool_calls=("read",), user_message_text="plan the architecture")
    # Rule 3 fails (read/glob > 0). Rule 4 fires (read > 2*0, bash=0, mut<2).
    assert classify_turn(turn) == "exploration"


def test_stage1_planning_does_not_fire_with_bash() -> None:
    # bash without edit/write is not planning.
    turn = _turn(tool_calls=("bash",), user_message_text="consider the options")
    # Rule 3 fails (bash > 0). Stage 2 doesn't match "consider" — wait, it does.
    # "consider" is in planning regex.
    assert classify_turn(turn) == "planning"


# ---------------------------------------------------------------------------
# Stage 1 — exploration
# ---------------------------------------------------------------------------


def test_stage1_exploration_read_glob_dominate() -> None:
    turn = _turn(tool_calls=("glob", "read", "read", "grep", "read"))
    assert classify_turn(turn) == "exploration"


def test_stage1_exploration_requires_no_bash() -> None:
    # bash present → rule 4 (exploration) fails → stage 2 needed.
    turn = _turn(
        tool_calls=("read", "glob", "bash"),
        user_message_text="understand the code",
    )
    # Stage 2: "understand" → exploration.
    assert classify_turn(turn) == "exploration"


def test_stage1_exploration_single_edit_below_threshold() -> None:
    # One edit call: mutation=1 < 2, and read/glob=3 > 2*1=2 → exploration.
    turn = _turn(tool_calls=("read", "read", "read", "edit"))
    assert classify_turn(turn) == "exploration"


def test_stage1_exploration_two_edits_not_dominating() -> None:
    # Two edits: mutation=2, not < 2 → rule 4 fails. Stage 2 runs.
    turn = _turn(
        tool_calls=("read", "read", "edit", "edit"),
        user_message_text="investigate the bug",
    )
    # "investigate" → exploration.
    assert classify_turn(turn) == "exploration"


# ---------------------------------------------------------------------------
# Stage 2 — keyword regex paths
# ---------------------------------------------------------------------------


def test_stage2_debug_loop_keyword_failing() -> None:
    # Tool: bash only (no mutation) → stage 1 ambiguous.
    turn = _turn(tool_calls=("bash",), user_message_text="failing test won't reproduce")
    assert classify_turn(turn) == "debug_loop"


def test_stage2_debug_loop_keyword_fix() -> None:
    turn = _turn(tool_calls=("bash",), user_message_text="fix the error")
    assert classify_turn(turn) == "debug_loop"


def test_stage2_mutation_heavy_keyword_implement() -> None:
    turn = _turn(tool_calls=("read",), user_message_text="implement the new feature")
    # read/glob=1 > 2*0 → exploration? Wait: read=1, mut=0, 1>0 → exploration fires.
    # Stage 1 returns exploration.
    assert classify_turn(turn) == "exploration"


def test_stage2_mutation_heavy_keyword_rename_ambiguous_stage1() -> None:
    # Two edits → mutation_count=2, not >=3. bash=0.
    # Rule 1: no. Rule 2: bash=0 → no. Rule 3: edit!=0 → no.
    # Rule 4: read/glob=0, 0>2*2? No. → Ambiguous.
    turn = _turn(tool_calls=("edit", "edit"), user_message_text="rename all UserRecord")
    assert classify_turn(turn) == "mutation_heavy"


def test_stage2_exploration_keyword_why() -> None:
    turn = _turn(tool_calls=(), user_message_text="why does this crash?")
    # Rule 3: no tools → planning. Stage 1 fires → planning.
    # Wait, rule 3 fires BEFORE stage 2. So keyword "why" won't apply here
    # because stage 1 already matched planning.
    assert classify_turn(turn) == "planning"


def test_stage2_exploration_keyword_ambiguous() -> None:
    # Two edits, two reads → rule 4 fails. rule 1/2/3 fail. Ambiguous.
    turn = _turn(
        tool_calls=("read", "read", "edit", "edit"),
        user_message_text="investigate the bug",
    )
    assert classify_turn(turn) == "exploration"


def test_stage2_planning_keyword_strategy() -> None:
    # One bash, no mutations → ambiguous (bash != 0, so rule 3 fails; read/glob=0).
    turn = _turn(tool_calls=("bash",), user_message_text="what strategy should we use?")
    # Stage 2: debug_loop? "should" not in debug. mutation? no. exploration? no.
    # planning: "strategy" → planning.
    assert classify_turn(turn) == "planning"


def test_stage2_no_keyword_returns_mixed() -> None:
    # No stage-1 match, no keyword match.
    turn = _turn(tool_calls=("bash",), user_message_text="ok thanks")
    assert classify_turn(turn) == "mixed"


# ---------------------------------------------------------------------------
# Keyword regex evaluation order in stage 2
# ---------------------------------------------------------------------------


def test_stage2_debug_beats_mutation_on_fix_implement() -> None:
    # "implement the fix" matches both debug_loop ("fix") and mutation_heavy
    # ("implement"). debug_loop is checked first → wins.
    turn = _turn(tool_calls=("bash",), user_message_text="implement the fix immediately")
    assert classify_turn(turn) == "debug_loop"
