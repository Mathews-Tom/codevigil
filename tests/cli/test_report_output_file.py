"""``codevigil report --output-file`` explicit-file destination."""

from __future__ import annotations

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


def test_output_file_markdown_single_period_writes_exact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")
    target = home / "custom" / "my_report.md"

    exit_code = main(
        [
            "report",
            str(fixture),
            "--format",
            "markdown",
            "--from",
            "2020-01-01",
            "--output-file",
            str(target),
        ]
    )
    assert exit_code == 0
    assert target.exists()
    body = target.read_text()
    assert body.strip(), "expected markdown body in output file"
    # Default filenames must NOT be created when --output-file is supplied.
    assert not (home / "reports" / "report.md").exists()
    assert not (home / "reports" / "report.json").exists()

    captured = capsys.readouterr().out
    assert captured.strip() == body.strip()


def test_output_file_multi_period_markdown_writes_exact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")
    target = home / "out" / "multi.md"

    exit_code = main(
        [
            "report",
            str(fixture),
            "--format",
            "markdown",
            "--output-file",
            str(target),
        ]
    )
    assert exit_code == 0
    assert target.exists()
    assert target.read_text().strip()
    assert not (home / "reports" / "report_multi_period.txt").exists()


def test_output_file_conflicts_with_output_dir_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")

    exit_code = main(
        [
            "report",
            str(fixture),
            "--from",
            "2020-01-01",
            "--output",
            str(home / "dir"),
            "--output-file",
            str(home / "dir" / "report.md"),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "flag_conflict" in err


def test_output_file_outside_home_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    fixture = write_fixture_session(home / "session.jsonl")
    outside = tmp_path / "outside" / "report.md"

    exit_code = main(
        [
            "report",
            str(fixture),
            "--from",
            "2020-01-01",
            "--output-file",
            str(outside),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "path_scope_violation" in err
    assert not outside.exists()
