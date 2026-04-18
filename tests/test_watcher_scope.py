"""Filesystem-scope rule: paths outside ``$HOME`` are rejected at construction."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.privacy import PrivacyViolationError
from codevigil.watcher import PollingSource
from tests._watcher_helpers import (
    install_error_log,
    read_error_codes,
    reset_error_log,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    install_error_log(tmp_path / "errors.log")
    yield home
    reset_error_log()


def test_root_inside_home_is_accepted(fake_home: Path) -> None:
    inside = fake_home / "projects"
    inside.mkdir()
    src = PollingSource(inside)
    assert src.root == inside.resolve()


def test_root_outside_home_raises_privacy_violation(fake_home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert not str(outside.resolve()).startswith(str(fake_home.resolve()))

    with pytest.raises(PrivacyViolationError) as exc:
        PollingSource(outside)
    assert "allow_roots_outside_home" in str(exc.value)

    codes = read_error_codes(tmp_path / "errors.log")
    assert "watcher.path_scope_violation" in codes


def test_root_outside_home_accepted_with_opt_in(fake_home: Path, tmp_path: Path) -> None:
    """``allow_outside_home=True`` is the runtime counterpart to the
    ``watch.allow_roots_outside_home`` config flag. With it set, outside-
    ``$HOME`` roots must resolve without raising and without recording a
    scope-violation on the error channel.
    """

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert not str(outside.resolve()).startswith(str(fake_home.resolve()))

    src = PollingSource(outside, allow_outside_home=True)
    assert src.root == outside.resolve()

    codes = read_error_codes(tmp_path / "errors.log")
    assert "watcher.path_scope_violation" not in codes
