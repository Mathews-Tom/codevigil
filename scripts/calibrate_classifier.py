#!/usr/bin/env python3
"""Generate the classifier calibration report.

Reads the hand-labeled sessions from
``tests/fixtures/task_classification/labels.json``, runs each through the
full parser+TurnGrouper pipeline, classifies each turn via
``classify_turn``, aggregates to session level via
``aggregate_session_task_type``, and emits a confusion-matrix report to
``docs/classifier-calibration.md``.

Rerun this script manually whenever TOOL_SIGNATURES or KEYWORD_PATTERNS in
``codevigil/classifier.py`` change, and commit the updated calibration report
alongside the rule changes.

Usage:
    uv run python scripts/calibrate_classifier.py

Stdlib only.  No new dependencies.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

# Ensure the project root is on the path so codevigil is importable.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from codevigil.classifier import (  # noqa: E402
    TASK_CATEGORIES,
    aggregate_session_task_type,
    classify_turn,
)
from codevigil.parser import SessionParser  # noqa: E402
from codevigil.turns import Turn, TurnGrouper  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "task_classification"
_LABELS_FILE = _FIXTURE_DIR / "labels.json"
_REPORT_PATH = _REPO_ROOT / "docs" / "classifier-calibration.md"

_AGREEMENT_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------


def _parse_and_classify(jsonl_path: Path, session_id: str) -> list[Turn]:
    """Parse JSONL and return classified Turn objects."""
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


# ---------------------------------------------------------------------------
# Confusion matrix
# ---------------------------------------------------------------------------


def _build_confusion_matrix(
    sessions: list[dict[str, str]],
) -> tuple[
    dict[tuple[str, str], int],  # (actual, predicted) → count
    list[dict[str, str]],  # failures
    int,  # correct
]:
    """Run all sessions through the classifier and build a confusion matrix."""
    matrix: dict[tuple[str, str], int] = {}
    failures: list[dict[str, str]] = []
    correct = 0

    for entry in sessions:
        session_id = entry["session_id"]
        expected = entry["label"]
        fixture_path = _FIXTURE_DIR / entry["file"]

        labelled = _parse_and_classify(fixture_path, session_id)
        predicted = aggregate_session_task_type(labelled)

        key = (expected, predicted)
        matrix[key] = matrix.get(key, 0) + 1

        if predicted == expected:
            correct += 1
        else:
            turn_labels = [t.task_type or "None" for t in labelled]
            failures.append(
                {
                    "session_id": session_id,
                    "expected": expected,
                    "predicted": predicted,
                    "turn_labels": str(turn_labels),
                    "rationale": entry.get("rationale", ""),
                }
            )

    return matrix, failures, correct


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _precision_recall(
    matrix: dict[tuple[str, str], int],
    categories: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    """Compute per-category precision and recall from the confusion matrix."""
    stats: dict[str, dict[str, float]] = {}
    for cat in categories:
        tp = matrix.get((cat, cat), 0)
        fp = sum(matrix.get((actual, cat), 0) for actual in categories if actual != cat)
        fn = sum(matrix.get((cat, pred), 0) for pred in categories if pred != cat)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        stats[cat] = {"precision": precision, "recall": recall, "tp": float(tp)}
    return stats


def _render_report(
    matrix: dict[tuple[str, str], int],
    failures: list[dict[str, str]],
    correct: int,
    total: int,
) -> str:
    """Render the calibration report as a Markdown string."""
    agreement = correct / total if total > 0 else 0.0
    gate_status = "PASS" if agreement >= _AGREEMENT_THRESHOLD else "FAIL"
    categories = TASK_CATEGORIES

    lines: list[str] = []
    lines.append("# Classifier Calibration Report")
    lines.append("")
    lines.append("Generated by `scripts/calibrate_classifier.py`.")
    lines.append("Rerun whenever rules in `codevigil/classifier.py` change.")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Sessions evaluated | {total} |")
    lines.append(f"| Sessions correct | {correct} |")
    lines.append(f"| Agreement | {agreement:.1%} |")
    lines.append(f"| Gate threshold | {_AGREEMENT_THRESHOLD:.0%} |")
    lines.append(f"| Gate status | **{gate_status}** |")
    lines.append("")

    # Confusion matrix
    lines.append("## Confusion Matrix (actual rows, predicted columns)")
    lines.append("")
    header = "| Actual \\\\ Predicted | " + " | ".join(categories) + " |"
    sep = "| --- | " + " | ".join(["---"] * len(categories)) + " |"
    lines.append(header)
    lines.append(sep)
    for actual in categories:
        row_counts = [str(matrix.get((actual, pred), 0)) for pred in categories]
        row_total = sum(matrix.get((actual, pred), 0) for pred in categories)
        if row_total == 0:
            continue
        lines.append(f"| {actual} | " + " | ".join(row_counts) + " |")
    lines.append("")

    # Per-category precision and recall
    stats = _precision_recall(matrix, categories)
    lines.append("## Per-Category Precision and Recall")
    lines.append("")
    lines.append("| Category | Precision | Recall |")
    lines.append("|----------|-----------|--------|")
    for cat in categories:
        s = stats[cat]
        if s["tp"] == 0 and s["precision"] == 0 and s["recall"] == 0:
            lines.append(f"| {cat} | — | — |")
        else:
            lines.append(f"| {cat} | {s['precision']:.1%} | {s['recall']:.1%} |")
    lines.append("")

    # Failures
    if failures:
        lines.append("## Misclassified Sessions")
        lines.append("")
        for f in failures:
            lines.append(f"### `{f['session_id']}`")
            lines.append("")
            lines.append(f"- **Expected:** `{f['expected']}`")
            lines.append(f"- **Predicted:** `{f['predicted']}`")
            lines.append(f"- **Turn labels:** `{f['turn_labels']}`")
            lines.append(f"- **Rationale:** {f['rationale']}")
            lines.append("")
    else:
        lines.append("## Misclassified Sessions")
        lines.append("")
        lines.append("None — all sessions classified correctly.")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    raw = json.loads(_LABELS_FILE.read_text(encoding="utf-8"))
    sessions = raw["sessions"]
    total = len(sessions)

    print(f"Calibrating classifier against {total} labeled sessions...")

    matrix, failures, correct = _build_confusion_matrix(sessions)
    agreement = correct / total if total > 0 else 0.0

    report_text = _render_report(matrix, failures, correct, total)
    _REPORT_PATH.write_text(report_text, encoding="utf-8")

    print(f"Agreement: {correct}/{total} sessions = {agreement:.1%}")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f['session_id']}: expected={f['expected']!r}, predicted={f['predicted']!r}")
    else:
        print("All sessions correct.")

    gate_pass = agreement >= _AGREEMENT_THRESHOLD
    if gate_pass:
        print(f"Gate: PASS (>= {_AGREEMENT_THRESHOLD:.0%})")
    else:
        print(f"Gate: FAIL (< {_AGREEMENT_THRESHOLD:.0%}) — rules must be revised")

    print(f"\nReport written to {_REPORT_PATH}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
