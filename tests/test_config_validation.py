"""Validation failure modes: unknown keys, wrong types, out-of-range, bad names."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codevigil.config import ConfigError, load_config, resolve_watch_roots


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


def test_resolve_watch_roots_rejects_overlapping_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    nested = home / ".claude" / "projects" / "team-a"
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    resolved = load_config(
        config_path=None,
        env={},
        cli_overrides={
            "watch.roots": [
                str(home / ".claude" / "projects"),
                str(nested),
            ]
        },
    )

    with pytest.raises(ConfigError) as exc:
        resolve_watch_roots(resolved.values)
    assert exc.value.code == "config.overlapping_watch_roots"


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


def test_resolve_watch_roots_deduplicates_equivalent_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    cfg = load_config(
        config_path=None,
        env={"CODEVIGIL_WATCH_ROOTS": f"{root}{os.pathsep}{root / '..' / 'projects'}"},
        cli_overrides={},
    )
    descriptors = resolve_watch_roots(cfg.values)
    assert len(descriptors) == 1
    assert descriptors[0].root_path == root.resolve()


def test_resolve_watch_roots_rejects_path_outside_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = load_config(
        config_path=None,
        env={"CODEVIGIL_WATCH_ROOT": str(outside)},
        cli_overrides={},
    )
    with pytest.raises(ConfigError) as exc:
        resolve_watch_roots(cfg.values)
    assert exc.value.code == "config.watch_root_scope_violation"
    assert "allow_roots_outside_home" in exc.value.message


def test_allow_roots_outside_home_defaults_to_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    cfg = load_config(config_path=None, env={}, cli_overrides={})
    assert cfg.values["watch"]["allow_roots_outside_home"] is False


def test_allow_roots_outside_home_toml_opt_in_permits_outside_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _write_config(
        tmp_path / "config.toml",
        f"""
        [watch]
        roots = [{str(outside)!r}]
        allow_roots_outside_home = true
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    descriptors = resolve_watch_roots(cfg.values)
    assert len(descriptors) == 1
    assert descriptors[0].root_path == outside.resolve()


def test_allow_roots_outside_home_env_opt_in_permits_outside_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = load_config(
        config_path=None,
        env={
            "CODEVIGIL_WATCH_ROOTS": str(outside),
            "CODEVIGIL_ALLOW_ROOTS_OUTSIDE_HOME": "true",
        },
        cli_overrides={},
    )
    descriptors = resolve_watch_roots(cfg.values)
    assert len(descriptors) == 1
    assert descriptors[0].root_path == outside.resolve()


def test_allow_roots_outside_home_opt_in_still_rejects_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside_parent = tmp_path / "outside"
    outside_child = outside_parent / "nested"
    outside_child.mkdir(parents=True)
    path = _write_config(
        tmp_path / "config.toml",
        f"""
        [watch]
        roots = [{str(outside_parent)!r}, {str(outside_child)!r}]
        allow_roots_outside_home = true
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    with pytest.raises(ConfigError) as exc:
        resolve_watch_roots(cfg.values)
    assert exc.value.code == "config.overlapping_watch_roots"


def test_allow_roots_outside_home_opt_in_still_accepts_inside_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home_root = tmp_path / "home" / ".claude" / "projects"
    home_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _write_config(
        tmp_path / "config.toml",
        f"""
        [watch]
        roots = [{str(home_root)!r}, {str(outside)!r}]
        allow_roots_outside_home = true
        """,
    )
    cfg = load_config(config_path=path, env={}, cli_overrides={})
    descriptors = resolve_watch_roots(cfg.values)
    assert {d.root_path for d in descriptors} == {home_root.resolve(), outside.resolve()}


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
