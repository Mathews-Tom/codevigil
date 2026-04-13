"""Validation failure modes: unknown keys, wrong types, out-of-range, bad names."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.config import ConfigError, load_config


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body.lstrip(), encoding="utf-8")
    return path


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [mystery]
        x = 1
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_key"
    assert "mystery" in exc.value.message


def test_unknown_nested_key_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        typo_key = true
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_key"
    assert "watch.typo_key" in exc.value.message


def test_wrong_type_in_file_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = "fast"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.type_mismatch"


def test_wrong_type_in_env_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            config_path=None,
            env={"CODEVIGIL_WATCH_POLL_INTERVAL": "notanumber"},
            cli_overrides={},
        )
    assert exc.value.code == "config.type_mismatch"


def test_out_of_range_poll_interval_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = -1.0
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"


def test_evict_less_than_stale_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        stale_after_seconds = 600
        evict_after_seconds = 500
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"


def test_unknown_collector_name_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [collectors]
        enabled = ["read_edit_ratio", "does_not_exist"]
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_collector"


def test_unknown_renderer_name_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [renderers]
        enabled = ["terminal", "projector"]
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_renderer"


def test_invalid_report_output_format_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [report]
        output_format = "pdf"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.invalid_output_format"


def test_cli_unknown_key_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            config_path=None,
            env={},
            cli_overrides={"watch.made_up": 1},
        )
    assert exc.value.code == "config.unknown_key"


def test_missing_explicit_config_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            config_path=tmp_path / "nope.toml",
            env={},
            cli_overrides={},
        )
    assert exc.value.code == "config.file_not_found"


def test_malformed_toml_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", "not = valid toml =\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.toml_parse_error"


def test_duplicate_collector_name_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [collectors]
        enabled = ["read_edit_ratio", "read_edit_ratio"]
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.duplicate_collector"
