"""Full-pipeline integration tests against the v0.1 fixture corpus.

Each fixture under ``tests/fixtures/sessions/`` is parsed via
:class:`SessionParser` and fed through one instance of every v0.1
collector. The expected severities below come from the sibling
``<fixture>.md`` description; adding a new fixture to the corpus is a
single row addition here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.collectors.read_edit_ratio import ReadEditRatioCollector
from codevigil.collectors.reasoning_loop import ReasoningLoopCollector
from codevigil.collectors.stop_phrase import StopPhraseCollector
from codevigil.parser import SessionParser
from codevigil.types import Severity

FIXTURES_DIR: Path = Path(__file__).resolve().parent.parent / "fixtures" / "sessions"


# (fixture stem, expected severities keyed by collector name)
_FIXTURE_EXPECTATIONS: tuple[tuple[str, dict[str, Severity]], ...] = (
    (
        "healthy_session",
        {
            "read_edit_ratio": Severity.OK,
            "stop_phrase": Severity.OK,
            "reasoning_loop": Severity.OK,
            "parse_health": Severity.OK,
        },
    ),
    (
        "degraded_re",
        {
            "read_edit_ratio": Severity.CRITICAL,
            "stop_phrase": Severity.OK,
            "reasoning_loop": Severity.OK,
            "parse_health": Severity.OK,
        },
    ),
    (
        "stop_phrase_triggered",
        {
            "read_edit_ratio": Severity.OK,
            "stop_phrase": Severity.CRITICAL,
            "reasoning_loop": Severity.OK,
            "parse_health": Severity.OK,
        },
    ),
    (
        "schema_drift",
        {
            "read_edit_ratio": Severity.OK,
            "stop_phrase": Severity.OK,
            "reasoning_loop": Severity.OK,
            "parse_health": Severity.CRITICAL,
        },
    ),
    (
        "mixed_calibration",
        {
            "read_edit_ratio": Severity.OK,
            "stop_phrase": Severity.OK,
            "reasoning_loop": Severity.OK,
            "parse_health": Severity.OK,
        },
    ),
)


def _run_pipeline(fixture_path: Path) -> dict[str, Severity]:
    parser = SessionParser(session_id=fixture_path.stem)
    parse_health = ParseHealthCollector(stats=parser.stats)
    read_edit = ReadEditRatioCollector()
    stop_phrase = StopPhraseCollector()
    reasoning_loop = ReasoningLoopCollector()
    collectors = (parse_health, read_edit, stop_phrase, reasoning_loop)
    with fixture_path.open("r", encoding="utf-8") as handle:
        for event in parser.parse(handle):
            for collector in collectors:
                collector.ingest(event)
    return {c.name: c.snapshot().severity for c in collectors}


@pytest.mark.parametrize(("fixture_stem", "expected"), _FIXTURE_EXPECTATIONS)
def test_fixture_pipeline_severities(fixture_stem: str, expected: dict[str, Severity]) -> None:
    fixture_path = FIXTURES_DIR / f"{fixture_stem}.jsonl"
    assert fixture_path.exists(), f"missing fixture: {fixture_path}"
    actual = _run_pipeline(fixture_path)
    for collector_name, expected_severity in expected.items():
        assert actual[collector_name] == expected_severity, (
            f"{fixture_stem}/{collector_name}: "
            f"expected {expected_severity.value}, got {actual[collector_name].value}"
        )
