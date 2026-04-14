"""Regression: deduplication must be a no-op on clean-fixture input.

Loads the Phase 0 baseline JSON (tests/fixtures/_baseline_pre_dedup.json)
and verifies that running the current loader over the same clean fixtures
produces bit-identical field values for each session. The baseline was
captured before dedup existed; any divergence on clean input means the
dedup logic incorrectly suppressed events that had unique IDs.

Sessions with expected duplicates (intra_file_duplicates, cross_file_a,
cross_file_b) are intentionally excluded because their post-dedup numbers
will differ from the pre-dedup baseline — that is the desired correctness
improvement, not a regression.

The script that produced the baseline (scripts/capture_baseline_metrics.py)
is not re-invoked here — the baseline JSON is the stable reference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.report.loader import load_reports_from_jsonl

_FIXTURES_ROOT = Path(__file__).parent / "fixtures"
_BASELINE = _FIXTURES_ROOT / "_baseline_pre_dedup.json"

# Sessions that will deliberately differ from the baseline after dedup lands
# (they contain real duplicates). Do NOT include them in the regression check.
_SKIP_SESSIONS: frozenset[str] = frozenset(
    {
        "intra_file_duplicates",
        "cross_file_a",
        "cross_file_b",
    }
)

# Fields to compare bit-identically. These are all the fields the baseline
# script captured. We compare them one by one with precise assertions so
# failures identify exactly which field drifted.
_SCALAR_FIELDS: tuple[str, ...] = (
    "session_id",
    "project_hash",
    "project_name",
    "model",
    "permission_mode",
    "started_at",
    "ended_at",
    "duration_seconds",
    "event_count",
    "parse_confidence",
    "eviction_churn",
    "cohort_size",
    "schema_version",
)


def _load_baseline() -> dict[str, dict[str, object]]:
    with _BASELINE.open(encoding="utf-8") as fh:
        data: dict[str, object] = json.load(fh)
    reports = data.get("reports", {})
    assert isinstance(reports, dict)
    return reports  # type: ignore[return-value]


def _collect_clean_fixture_paths() -> list[Path]:
    """Return JSONL paths for all fixtures that have no intentional duplicates."""
    all_dirs = [
        _FIXTURES_ROOT / "duplicate_messages",
        _FIXTURES_ROOT / "midnight_straddle",
        _FIXTURES_ROOT / "task_classification",
    ]
    paths: list[Path] = []
    for d in all_dirs:
        for p in sorted(d.rglob("*.jsonl")):
            if p.stem not in _SKIP_SESSIONS:
                paths.append(p)
    return paths


@pytest.fixture(scope="module")
def baseline() -> dict[str, dict[str, object]]:
    return _load_baseline()


@pytest.fixture(scope="module")
def current_reports() -> dict[str, object]:
    paths = _collect_clean_fixture_paths()
    assert paths, "no clean JSONL fixture paths found — check fixture directories"
    reports = load_reports_from_jsonl(paths)
    return {r.session_id: r for r in reports}


def _session_ids_to_check(baseline: dict[str, dict[str, object]]) -> list[str]:
    return sorted(k for k in baseline if k not in _SKIP_SESSIONS)


class TestBaselineRegression:
    """Parametrised regression: one test per clean session from the baseline."""

    @pytest.mark.parametrize("session_id", _session_ids_to_check(_load_baseline()))
    def test_scalar_fields_match_baseline(
        self,
        session_id: str,
        baseline: dict[str, dict[str, object]],
        current_reports: dict[str, object],
    ) -> None:
        """Each scalar field of the session report must match the baseline value."""
        assert session_id in current_reports, (
            f"session '{session_id}' was in baseline but not produced by the loader; "
            "dedup may have incorrectly suppressed events"
        )
        report = current_reports[session_id]
        expected: dict[str, object] = baseline[session_id]

        for field in _SCALAR_FIELDS:
            if field not in expected:
                continue  # baseline may not have all fields; skip missing ones
            # SessionReport exposes datetime properties as ISO strings via
            # ._data; access the raw _data value for comparison.
            raw_value = report._data.get(field)  # type: ignore[union-attr]
            assert raw_value == expected[field], (
                f"session '{session_id}': field '{field}' diverged from baseline. "
                f"baseline={expected[field]!r}, current={raw_value!r}"
            )

    @pytest.mark.parametrize("session_id", _session_ids_to_check(_load_baseline()))
    def test_metrics_match_baseline(
        self,
        session_id: str,
        baseline: dict[str, dict[str, object]],
        current_reports: dict[str, object],
    ) -> None:
        """Collector metric values must match the baseline exactly."""
        assert session_id in current_reports
        report = current_reports[session_id]
        expected_metrics: dict[str, float] = baseline[session_id].get("metrics", {})  # type: ignore[assignment]
        actual_metrics: dict[str, float] = report._data.get("metrics", {})  # type: ignore[union-attr]

        for metric_name, expected_value in expected_metrics.items():
            assert metric_name in actual_metrics, (
                f"session '{session_id}': metric '{metric_name}' present in baseline "
                "but missing from current output"
            )
            assert actual_metrics[metric_name] == pytest.approx(expected_value, rel=1e-9), (
                f"session '{session_id}': metric '{metric_name}' diverged. "
                f"baseline={expected_value!r}, current={actual_metrics[metric_name]!r}"
            )
