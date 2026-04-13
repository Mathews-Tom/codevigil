"""Path-scope enforcement for ``codevigil report --output``.

``report`` must refuse to write outside ``$HOME`` so the privacy rule is
consistent with the watcher and JsonFileRenderer gates. We drive the
check by pointing ``$HOME`` at a tmp subdirectory and passing an
``--output`` path outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.cli import main

from ._fixtures import write_fixture_session


def test_report_output_outside_home_exits_critical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "elsewhere"
    home.mkdir()
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))

    fixture = write_fixture_session(home / "session.jsonl")

    exit_code = main(["report", str(fixture), "--output", str(outside)])
    assert exit_code == 2

    err = capsys.readouterr().err
    assert "path_scope_violation" in err
    assert "outside the user home directory" in err
