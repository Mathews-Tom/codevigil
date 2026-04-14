"""Fixture factory for watch lifecycle tests.

Copies watch_cold_replay JSONL files to a temporary directory and back-dates
their ``mtime`` via ``os.utime()`` so ``_run_lifecycle_pass`` sees a realistic
on-disk age without any ``time.sleep``.

Usage
-----
::

    from tests.fixtures._watch_factory import stamp_watch_fixtures

    def test_something(tmp_path):
        paths = stamp_watch_fixtures(tmp_path)
        # paths["fresh_active"] is a Path with mtime ~30 s in the past
        # paths["recently_silent"] is a Path with mtime ~4 min in the past
        # paths["stale"] is a Path with mtime ~10 min in the past
        # paths["evicted_hours"] is a Path with mtime ~2 h in the past
        # paths["evicted_days"] is a Path with mtime ~5 days in the past

The factory never mutates the source fixture files; it always writes to the
caller-supplied ``tmp_path``.  Ages are applied at call time, so two
invocations within the same test run produce ``mtime`` values that are
internally consistent with each other.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Age offsets in seconds for each fixture.
# These map directly to the intended lifecycle classification documented in
# the watch-lifecycle fixture corpus. Do NOT change them without updating the
# companion acceptance note in .docs/watch-lifecycle-fix-plan.md.
# ---------------------------------------------------------------------------

#: Age offsets (seconds) keyed by fixture stem.
FIXTURE_AGES: dict[str, float] = {
    "fresh_active": 30.0,  # ~30 s  → ACTIVE
    "recently_silent": 4 * 60.0,  # ~4 min → ACTIVE (below stale threshold)
    "stale": 10 * 60.0,  # ~10 min → STALE
    "evicted_hours": 2 * 3600.0,  # ~2 h  → EVICTED
    "evicted_days": 5 * 86400.0,  # ~5 days → EVICTED
}

_SOURCE_DIR = Path(__file__).parent / "watch_cold_replay"


def stamp_watch_fixtures(dest_dir: Path) -> dict[str, Path]:
    """Copy watch_cold_replay fixtures to *dest_dir* and back-date their mtime.

    Parameters
    ----------
    dest_dir:
        A writable directory (typically ``tmp_path`` from a pytest fixture).
        Must already exist; the function does not create it.

    Returns
    -------
    dict[str, Path]
        Mapping from fixture stem (e.g. ``"fresh_active"``) to the copied
        file's absolute ``Path``.  ``os.stat(path).st_mtime`` on any returned
        path will be within a few milliseconds of
        ``time.time() - FIXTURE_AGES[stem]`` at the moment this function runs.

    Raises
    ------
    FileNotFoundError
        If a source fixture file is missing from ``tests/fixtures/watch_cold_replay/``.
    """

    now = time.time()
    result: dict[str, Path] = {}

    for stem, age_seconds in FIXTURE_AGES.items():
        src = _SOURCE_DIR / f"{stem}.jsonl"
        if not src.exists():
            raise FileNotFoundError(f"watch_cold_replay fixture missing: {src}")

        dst = dest_dir / f"{stem}.jsonl"
        shutil.copy2(src, dst)

        target_mtime = now - age_seconds
        os.utime(dst, (target_mtime, target_mtime))

        result[stem] = dst

    return result
