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
    assert "deprecations\n" not in out
    # A few representative leaf keys with their default provenance.
    assert "watch.poll_interval = 60.0  (default)" in out
    assert "watch.root = '~/.claude/projects'  (default)" in out
    assert "watch.roots = ['~/.claude/projects']  (default)" in out
    enabled_line = (
        "collectors.enabled = ['read_edit_ratio', 'stop_phrase', 'reasoning_loop', "
        "'thinking', 'prompts']  (default)"
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


def test_config_check_shows_watch_root_deprecation_from_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "c.toml"
    config_file.write_text("[watch]\nroot = '~/legacy'\n", encoding="utf-8")
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)

    exit_code = main(["--config", str(config_file), "config", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "deprecations" in out
    assert f"file:{config_file} sets deprecated watch.root; use watch.roots instead." in out


def test_config_check_shows_watch_root_env_deprecation(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", "~/legacy")

    exit_code = main(["config", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "CODEVIGIL_WATCH_ROOT is deprecated; use CODEVIGIL_WATCH_ROOTS instead." in out


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


def test_config_check_surfaces_watch_root_scope_violation(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``config check`` must reject an outside-``$HOME`` root, not green-light it.

    Prior behaviour: ``config check`` returned 0 and ingest/watch subsequently
    failed with ``config.watch_root_scope_violation``. Parity fix: the same
    check runs in ``config check`` so users find out early.
    """

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOTS", str(outside))

    exit_code = main(["config", "check"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "watch_root_scope_violation" in err
    assert "allow_roots_outside_home" in err


def test_config_check_accepts_outside_home_with_opt_in(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for env_key in list(k for k in __import__("os").environ if k.startswith("CODEVIGIL_")):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOTS", str(outside))
    monkeypatch.setenv("CODEVIGIL_ALLOW_ROOTS_OUTSIDE_HOME", "true")

    exit_code = main(["config", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "watch.allow_roots_outside_home = True" in out


def test_bare_invocation_prints_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codevigil import __version__

    exit_code = main([])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"codevigil {__version__}" in out


def test_report_missing_path_argument_is_argparse_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``codevigil report`` requires a path argument; bare form is rejected."""

    with pytest.raises(SystemExit):
        main(["report"])
    err = capsys.readouterr().err
    assert "path" in err
