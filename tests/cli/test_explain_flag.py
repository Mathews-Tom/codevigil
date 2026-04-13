"""``--explain`` surfaces stop_phrase intent annotations in report output."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.cli import main

from ._fixtures import write_fixture_session


def test_explain_annotates_report_markdown_with_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_REPORT_OUTPUT_DIR", str(home / "reports"))

    fixture = write_fixture_session(
        home / "session.jsonl",
        include_stop_phrase=True,
    )

    # Without --explain, intent must NOT appear.
    assert main(["report", str(fixture), "--format", "markdown"]) == 0
    plain_out = capsys.readouterr().out
    assert "intent:" not in plain_out

    # With --explain, intent annotation appears on the stop_phrase row.
    assert (
        main(
            [
                "--explain",
                "report",
                str(fixture),
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    explained = capsys.readouterr().out
    assert "stop_phrase" in explained
    assert "intent:" in explained
