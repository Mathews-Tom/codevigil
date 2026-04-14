"""Capture baseline collector metrics for Phase 0 regression reference.

Runs the report loader over all Phase 0 fixtures and writes the collector
outputs to tests/fixtures/_baseline_pre_dedup.json.

This script is a throwaway: it will be deleted when Phase 2 lands. The
produced JSON file is the regression reference that Phase 1 and Phase 2
tests use to assert correctness improvements without regressing accepted
behaviour.

Usage:
    uv run python scripts/capture_baseline_metrics.py

No arguments. Paths are relative to the repository root (cwd must be the
repo root when you invoke it).

Zero network calls. Zero new runtime dependencies.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure the package is importable when the script is run from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from codevigil.report.loader import load_reports_from_jsonl


def _fixture_dirs() -> list[Path]:
    root = Path(__file__).parent.parent / "tests" / "fixtures"
    return [
        root / "duplicate_messages",
        root / "midnight_straddle",
        root / "task_classification",
    ]


def _collect_jsonl_paths(dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for d in dirs:
        for p in sorted(d.rglob("*.jsonl")):
            if p.is_file():
                paths.append(p)
    return paths


def _serialise_report(report: object) -> dict[str, object]:
    """Return the SessionReport's internal data dict (already JSON-serialisable).

    SessionReport stores data as a plain dict with ISO timestamp strings
    internally. Accessing the private ``_data`` attribute is intentional
    for this throwaway script — it avoids re-implementing the full
    property surface and is deleted before Phase 2 lands.
    """
    return dict(report._data)  # type: ignore[union-attr]


def main() -> None:
    output_path = Path(__file__).parent.parent / "tests" / "fixtures" / "_baseline_pre_dedup.json"

    fixture_dirs = _fixture_dirs()
    for d in fixture_dirs:
        if not d.exists():
            print(f"ERROR: fixture directory missing: {d}", file=sys.stderr)
            sys.exit(1)

    paths = _collect_jsonl_paths(fixture_dirs)
    if not paths:
        print("ERROR: no JSONL files found in fixture directories", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {len(paths)} fixture files...")
    reports = load_reports_from_jsonl(paths)

    print(f"  Loaded {len(reports)} session reports")

    baseline: dict[str, object] = {
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "fixture_count": len(paths),
        "report_count": len(reports),
        "reports": {r.session_id: _serialise_report(r) for r in reports},
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(baseline, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"Baseline written to: {output_path}")
    print(f"  Sessions captured: {', '.join(sorted(baseline['reports']))}")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
