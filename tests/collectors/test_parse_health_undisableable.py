"""parse_health is a built-in always-on collector and cannot be disabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.collectors import COLLECTORS
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.config import ConfigError, load_config


def test_parse_health_registered_in_builtin_collector_map() -> None:
    assert COLLECTORS["parse_health"] is ParseHealthCollector


def test_explicit_disable_via_file_layer_raises(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[collectors.parse_health]\nenabled = false\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path=config, env={}, cli_overrides={})
    assert excinfo.value.code == "config.parse_health_undisableable"


def test_explicit_disable_via_cli_override_raises() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            config_path=None,
            env={},
            cli_overrides={"collectors.parse_health.enabled": False},
        )
    assert excinfo.value.code == "config.parse_health_undisableable"


def test_default_layer_keeps_parse_health_enabled() -> None:
    resolved = load_config(config_path=None, env={}, cli_overrides={})
    assert resolved.values["collectors"]["parse_health"]["enabled"] is True
