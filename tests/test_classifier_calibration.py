"""Calibration gate test for the task classifier.

Loads the hand-labeled sessions from
``tests/fixtures/task_classification/labels.json``, runs each through the
full parser+TurnGrouper pipeline to produce Turn objects, classifies them via
``classify_turn``, aggregates to session level via
``aggregate_session_task_type``, and asserts that overall agreement between
the classifier and the hand labels is >=85%.

This is a HARD BUILD GATE.  If it fails, the rules must be revised before
shipping.  Never weaken the 85% threshold — if calibration cannot be achieved,
stop and consult the orchestrator (see ``.docs/codeburn-integration-plan.md``
Phase 5 risk mitigation).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from codevigil.classifier import aggregate_session_task_type, classify_turn
from codevigil.parser import SessionParser
from codevigil.turns import Turn, TurnGrouper

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "task_classification"
_LABELS_FILE = _FIXTURE_DIR / "labels.json"

_AGREEMENT_THRESHOLD = 0.85


def _parse_and_classify_session(jsonl_path: Path, session_id: str) -> list[Turn]:
    """Parse a JSONL session file and return classified Turn objects.

    Uses the full parser+TurnGrouper stack.  Each turn's ``task_type`` is
    set by calling ``classify_turn`` immediately after the turn closes.
    """
    parser = SessionParser(session_id=session_id)
    grouper = TurnGrouper(session_id=session_id)
    turns: list[Turn] = []

    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    for event in parser.parse(lines):
        raw_turn = grouper.ingest(event)
        if raw_turn is not None:
            turns.append(dataclasses.replace(raw_turn, task_type=classify_turn(raw_turn)))

    final = grouper.finalize()
    if final is not None:
        turns.append(dataclasses.replace(final, task_type=classify_turn(final)))

    return turns


def test_classifier_calibration_agreement() -> None:
    """Assert >=85% session-level agreement on the labeled corpus."""
    raw = json.loads(_LABELS_FILE.read_text(encoding="utf-8"))
    sessions = raw["sessions"]

    correct = 0
    total = len(sessions)
    failures: list[dict[str, str]] = []

    for entry in sessions:
        session_id: str = entry["session_id"]
        expected: str = entry["label"]
        fixture_path = _FIXTURE_DIR / entry["file"]

        labelled = _parse_and_classify_session(fixture_path, session_id)
        predicted = aggregate_session_task_type(labelled)

        if predicted == expected:
            correct += 1
        else:
            failures.append(
                {
                    "session_id": session_id,
                    "expected": expected,
                    "predicted": predicted,
                    "turn_labels": str([t.task_type for t in labelled]),
                }
            )

    agreement = correct / total if total > 0 else 0.0

    if failures:
        failure_lines = "\n".join(
            f"  {f['session_id']}: expected={f['expected']!r}, "
            f"predicted={f['predicted']!r}, turns={f['turn_labels']}"
            for f in failures
        )
        failure_summary = f"\nCalibration failures ({len(failures)}/{total}):\n{failure_lines}"
    else:
        failure_summary = ""

    assert agreement >= _AGREEMENT_THRESHOLD, (
        f"Classifier agreement {agreement:.1%} is below the 85% gate "
        f"({correct}/{total} sessions correct).{failure_summary}\n"
        f"Revise TOOL_SIGNATURES or KEYWORD_PATTERNS in codevigil/classifier.py "
        f"and re-run scripts/calibrate_classifier.py."
    )
