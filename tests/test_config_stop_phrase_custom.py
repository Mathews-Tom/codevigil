"""``collectors.stop_phrase.custom_phrases`` accepts mixed string/table form."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.config import ConfigError, load_config


def _write(path: Path, body: str) -> Path:
    path.write_text(body.lstrip(), encoding="utf-8")
    return path


def test_plain_string_form_accepted(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [collectors.stop_phrase]
        custom_phrases = ["foo", "bar"]
        """,
    )
    resolved = load_config(config_path=cfg, env={}, cli_overrides={})
    phrases = resolved.values["collectors"]["stop_phrase"]["custom_phrases"]
    assert phrases == ["foo", "bar"]


def test_table_form_accepted(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [[collectors.stop_phrase.custom_phrases]]
        text = "foo"
        mode = "substring"
        category = "noise"
        """,
    )
    resolved = load_config(config_path=cfg, env={}, cli_overrides={})
    phrases = resolved.values["collectors"]["stop_phrase"]["custom_phrases"]
    assert phrases == [{"text": "foo", "mode": "substring", "category": "noise"}]


def test_mixed_form_accepted(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [collectors.stop_phrase]
        custom_phrases = ["plain", { text = "tabled", mode = "word" }]
        """,
    )
    resolved = load_config(config_path=cfg, env={}, cli_overrides={})
    phrases = resolved.values["collectors"]["stop_phrase"]["custom_phrases"]
    assert phrases[0] == "plain"
    assert phrases[1]["text"] == "tabled"


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [[collectors.stop_phrase.custom_phrases]]
        text = "foo"
        mode = "bogus"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=cfg, env={}, cli_overrides={})
    assert exc.value.code == "config.out_of_range"


def test_unknown_field_rejected(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [[collectors.stop_phrase.custom_phrases]]
        text = "foo"
        flavor = "spicy"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=cfg, env={}, cli_overrides={})
    assert exc.value.code == "config.unknown_key"


def test_missing_text_rejected(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.toml",
        """
        [[collectors.stop_phrase.custom_phrases]]
        mode = "word"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(config_path=cfg, env={}, cli_overrides={})
    assert exc.value.code == "config.type_mismatch"
