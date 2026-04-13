"""CLI: ``codevigil config check`` renders effective config with sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.cli import main


def test_config_check_prints_defaults(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point HOME at a clean tmp dir so the loader does not pick up a real
    # user config file that would drift the snapshot.
    monkeypatch.setenv("HOME", str(tmp_path))
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)

    exit_code = main(["config", "check"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert out.startswith("codevigil config check\n")
    # A few representative leaf keys with their default provenance.
    assert "watch.poll_interval = 2.0  (default)" in out
    assert "watch.root = '~/.claude/projects'  (default)" in out
    enabled_line = (
        "collectors.enabled = ['read_edit_ratio', 'stop_phrase', 'reasoning_loop']  (default)"
    )
    assert enabled_line in out
    assert "report.output_format = 'json'  (default)" in out


def test_config_check_shows_file_source(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "c.toml"
    config_file.write_text("[watch]\npoll_interval = 6.5\n", encoding="utf-8")
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)

    exit_code = main(["--config", str(config_file), "config", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"watch.poll_interval = 6.5  (file:{config_file})" in out


def test_config_check_reports_validation_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "c.toml"
    config_file.write_text("[watch]\npoll_interval = -5\n", encoding="utf-8")
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)

    exit_code = main(["--config", str(config_file), "config", "check"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "out_of_range" in err or "poll_interval" in err


def test_bare_invocation_prints_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codevigil import __version__

    exit_code = main([])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"codevigil {__version__}" in out


def test_unwired_command_reports_not_yet_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["watch"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not wired yet" in err
