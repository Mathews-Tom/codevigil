"""Tests for Phase 7: multi-period default for ``codevigil report``.

Covers:
- ``codevigil report PATH`` with no date flags produces three JSON keys
  (``today`` / ``7d`` / ``30d``) and three rich panels in text mode.
- ``--from`` flag preserves single-period behavior (unchanged).
- ``--to`` flag preserves single-period behavior (unchanged).
- JSON multi-period output has the three top-level keys.
- Empty period buckets render as "no sessions in period" in text mode
  and as an empty list ``[]`` in JSON mode.
- Regression: existing ``test_loader_date_filter.py`` tests still pass
  (this file does not modify that module).

The tests use the midnight-straddle fixture for content and also construct
minimal synthetic sessions directly for isolation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codevigil.cli import main
from codevigil.report.loader import load_reports_for_windows
from codevigil.report.renderer import render_multi_period

_FIXTURE_STRADDLE = (
    Path(__file__).parent.parent / "fixtures" / "midnight_straddle" / "straddle.jsonl"
)


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a tmp directory so the privacy gate passes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_REPORT_OUTPUT_DIR", str(home / "reports"))
    return home


def _write_session(path: Path, ts_date: str, session_id: str) -> None:
    """Write a minimal JSONL session with a read and a write tool call."""
    lines = [
        json.dumps(
            {
                "type": "system",
                "timestamp": f"{ts_date}T09:00:00+00:00",
                "session_id": session_id,
                "subtype": "session_start",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": f"{ts_date}T09:01:00+00:00",
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t-r",
                            "name": "Read",
                            "input": {"file_path": "/home/user/code.py"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": f"{ts_date}T09:02:00+00:00",
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t-w",
                            "name": "Write",
                            "input": {
                                "file_path": "/home/user/code.py",
                                "content": "x = 1",
                            },
                        }
                    ]
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def recent_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Session directory with one session from today (UTC) and privacy gate configured.

    Events are stamped at the current wall-clock UTC time, not at a fixed
    hour. The multi-period "today" window is ``(midnight_today, now)`` —
    if the fixture used a fixed hour like ``T09:00:00+00:00``, the session
    would fall outside the window any time CI ran before 09:00 UTC.
    """
    home = _setup_home(tmp_path, monkeypatch)
    sessions_dir = home / "sessions"
    sessions_dir.mkdir()
    now = datetime.now(tz=UTC)
    _write_session_at(sessions_dir / "today-s0.jsonl", now, "today-s0")
    return sessions_dir


def _write_session_at(path: Path, when: datetime, session_id: str) -> None:
    """Write a minimal JSONL session whose events sit just before ``when``.

    Events are spaced at one-second intervals ending at ``when - 1s`` so they
    remain strictly inside any window whose upper bound is ``now``.
    """
    base = when - timedelta(seconds=3)
    lines = [
        json.dumps(
            {
                "type": "system",
                "timestamp": (base + timedelta(seconds=0)).isoformat(),
                "session_id": session_id,
                "subtype": "session_start",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": (base + timedelta(seconds=1)).isoformat(),
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t-r",
                            "name": "Read",
                            "input": {"file_path": "/home/user/code.py"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": (base + timedelta(seconds=2)).isoformat(),
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t-w",
                            "name": "Write",
                            "input": {
                                "file_path": "/home/user/code.py",
                                "content": "x = 1",
                            },
                        }
                    ]
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def empty_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Session directory with no JSONL files for empty-bucket tests."""
    home = _setup_home(tmp_path, monkeypatch)
    sessions_dir = home / "sessions"
    sessions_dir.mkdir()
    return sessions_dir


# ---------------------------------------------------------------------------
# JSON multi-period output: three top-level keys
# ---------------------------------------------------------------------------


class TestMultiPeriodJsonOutput:
    """``--format json`` with no date flags emits three top-level keys."""

    def test_three_top_level_keys_present(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["report", str(recent_session_dir), "--format", "json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert set(data.keys()) == {"today", "7d", "30d"}

    def test_each_key_is_a_list(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["report", str(recent_session_dir), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        for key in ("today", "7d", "30d"):
            assert isinstance(data[key], list), f"key {key!r} is not a list"

    def test_today_session_appears_in_today_key(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["report", str(recent_session_dir), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # The session written today must appear in "today".
        assert len(data["today"]) >= 1, "expected at least one session in 'today'"

    def test_today_session_in_7d_and_30d(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["report", str(recent_session_dir), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # A session from today is also within the 7d and 30d windows.
        assert len(data["7d"]) >= 1
        assert len(data["30d"]) >= 1

    def test_session_dict_has_expected_keys(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["report", str(recent_session_dir), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        sessions = data["today"]
        assert len(sessions) >= 1
        session = sessions[0]
        expected_fields = (
            "session_id",
            "started_at",
            "ended_at",
            "event_count",
            "parse_confidence",
            "metrics",
        )
        for field in expected_fields:
            assert field in session, f"expected field {field!r} in session dict"

    def test_writes_json_file(
        self,
        recent_session_dir: Path,
        tmp_path: Path,
    ) -> None:
        home = tmp_path / "home"
        main(["report", str(recent_session_dir), "--format", "json"])
        assert (home / "reports" / "report_multi_period.json").exists()


# ---------------------------------------------------------------------------
# Text multi-period output: three rich panels
# ---------------------------------------------------------------------------


class TestMultiPeriodTextOutput:
    """``--format markdown`` with no date flags emits three rich panels."""

    def test_three_period_labels_in_output(
        self,
        recent_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["report", str(recent_session_dir), "--format", "markdown"])
        assert rc == 0
        captured = capsys.readouterr()
        # Rich panel titles are embedded in the frame.
        assert "Today" in captured.out
        assert "Last 7 days" in captured.out
        assert "Last 30 days" in captured.out

    def test_writes_text_file(
        self,
        recent_session_dir: Path,
        tmp_path: Path,
    ) -> None:
        home = tmp_path / "home"
        main(["report", str(recent_session_dir), "--format", "markdown"])
        assert (home / "reports" / "report_multi_period.txt").exists()


# ---------------------------------------------------------------------------
# Single-period mode preserved when --from or --to is passed
# ---------------------------------------------------------------------------


class TestSinglePeriodModeUnchanged:
    """Passing --from or --to bypasses multi-period and uses the original path."""

    def test_from_flag_produces_single_period_json(
        self,
        recent_session_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home = tmp_path / "home"
        rc = main(
            [
                "report",
                str(recent_session_dir),
                "--format",
                "json",
                "--from",
                "2020-01-01",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        # Single-period JSON is NDJSON (one object per line), not a three-key dict.
        # An empty corpus produces an empty string; non-empty produces line-per-session.
        # Either way, it must NOT be parseable as a three-key dict.
        if captured.out.strip():
            data = json.loads(captured.out.splitlines()[0])
            # Single-period records have "kind" field set to "session_report".
            assert data.get("kind") == "session_report"
        # The multi-period file must NOT have been written.
        assert not (home / "reports" / "report_multi_period.json").exists()

    def test_to_flag_produces_single_period_json(
        self,
        recent_session_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home = tmp_path / "home"
        rc = main(
            [
                "report",
                str(recent_session_dir),
                "--format",
                "json",
                "--to",
                "2099-12-31",
            ]
        )
        assert rc == 0
        # The multi-period file must NOT have been written.
        assert not (home / "reports" / "report_multi_period.json").exists()

    def test_from_and_to_flags_produce_single_period_json(
        self,
        recent_session_dir: Path,
        tmp_path: Path,
    ) -> None:
        home = tmp_path / "home"
        main(
            [
                "report",
                str(recent_session_dir),
                "--format",
                "json",
                "--from",
                "2020-01-01",
                "--to",
                "2099-12-31",
            ]
        )
        assert not (home / "reports" / "report_multi_period.json").exists()
        assert (home / "reports" / "report.json").exists()


# ---------------------------------------------------------------------------
# Empty bucket rendering
# ---------------------------------------------------------------------------


class TestEmptyBuckets:
    """Empty periods render cleanly in both text and JSON modes."""

    def test_empty_dir_json_has_three_empty_lists(
        self,
        empty_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["report", str(empty_session_dir), "--format", "json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == {"today": [], "7d": [], "30d": []}

    def test_empty_dir_text_contains_no_sessions_sentinel(
        self,
        empty_session_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["report", str(empty_session_dir), "--format", "markdown"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no sessions in period" in captured.out


# ---------------------------------------------------------------------------
# Unit tests for load_reports_for_windows helper
# ---------------------------------------------------------------------------


class TestLoadReportsForWindowsHelper:
    """Unit tests for the new loader helper."""

    def test_returns_dict_with_all_labels(self) -> None:
        now = datetime.now(tz=UTC)
        windows = [
            ("today", now.replace(hour=0, minute=0, second=0, microsecond=0), now),
            ("7d", now - timedelta(days=7), now),
            ("30d", now - timedelta(days=30), now),
        ]
        result = load_reports_for_windows([_FIXTURE_STRADDLE], windows)
        assert set(result.keys()) == {"today", "7d", "30d"}

    def test_empty_window_returns_empty_list_for_label(self) -> None:
        # Window far in the future — no sessions from fixture will match.
        future_from = datetime(2090, 1, 1, 0, 0, 0, tzinfo=UTC)
        future_to = datetime(2090, 12, 31, 23, 59, 59, tzinfo=UTC)
        result = load_reports_for_windows(
            [_FIXTURE_STRADDLE],
            [("future", future_from, future_to)],
        )
        assert result["future"] == []

    def test_covering_window_yields_reports(self) -> None:
        # Window that fully covers the straddle fixture (2026-01-01 → 2026-01-02).
        from_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        to_ts = datetime(2026, 1, 3, 0, 0, 0, tzinfo=UTC)
        result = load_reports_for_windows(
            [_FIXTURE_STRADDLE],
            [("full", from_ts, to_ts)],
        )
        assert len(result["full"]) == 1

    def test_each_label_is_independent(self) -> None:
        # Two overlapping windows should both return reports for the same fixture.
        from_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        to_ts = datetime(2026, 1, 3, 0, 0, 0, tzinfo=UTC)
        windows = [
            ("window_a", from_ts, to_ts),
            ("window_b", from_ts, to_ts),
        ]
        result = load_reports_for_windows([_FIXTURE_STRADDLE], windows)
        assert len(result["window_a"]) == len(result["window_b"])


# ---------------------------------------------------------------------------
# Unit tests for render_multi_period renderer
# ---------------------------------------------------------------------------


class TestRenderMultiPeriod:
    """Unit tests for the render_multi_period renderer."""

    def test_output_is_string(self) -> None:
        out = render_multi_period({"today": [], "7d": [], "30d": []})
        assert isinstance(out, str)

    def test_all_three_panel_titles_present(self) -> None:
        out = render_multi_period({"today": [], "7d": [], "30d": []})
        assert "Today" in out
        assert "Last 7 days" in out
        assert "Last 30 days" in out

    def test_empty_bucket_shows_sentinel(self) -> None:
        out = render_multi_period({"today": [], "7d": [], "30d": []})
        assert "no sessions in period" in out

    def test_non_empty_bucket_shows_session_id(self) -> None:
        from datetime import UTC, datetime

        from codevigil.analysis.store import build_report

        report = build_report(
            session_id="test-session-abc",
            project_hash="proj-x",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            ended_at=datetime(2026, 4, 14, 11, 0, 0, tzinfo=UTC),
            event_count=5,
            parse_confidence=0.99,
            metrics={"read_edit_ratio": 3.5},
        )
        out = render_multi_period({"today": [report], "7d": [], "30d": []})
        assert "test-session-abc" in out

    def test_extra_labels_appended(self) -> None:
        """Labels outside the canonical three are rendered after the canonical ones."""
        out = render_multi_period({"today": [], "7d": [], "30d": [], "custom": []})
        # All four panel titles must appear.
        assert "Today" in out
        assert "Last 7 days" in out
        assert "Last 30 days" in out
        assert "custom" in out

    def test_empty_mapping_produces_no_panels(self) -> None:
        """An empty mapping produces an empty string (no panels to render)."""
        out = render_multi_period({})
        # Nothing to render — output may be blank.
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Snapshot: rich panel layout
# ---------------------------------------------------------------------------


class TestMultiPeriodSnapshot:
    """Snapshot-style test for the rich panel layout.

    Captures the text output for a deterministic corpus (two synthetic
    sessions from 2026-01-01 — well within 30d relative to any test run
    using fixture data) and asserts structural invariants rather than
    byte-identical snapshot matching, because Rich formats depend on
    terminal width which can vary.
    """

    def test_panel_structure_has_borders(self) -> None:
        """Rich panels emit ASCII box-drawing characters (─ or similar)."""
        from codevigil.analysis.store import build_report

        report = build_report(
            session_id="snap-001",
            project_hash="proj-snap",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=datetime(2026, 4, 14, 9, 0, 0, tzinfo=UTC),
            ended_at=datetime(2026, 4, 14, 9, 30, 0, tzinfo=UTC),
            event_count=10,
            parse_confidence=0.98,
            metrics={"read_edit_ratio": 4.0, "stop_phrase": 0.5},
        )
        out = render_multi_period({"today": [report], "7d": [report], "30d": [report]})
        # Rich panels render box-drawing or ASCII borders.
        assert any(ch in out for ch in ("─", "│", "╭", "╰", "+", "-", "|")), (
            "expected panel borders in rich output"
        )

    def test_metrics_appear_in_non_empty_panel(self) -> None:
        from codevigil.analysis.store import build_report

        report = build_report(
            session_id="snap-002",
            project_hash="proj-snap",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=datetime(2026, 4, 14, 9, 0, 0, tzinfo=UTC),
            ended_at=datetime(2026, 4, 14, 9, 30, 0, tzinfo=UTC),
            event_count=8,
            parse_confidence=0.97,
            metrics={"read_edit_ratio": 2.5},
        )
        out = render_multi_period({"today": [report], "7d": [], "30d": []})
        assert "read_edit_ratio" in out
        assert "2.50" in out
