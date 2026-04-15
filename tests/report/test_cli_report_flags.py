"""End-to-end CLI tests for --group-by and --compare-periods flags.

Tests the full dispatch path from CLI argument parsing through the cohort
renderer, including:
- --group-by week produces a Markdown report with the weekly table.
- --compare-periods produces a comparison Markdown report.
- Mutual exclusivity check (both flags together returns exit 2).
- Bad date format returns exit 2.
- Existing report path (neither flag) still works unchanged.

The output directory must be under $HOME (privacy gate). Tests use
monkeypatch.setenv("HOME", ...) to point HOME at a tmp_path subdirectory,
following the same pattern as tests/cli/test_report_golden.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set HOME to a tmp_path subdirectory so the privacy gate passes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_REPORT_OUTPUT_DIR", str(home / "reports"))
    return home


def _write_minimal_session(path: Path, ts_date: str, session_id: str) -> None:
    """Write a minimal JSONL file with a read and a write tool call."""
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
def corpus_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a session corpus under home and configure HOME."""
    home = _setup_home(tmp_path, monkeypatch)
    sessions_dir = home / "sessions"
    sessions_dir.mkdir()

    # W14 (2026-03-30 .. 2026-04-05): 5 sessions to pass the n>=5 guard.
    w14_dates = ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03"]
    for i, d in enumerate(w14_dates):
        _write_minimal_session(sessions_dir / f"w14-s{i}.jsonl", d, f"w14-s{i}")

    # W15 (2026-04-06 .. 2026-04-12): 5 sessions.
    w15_dates = ["2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10"]
    for i, d in enumerate(w15_dates):
        _write_minimal_session(sessions_dir / f"w15-s{i}.jsonl", d, f"w15-s{i}")

    return sessions_dir


# ---------------------------------------------------------------------------
# --group-by week
# ---------------------------------------------------------------------------


class TestGroupByWeekCLI:
    def test_exit_code_zero(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rc = main(["report", str(corpus_dir), "--group-by", "week"])
        assert rc == 0

    def test_writes_markdown_file(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        main(["report", str(corpus_dir), "--group-by", "week"])
        report_file = home / "reports" / "cohort_week.md"
        assert report_file.exists()

    def test_output_contains_required_sections(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main(["report", str(corpus_dir), "--group-by", "week"])
        body = (tmp_path / "home" / "reports" / "cohort_week.md").read_text(encoding="utf-8")
        assert "# Cohort Trend Report" in body
        assert "## Methodology" in body
        assert "## Appendix" in body

    def test_methodology_disclaimer_present(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main(["report", str(corpus_dir), "--group-by", "week"])
        body = (tmp_path / "home" / "reports" / "cohort_week.md").read_text(encoding="utf-8")
        assert "metrics have not been validated against labeled outcomes" in body.lower()

    def test_no_banned_words_in_output(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main(["report", str(corpus_dir), "--group-by", "week"])
        body = (
            (tmp_path / "home" / "reports" / "cohort_week.md").read_text(encoding="utf-8").lower()
        )
        from codevigil.report.renderer import BANNED_CAUSAL_WORDS

        for word in BANNED_CAUSAL_WORDS:
            assert word not in body, f"Banned word {word!r} in output"

    def test_write_precision_column_present(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main(["report", str(corpus_dir), "--group-by", "week"])
        body = (tmp_path / "home" / "reports" / "cohort_week.md").read_text(encoding="utf-8")
        assert "Write Precision" in body


# ---------------------------------------------------------------------------
# --compare-periods
# ---------------------------------------------------------------------------


class TestComparePeriodsDataCLI:
    def test_exit_code_zero(
        self,
        corpus_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rc = main(
            [
                "report",
                str(corpus_dir),
                "--compare-periods",
                "2026-03-30:2026-04-05,2026-04-06:2026-04-12",
            ]
        )
        assert rc == 0

    def test_writes_markdown_file(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        main(
            [
                "report",
                str(corpus_dir),
                "--compare-periods",
                "2026-03-30:2026-04-05,2026-04-06:2026-04-12",
            ]
        )
        report_file = home / "reports" / "compare_periods.md"
        assert report_file.exists()

    def test_output_contains_required_sections(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main(
            [
                "report",
                str(corpus_dir),
                "--compare-periods",
                "2026-03-30:2026-04-05,2026-04-06:2026-04-12",
            ]
        )
        body = (tmp_path / "home" / "reports" / "compare_periods.md").read_text(encoding="utf-8")
        assert "# Period Comparison" in body
        assert "## Methodology" in body
        assert "## Appendix" in body

    def test_bad_format_returns_exit_2(
        self,
        corpus_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rc = main(
            [
                "report",
                str(corpus_dir),
                "--compare-periods",
                "bad-format",
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Mutual exclusivity
# ---------------------------------------------------------------------------


class TestMutualExclusivity:
    def test_both_flags_exit_2(
        self,
        corpus_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rc = main(
            [
                "report",
                str(corpus_dir),
                "--group-by",
                "week",
                "--compare-periods",
                "2026-03-30:2026-04-05,2026-04-06:2026-04-12",
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Existing report path unchanged
# ---------------------------------------------------------------------------


class TestOriginalReportPathUnchanged:
    def test_json_format_still_works(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Passing --from preserves the single-period path and produces report.json.
        home = tmp_path / "home"
        rc = main(["report", str(corpus_dir), "--format", "json", "--from", "2020-01-01"])
        assert rc == 0
        assert (home / "reports" / "report.json").exists()

    def test_markdown_format_still_works(
        self,
        corpus_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Passing --from preserves the single-period path and produces report.md.
        home = tmp_path / "home"
        rc = main(["report", str(corpus_dir), "--format", "markdown", "--from", "2020-01-01"])
        assert rc == 0
        assert (home / "reports" / "report.md").exists()
