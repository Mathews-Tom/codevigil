"""Deterministic golden-output checks for ``codevigil report``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.cli import main

from ._fixtures import write_fixture_session


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_REPORT_OUTPUT_DIR", str(home / "reports"))
    return home


def test_report_json_output_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")

    # Pass --from to activate the single-period path (no date flags → multi-period).
    exit_code = main(["report", str(fixture), "--format", "json", "--from", "2020-01-01"])
    assert exit_code == 0

    captured = capsys.readouterr().out.strip()
    assert captured, "expected json output on stdout"
    record = json.loads(captured.splitlines()[0])
    assert record["kind"] == "session_report"
    assert record["session_id"] == "session"
    assert record["event_count"] >= 4
    assert record["parse_confidence"] == pytest.approx(1.0)

    # Metrics list sorted by name and contains parse_health.
    names = [m["name"] for m in record["metrics"]]
    assert "parse_health" in names
    assert names == sorted(names)

    # Report file written to output_dir.
    report_file = home / "reports" / "report.json"
    assert report_file.exists()


def test_report_markdown_golden_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")

    # Pass --from to activate the single-period path (no date flags → multi-period).
    exit_code = main(["report", str(fixture), "--format", "markdown", "--from", "2020-01-01"])
    assert exit_code == 0

    out = capsys.readouterr().out
    # Golden fragments: stable header, session anchor, and table header.
    assert out.startswith("# codevigil report\n")
    assert "## session `session`" in out
    assert "| metric | value | severity | label |" in out
    assert "| --- | --- | --- | --- |" in out
    # parse_health row is present and reads 1.00.
    assert "| parse_health | 1.00 | OK |" in out

    # Rerun and confirm byte-identical output — the markdown path must
    # be deterministic (no timestamps, sorted sessions and metrics).
    capsys.readouterr()
    assert main(["report", str(fixture), "--format", "markdown", "--from", "2020-01-01"]) == 0
    out_second = capsys.readouterr().out
    assert out_second == out


def test_report_date_filter_drops_out_of_range_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")

    # Fixture starts at 2026-04-13; filter everything before 2027 and the
    # report should be empty.
    exit_code = main(["report", str(fixture), "--from", "2027-01-01", "--format", "json"])
    assert exit_code == 0
    assert capsys.readouterr().out == ""
