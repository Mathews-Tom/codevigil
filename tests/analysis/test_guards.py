"""Unit tests for codevigil.analysis.guards.

The sample-size guard is a critical-path component — it must block n<5 cells
from rendering as headline numbers in every output path. These tests verify
that contract and the span guard.
"""

from __future__ import annotations

import logging

import pytest

from codevigil.analysis.guards import (
    DEFAULT_MIN_OBSERVATION_DAYS,
    MIN_CELL_N,
    CellTooSmall,
    GuardedCell,
    SpanTooShort,
    cell_sentinel,
    guard_cell,
    guard_span,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_min_cell_n_is_five() -> None:
    assert MIN_CELL_N == 5


def test_default_min_observation_days_is_one() -> None:
    assert DEFAULT_MIN_OBSERVATION_DAYS == 1


# ---------------------------------------------------------------------------
# guard_cell — passing cases
# ---------------------------------------------------------------------------


def test_guard_cell_exactly_at_min_n_passes() -> None:
    result = guard_cell(3.14, 5)
    assert isinstance(result, GuardedCell)
    assert result.value == pytest.approx(3.14)
    assert result.n == 5


def test_guard_cell_above_min_n_passes() -> None:
    result = guard_cell(1.0, 100)
    assert result.n == 100


def test_guard_cell_custom_min_n_passes() -> None:
    result = guard_cell(0.0, 3, min_n=3)
    assert result.n == 3


def test_guard_cell_zero_value_passes() -> None:
    result = guard_cell(0.0, 10)
    assert result.value == pytest.approx(0.0)


def test_guard_cell_negative_value_passes() -> None:
    result = guard_cell(-5.5, 10)
    assert result.value == pytest.approx(-5.5)


# ---------------------------------------------------------------------------
# guard_cell — failing cases (n < min_n)
# ---------------------------------------------------------------------------


def test_guard_cell_n_zero_raises() -> None:
    with pytest.raises(CellTooSmall) as exc_info:
        guard_cell(1.0, 0)
    assert exc_info.value.n == 0
    assert exc_info.value.min_n == MIN_CELL_N


def test_guard_cell_n_one_raises() -> None:
    with pytest.raises(CellTooSmall):
        guard_cell(2.0, 1)


def test_guard_cell_n_four_raises() -> None:
    with pytest.raises(CellTooSmall) as exc_info:
        guard_cell(2.0, 4)
    assert exc_info.value.n == 4


def test_guard_cell_sentinel_string() -> None:
    with pytest.raises(CellTooSmall) as exc_info:
        guard_cell(1.0, 3)
    assert exc_info.value.sentinel == "n<5"


def test_guard_cell_custom_min_n_sentinel_string() -> None:
    with pytest.raises(CellTooSmall) as exc_info:
        guard_cell(1.0, 2, min_n=10)
    assert exc_info.value.sentinel == "n<10"


def test_guard_cell_exception_message_contains_sentinel() -> None:
    with pytest.raises(CellTooSmall) as exc_info:
        guard_cell(1.0, 2)
    assert "n<5" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CellTooSmall attributes
# ---------------------------------------------------------------------------


def test_cell_too_small_attributes() -> None:
    exc = CellTooSmall(3, min_n=5)
    assert exc.n == 3
    assert exc.min_n == 5
    assert exc.sentinel == "n<5"


def test_cell_too_small_default_min_n() -> None:
    exc = CellTooSmall(1)
    assert exc.min_n == MIN_CELL_N
    assert exc.sentinel == f"n<{MIN_CELL_N}"


# ---------------------------------------------------------------------------
# guard_span — passing cases
# ---------------------------------------------------------------------------


def test_guard_span_exactly_at_min_days_passes() -> None:
    guard_span(1.0, min_days=1)  # Must not raise


def test_guard_span_above_min_days_passes() -> None:
    guard_span(30.0, min_days=7)  # Must not raise


def test_guard_span_default_min_days() -> None:
    guard_span(1.0)  # DEFAULT_MIN_OBSERVATION_DAYS=1, must not raise


# ---------------------------------------------------------------------------
# guard_span — failing cases
# ---------------------------------------------------------------------------


def test_guard_span_zero_days_raises() -> None:
    with pytest.raises(SpanTooShort) as exc_info:
        guard_span(0.0, min_days=1)
    assert exc_info.value.actual_days == 0.0
    assert exc_info.value.min_days == 1


def test_guard_span_fractional_too_short_raises() -> None:
    with pytest.raises(SpanTooShort):
        guard_span(0.5, min_days=1)


def test_guard_span_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="codevigil.analysis.guards"),
        pytest.raises(SpanTooShort),
    ):
        guard_span(0.0, min_days=3, label="test-period")
    assert any("dropping period" in r.message for r in caplog.records)


def test_guard_span_reason_contains_context() -> None:
    with pytest.raises(SpanTooShort) as exc_info:
        guard_span(0.0, min_days=7, label="week-A")
    assert "week-A" in exc_info.value.reason


def test_guard_span_reason_without_label() -> None:
    with pytest.raises(SpanTooShort) as exc_info:
        guard_span(0.5, min_days=2)
    assert "0.5" in exc_info.value.reason
    assert "2" in exc_info.value.reason


# ---------------------------------------------------------------------------
# SpanTooShort attributes
# ---------------------------------------------------------------------------


def test_span_too_short_attributes() -> None:
    exc = SpanTooShort(0.3, min_days=7, label="2026-W15")
    assert exc.actual_days == pytest.approx(0.3)
    assert exc.min_days == 7
    assert "2026-W15" in exc.reason


# ---------------------------------------------------------------------------
# cell_sentinel helper
# ---------------------------------------------------------------------------


def test_cell_sentinel_default() -> None:
    assert cell_sentinel() == "n<5"


def test_cell_sentinel_custom_min_n() -> None:
    assert cell_sentinel(10) == "n<10"


def test_cell_sentinel_matches_exception_sentinel() -> None:
    exc = CellTooSmall(3)
    assert cell_sentinel() == exc.sentinel


# ---------------------------------------------------------------------------
# Contract: n<5 cells must never render as headline numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_guard_cell_blocks_all_n_below_5(n: int) -> None:
    """Any n < 5 must raise CellTooSmall — never return a GuardedCell."""
    with pytest.raises(CellTooSmall):
        guard_cell(99.0, n)


@pytest.mark.parametrize("n", [5, 6, 10, 50, 1000])
def test_guard_cell_allows_all_n_at_or_above_5(n: int) -> None:
    """Any n >= 5 must return a GuardedCell — never raise."""
    result = guard_cell(1.0, n)
    assert result.n == n
