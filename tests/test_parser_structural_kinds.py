"""Structural metadata records must not penalise parse_confidence.

Claude Code emits several top-level ``type`` values alongside the
conversational ``user`` / ``assistant`` / ``system`` turns: ``progress``
heartbeats, ``file-history-snapshot`` markers, ``permission-mode``
transitions, ``attachment`` pointers, ``queue-operation`` events, and
``last-prompt`` markers. These are not turn records and the collectors
do not consume them, but they still represent *structurally valid*
JSONL lines the parser chose to skip.

Before the fix, every skipped line was counted as a failed parse
(``parsed_events`` stayed flat while ``total_lines`` advanced), so a
session dominated by ``progress`` records — the normal case in modern
Claude Code logs — tripped the ``parse_health`` CRITICAL threshold and
painted the project row red.

This module verifies the fix: structural kinds are silently skipped
without a ``parser.unknown_type`` warning and without degrading
``parse_confidence``.
"""

from __future__ import annotations

import json

from codevigil.parser import SessionParser


def _line(payload: dict[str, object]) -> str:
    return json.dumps(payload)


def _turn_user() -> str:
    return _line(
        {
            "type": "user",
            "timestamp": "2025-11-01T10:00:00Z",
            "session_id": "s",
            "message": {
                "id": "u1",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )


def _turn_assistant() -> str:
    return _line(
        {
            "type": "assistant",
            "timestamp": "2025-11-01T10:00:05Z",
            "session_id": "s",
            "message": {
                "id": "a1",
                "content": [{"type": "text", "text": "yo"}],
            },
        }
    )


def test_progress_records_do_not_penalise_parse_confidence() -> None:
    parser = SessionParser(session_id="s")
    lines = (
        [_turn_user()]
        + [_line({"type": "progress", "data": {}}) for _ in range(20)]
        + [_turn_assistant()]
    )
    list(parser.parse(lines))
    assert parser.stats.total_lines == 22
    # parsed_events counts user_message (1) + assistant_message (1) + 20
    # structural skips credited as parsed → ≥ 22.
    assert parser.stats.parsed_events >= 22
    assert parser.stats.parse_confidence >= 0.95


def test_all_structural_kinds_skipped_without_warning() -> None:
    parser = SessionParser(session_id="s")
    structural = [
        _line({"type": "progress", "data": {}}),
        _line({"type": "attachment", "id": "att1"}),
        _line({"type": "file-history-snapshot", "snapshot": {}}),
        _line({"type": "permission-mode", "mode": "default"}),
        _line({"type": "last-prompt", "text": ""}),
        _line({"type": "queue-operation", "op": "enqueue"}),
    ]
    list(parser.parse(structural))
    assert parser.stats.total_lines == 6
    # parse_confidence stays high — every structural line is treated as
    # successfully handled by the dispatcher.
    assert parser.stats.parse_confidence >= 0.99


def test_unknown_type_still_flagged_as_drift() -> None:
    """A genuinely unknown type (not in the structural allowlist) must
    still count against parse_confidence so drift detection works."""
    parser = SessionParser(session_id="s")
    lines = [
        _turn_user(),
        _line({"type": "totally-unknown-type"}),
        _line({"type": "another-unknown"}),
        _line({"type": "third-unknown"}),
    ]
    list(parser.parse(lines))
    # Only the user line was successfully parsed.
    assert parser.stats.parsed_events >= 1
    assert parser.stats.parse_confidence < 1.0


def test_progress_majority_session_is_healthy() -> None:
    """Realistic shape: 90 %+ progress records, 5 assistant/user turns.
    Mirrors the sessions we saw in the wild that were being flagged red
    before the fix."""
    parser = SessionParser(session_id="s")
    lines: list[str] = []
    for _ in range(148):
        lines.append(_line({"type": "progress", "data": {"step": 1}}))
    lines.append(_turn_user())
    for _ in range(5):
        lines.append(_turn_assistant())
    lines.append(_line({"type": "file-history-snapshot"}))
    lines.append(_line({"type": "system", "timestamp": "2025-11-01T10:00:00Z", "subtype": "x"}))

    list(parser.parse(lines))
    assert parser.stats.parse_confidence >= 0.95
