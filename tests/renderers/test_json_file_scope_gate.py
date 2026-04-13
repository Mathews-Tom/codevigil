"""NDJSON renderer must refuse output directories outside ``$HOME``."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.privacy import PrivacyViolationError
from codevigil.renderers.json_file import JsonFileRenderer


def test_inside_home_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    inside = tmp_path / "codevigil"
    inside.mkdir()
    JsonFileRenderer(output_dir=inside)  # no raise


def test_outside_home_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    outside = tmp_path / "not-home"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(PrivacyViolationError):
        JsonFileRenderer(output_dir=outside)
