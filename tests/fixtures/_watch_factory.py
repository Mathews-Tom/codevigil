"""Fixture factory for watch lifecycle tests.

Copies watch_cold_replay JSONL files to a temporary directory and back-dates
their ``mtime`` via ``os.utime()`` so ``_run_lifecycle_pass`` sees a realistic
on-disk age without any ``time.sleep``.

The factory also rewrites JSONL event timestamps inside each copy so they are
consistent with the file's intended age.  Each event in the source file has a
timestamp offset from a fixed base time; ``stamp_watch_fixtures`` replaces
that base with ``datetime.now(UTC) - intended_age`` so the aggregator sees
genuinely old event timestamps rather than fixed-date strings that may lie in
the future or distant past relative to the test's runtime.

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

import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
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
    """Copy watch_cold_replay fixtures to *dest_dir*, back-date mtime and JSONL timestamps.

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

        JSONL event timestamps within each copy are also rewritten so the
        newest event timestamp equals the file's intended mtime, making the
        fixture self-consistent for aggregator lifecycle tests.

    Raises
    ------
    FileNotFoundError
        If a source fixture file is missing from ``tests/fixtures/watch_cold_replay/``.
    """

    now_wall = time.time()
    now_dt = datetime.fromtimestamp(now_wall, tz=UTC)
    result: dict[str, Path] = {}

    for stem, age_seconds in FIXTURE_AGES.items():
        src = _SOURCE_DIR / f"{stem}.jsonl"
        if not src.exists():
            raise FileNotFoundError(f"watch_cold_replay fixture missing: {src}")

        dst = dest_dir / f"{stem}.jsonl"
        shutil.copy2(src, dst)

        # Rewrite JSONL event timestamps so they are relative to the file's
        # intended age rather than the fixed date encoded in the source file.
        # Find the latest timestamp in the source file and compute a shift
        # that maps it to (now - age_seconds).  All other event timestamps
        # are shifted by the same delta so relative ordering is preserved.
        _rewrite_jsonl_timestamps(src, dst, now_dt=now_dt, age_seconds=age_seconds)

        target_mtime = now_wall - age_seconds
        os.utime(dst, (target_mtime, target_mtime))

        result[stem] = dst

    return result


def _rewrite_jsonl_timestamps(
    src: Path,
    dst: Path,
    *,
    now_dt: datetime,
    age_seconds: float,
) -> None:
    """Rewrite JSONL event ``timestamp`` fields in *dst* so the newest event
    has timestamp ``now_dt - age_seconds``.

    Reads the source file to find the latest timestamp, computes the
    required shift, then rewrites the destination file line by line.
    Lines without a ``timestamp`` field are passed through unchanged.
    """

    lines = src.read_text(encoding="utf-8").splitlines()

    # Find the latest timestamp across all events in the source.
    latest_src: datetime | None = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts_str = obj.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if latest_src is None or ts > latest_src:
            latest_src = ts

    if latest_src is None:
        # No parseable timestamps found; leave the file as copied.
        return

    # Intended newest-event time = now - age_seconds (matches the mtime).
    target_newest = now_dt - timedelta(seconds=age_seconds)
    shift = target_newest - latest_src

    # Rewrite each line with the shifted timestamp.
    out_lines: list[str] = []
    for raw in lines:
        if not raw.strip():
            out_lines.append(raw)
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            out_lines.append(raw)
            continue
        ts_str = obj.get("timestamp")
        if isinstance(ts_str, str):
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                new_ts = ts + shift
                obj["timestamp"] = new_ts.isoformat()
                raw = json.dumps(obj)
            except (ValueError, OverflowError):
                pass
        out_lines.append(raw)

    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
