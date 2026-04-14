"""Acceptance tests for the watch-lifecycle fixture corpus.

Verifies:
1. All eight fixture JSONL files exist and are non-empty.
2. The parser reads every fixture without raising.
3. stamp_watch_fixtures() sets mtime within tolerance of the intended age offset.

These tests do not classify lifecycle state; they only confirm the fixture
corpus itself is structurally sound and the factory produces correctly
back-dated copies.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from codevigil.parser import SessionParser
from tests.fixtures._watch_factory import FIXTURE_AGES, stamp_watch_fixtures

# ---------------------------------------------------------------------------
# Paths to fixture directories
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent
_COLD_REPLAY_DIR = _FIXTURES_DIR / "watch_cold_replay"
_SCHEMA_DRIFT_DIR = _FIXTURES_DIR / "parser_schema_drift"

# ---------------------------------------------------------------------------
# Expected fixture file stems
# ---------------------------------------------------------------------------

_COLD_REPLAY_STEMS = [
    "fresh_active",
    "recently_silent",
    "stale",
    "evicted_hours",
    "evicted_days",
]

_SCHEMA_DRIFT_STEMS = [
    "pre_v1_no_type",
    "pre_v1_no_timestamp",
    "pre_v1_flat_content",
]

# Tolerance for mtime comparison: the factory call and the stat() read take
# some time, so we allow a 2-second window before raising.
_MTIME_TOLERANCE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Fixture corpus existence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", _COLD_REPLAY_STEMS)
def test_cold_replay_fixture_exists(stem: str) -> None:
    path = _COLD_REPLAY_DIR / f"{stem}.jsonl"
    assert path.exists(), f"Missing cold-replay fixture: {path}"
    assert path.stat().st_size > 0, f"Empty cold-replay fixture: {path}"


@pytest.mark.parametrize("stem", _SCHEMA_DRIFT_STEMS)
def test_schema_drift_fixture_exists(stem: str) -> None:
    path = _SCHEMA_DRIFT_DIR / f"{stem}.jsonl"
    assert path.exists(), f"Missing schema-drift fixture: {path}"
    assert path.stat().st_size > 0, f"Empty schema-drift fixture: {path}"


# ---------------------------------------------------------------------------
# Parser reads fixtures without raising
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", _COLD_REPLAY_STEMS)
def test_cold_replay_fixture_is_parseable(stem: str) -> None:
    """Parser must not raise on any cold-replay fixture."""
    path = _COLD_REPLAY_DIR / f"{stem}.jsonl"
    parser = SessionParser(session_id=stem)
    events = list(parser.parse(path.read_text().splitlines()))
    # Cold-replay fixtures use the modern 2026-03 Claude Code schema, so the
    # parser should emit at least one event per file.
    assert len(events) > 0, f"Parser emitted no events for {stem}"


@pytest.mark.parametrize("stem", _SCHEMA_DRIFT_STEMS)
def test_schema_drift_fixture_is_readable(stem: str) -> None:
    """Parser must not raise on schema-drift fixtures.

    These fixtures intentionally use historical shapes that the parser does
    not yet fully recognise. This test only asserts the parser processes every
    line without an exception. It does not assert high parse_confidence —
    that assertion lives with the parser shape tests.
    """
    path = _SCHEMA_DRIFT_DIR / f"{stem}.jsonl"
    parser = SessionParser(session_id=stem)
    # Consume the full generator — any uncaught exception surfaces here.
    list(parser.parse(path.read_text().splitlines()))


# ---------------------------------------------------------------------------
# Factory mtime accuracy
# ---------------------------------------------------------------------------


def test_stamp_watch_fixtures_returns_all_stems(tmp_path: Path) -> None:
    result = stamp_watch_fixtures(tmp_path)
    assert set(result.keys()) == set(FIXTURE_AGES.keys())


@pytest.mark.parametrize("stem", _COLD_REPLAY_STEMS)
def test_stamp_watch_fixtures_mtime_matches_intended_age(stem: str, tmp_path: Path) -> None:
    """os.stat().st_mtime must be within tolerance of time.time() - intended age."""
    before = time.time()
    result = stamp_watch_fixtures(tmp_path)
    after = time.time()

    path = result[stem]
    actual_mtime = os.stat(path).st_mtime
    intended_age = FIXTURE_AGES[stem]

    # The file's mtime should represent a point in the past that is
    # (now - intended_age).  We bracket with before/after to avoid
    # clock skew from the copy itself.
    earliest_expected = before - intended_age - _MTIME_TOLERANCE_SECONDS
    latest_expected = after - intended_age + _MTIME_TOLERANCE_SECONDS

    assert earliest_expected <= actual_mtime <= latest_expected, (
        f"{stem}: mtime={actual_mtime:.3f} not in "
        f"[{earliest_expected:.3f}, {latest_expected:.3f}] "
        f"(intended age={intended_age}s)"
    )


def test_stamp_watch_fixtures_does_not_mutate_source(tmp_path: Path) -> None:
    """Source fixture mtime must not change when the factory runs."""
    from tests.fixtures._watch_factory import _SOURCE_DIR

    src_mtimes = {
        p.name: os.stat(p).st_mtime for p in _SOURCE_DIR.iterdir() if p.suffix == ".jsonl"
    }

    stamp_watch_fixtures(tmp_path)

    for name, original_mtime in src_mtimes.items():
        current_mtime = os.stat(_SOURCE_DIR / name).st_mtime
        assert current_mtime == original_mtime, (
            f"Source fixture {name} mtime changed after stamp_watch_fixtures()"
        )
