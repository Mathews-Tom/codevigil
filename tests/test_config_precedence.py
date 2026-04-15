"""Precedence matrix: default → file → env → CLI, later layers win."""

from __future__ import annotations

from pathlib import Path

from codevigil.config import load_config


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body.lstrip(), encoding="utf-8")
    return path


def test_defaults_only_when_no_other_layer(tmp_path: Path) -> None:
    resolved = load_config(config_path=None, env={}, cli_overrides={})
    assert resolved.values["watch"]["poll_interval"] == 60.0
    assert resolved.sources["watch.poll_interval"] == "default"


def test_file_overrides_default(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = 5.0
        """,
    )
    resolved = load_config(config_path=path, env={}, cli_overrides={})
    assert resolved.values["watch"]["poll_interval"] == 5.0
    assert resolved.sources["watch.poll_interval"].startswith("file:")


def test_env_overrides_file(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = 5.0
        """,
    )
    resolved = load_config(
        config_path=path,
        env={"CODEVIGIL_WATCH_POLL_INTERVAL": "7.5"},
        cli_overrides={},
    )
    assert resolved.values["watch"]["poll_interval"] == 7.5
    assert resolved.sources["watch.poll_interval"] == "env:CODEVIGIL_WATCH_POLL_INTERVAL"


def test_cli_overrides_env(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = 5.0
        """,
    )
    resolved = load_config(
        config_path=path,
        env={"CODEVIGIL_WATCH_POLL_INTERVAL": "7.5"},
        cli_overrides={"watch.poll_interval": 9.0},
    )
    assert resolved.values["watch"]["poll_interval"] == 9.0
    assert resolved.sources["watch.poll_interval"].startswith("cli:")


def test_env_overrides_default_without_file() -> None:
    resolved = load_config(
        config_path=None,
        env={"CODEVIGIL_LOG_PATH": "/tmp/custom.log"},
        cli_overrides={},
    )
    assert resolved.values["logging"]["log_path"] == "/tmp/custom.log"
    assert resolved.sources["logging.log_path"] == "env:CODEVIGIL_LOG_PATH"


def test_unrelated_layers_do_not_disturb_unchanged_keys(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = 5.0
        """,
    )
    resolved = load_config(config_path=path, env={}, cli_overrides={})
    # watch.root was not overridden anywhere — it still resolves to the default.
    assert resolved.values["watch"]["root"] == "~/.claude/projects"
    assert resolved.sources["watch.root"] == "default"


def test_comma_separated_env_list(tmp_path: Path) -> None:
    resolved = load_config(
        config_path=None,
        env={"CODEVIGIL_REPORT_OUTPUT_FORMAT": "markdown"},
        cli_overrides={},
    )
    assert resolved.values["report"]["output_format"] == "markdown"


def test_int_coercion_from_env() -> None:
    resolved = load_config(
        config_path=None,
        env={"CODEVIGIL_BOOTSTRAP_SESSIONS": "25"},
        cli_overrides={},
    )
    assert resolved.values["bootstrap"]["sessions"] == 25
    assert resolved.sources["bootstrap.sessions"] == "env:CODEVIGIL_BOOTSTRAP_SESSIONS"
