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


def test_empty_watch_roots_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        roots = []
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.empty_watch_roots"


def test_empty_watch_roots_env_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            config_path=None,
            env={"CODEVIGIL_WATCH_ROOTS": ""},
            cli_overrides={},
        )
    assert exc.value.code == "config.empty_watch_roots"


# ---------------------------------------------------------------------------
# classifier section
# ---------------------------------------------------------------------------


def test_classifier_defaults_are_valid() -> None:
    """Default config with no classifier section resolves without error."""
    cfg = load_config(config_path=None, env={}, cli_overrides={})
    assert cfg.values["classifier"]["enabled"] is True
    assert cfg.values["classifier"]["experimental"] is True


def test_classifier_enabled_false_accepted(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [classifier]
        enabled = false
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    assert cfg.values["classifier"]["enabled"] is False


def test_classifier_experimental_false_accepted(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [classifier]
        experimental = false
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    assert cfg.values["classifier"]["experimental"] is False


def test_classifier_enabled_wrong_type_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [classifier]
        enabled = "yes"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.type_mismatch"
    assert "classifier.enabled" in exc.value.message


def test_classifier_experimental_wrong_type_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [classifier]
        experimental = 1
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.type_mismatch"
    assert "classifier.experimental" in exc.value.message


def test_classifier_unknown_key_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [classifier]
        unknown_option = true
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_key"
    assert "classifier.unknown_option" in exc.value.message


# ---------------------------------------------------------------------------
# watch.display_limit validation
# ---------------------------------------------------------------------------


def test_display_limit_default_is_20() -> None:
    cfg = load_config(config_path=None, env={}, cli_overrides={})
    assert cfg.values["watch"]["display_limit"] == 20


def test_display_limit_boundary_one_accepted(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = 1
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    assert cfg.values["watch"]["display_limit"] == 1


def test_display_limit_boundary_500_accepted(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = 500
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    assert cfg.values["watch"]["display_limit"] == 500


def test_display_limit_zero_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = 0
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"
    assert "watch.display_limit" in exc.value.message


def test_display_limit_negative_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = -1
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"


def test_display_limit_501_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = 501
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"


def test_display_limit_string_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        display_limit = "twenty"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=path, env={}, cli_overrides={})
    assert exc.value.code == "config.type_mismatch"
    assert "watch.display_limit" in exc.value.message


def test_display_limit_env_binding_accepted() -> None:
    cfg = load_config(
        config_path=None,
        env={"CODEVIGIL_WATCH_DISPLAY_LIMIT": "50"},
        cli_overrides={},
    )
    assert cfg.values["watch"]["display_limit"] == 50


def test_display_limit_env_binding_invalid_string_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            config_path=None,
            env={"CODEVIGIL_WATCH_DISPLAY_LIMIT": "twenty"},
            cli_overrides={},
        )
    assert exc.value.code == "config.type_mismatch"


def test_existing_config_without_display_limit_loads_unchanged(tmp_path: Path) -> None:
    """A config file that predates display_limit loads with the default value."""
    path = _write_config(
        tmp_path / "config.toml",
        """
        [watch]
        poll_interval = 3.0
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    assert cfg.values["watch"]["display_limit"] == 20
    assert cfg.values["watch"]["poll_interval"] == 3.0
