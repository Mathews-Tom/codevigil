"""safe_get: present / missing / type-mismatch behavior plus error-channel side effects."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.types import safe_get


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "safe_get.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _read_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["code"] for line in path.read_text().splitlines()]


def test_present_and_type_matching_returns_value(error_log: Path) -> None:
    payload = {"tool_name": "Read"}
    assert safe_get(payload, "tool_name", "", expected=str) == "Read"
    assert _read_codes(error_log) == []


def test_missing_optional_returns_default_without_warning(error_log: Path) -> None:
    payload: dict[str, object] = {}
    assert safe_get(payload, "token_count", 0, expected=int) == 0
    assert _read_codes(error_log) == []


def test_missing_required_logs_warn(error_log: Path) -> None:
    payload: dict[str, object] = {}
    assert safe_get(payload, "tool_name", "", expected=str, required=True) == ""
    assert _read_codes(error_log) == ["safe_get.missing_required"]


def test_type_mismatch_logs_warn_and_returns_default(error_log: Path) -> None:
    payload = {"token_count": "not-an-int"}
    assert safe_get(payload, "token_count", 0, expected=int) == 0
    assert _read_codes(error_log) == ["safe_get.type_mismatch"]


def test_no_expected_type_disables_type_check(error_log: Path) -> None:
    payload = {"free_form": {"nested": True}}
    result = safe_get(payload, "free_form", None)
    assert result == {"nested": True}
    assert _read_codes(error_log) == []
