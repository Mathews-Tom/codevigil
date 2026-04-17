"""Precedence matrix: default → file → env → CLI, later layers win."""

from __future__ import annotations

import os
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
    assert resolved.values["watch"]["roots"] == ["~/.claude/projects"]
    assert resolved.sources["watch.roots"] == "default"


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


def test_file_watch_roots_derives_legacy_watch_root(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        roots = ["~/a", "~/b"]
        """,
    )
    resolved = load_config(config_path=path, env={}, cli_overrides={})
    assert resolved.values["watch"]["roots"] == ["~/a", "~/b"]
    assert resolved.values["watch"]["root"] == "~/a"
    assert resolved.sources["watch.roots"].startswith("file:")
    assert resolved.sources["watch.root"] == resolved.sources["watch.roots"]


def test_env_watch_roots_uses_os_pathsep_and_derives_watch_root() -> None:
    resolved = load_config(
        config_path=None,
        env={"CODEVIGIL_WATCH_ROOTS": f"~/a{os.pathsep}~/b"},
        cli_overrides={},
    )
    assert resolved.values["watch"]["roots"] == ["~/a", "~/b"]
    assert resolved.values["watch"]["root"] == "~/a"
    assert resolved.sources["watch.roots"] == "env:CODEVIGIL_WATCH_ROOTS"
    assert resolved.sources["watch.root"] == "env:CODEVIGIL_WATCH_ROOTS"
    assert resolved.deprecations == ()


def test_higher_precedence_watch_root_overrides_lower_precedence_watch_roots(
    tmp_path: Path,
) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        roots = ["~/a", "~/b"]
        """,
    )
    resolved = load_config(
        config_path=path,
        env={"CODEVIGIL_WATCH_ROOT": "~/override"},
        cli_overrides={},
    )
    assert resolved.values["watch"]["root"] == "~/override"
    assert resolved.values["watch"]["roots"] == ["~/override"]
    assert resolved.sources["watch.root"] == "env:CODEVIGIL_WATCH_ROOT"
    assert resolved.sources["watch.roots"] == "env:CODEVIGIL_WATCH_ROOT"
    assert resolved.deprecations == (
        "CODEVIGIL_WATCH_ROOT is deprecated; use CODEVIGIL_WATCH_ROOTS instead.",
    )


def test_same_layer_prefers_watch_roots_over_legacy_watch_root(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        root = "~/legacy"
        roots = ["~/canon-a", "~/canon-b"]
        """,
    )
    resolved = load_config(config_path=path, env={}, cli_overrides={})
    assert resolved.values["watch"]["root"] == "~/canon-a"
    assert resolved.values["watch"]["roots"] == ["~/canon-a", "~/canon-b"]
    assert resolved.deprecations == (
        f"file:{path} sets deprecated watch.root; use watch.roots instead.",
    )
