"""Tests for codevigil.renderers._bars.render_gradient_bar."""

from __future__ import annotations

import pytest

from codevigil.renderers._bars import render_gradient_bar


class TestRenderGradientBarBoundaries:
    def test_zero_value_returns_empty_bar(self) -> None:
        result = render_gradient_bar(0, 10)
        assert len(result) == 8
        # All characters should be the empty glyph (space).
        assert result == " " * 8

    def test_full_value_returns_full_bar(self) -> None:
        result = render_gradient_bar(10, 10)
        assert len(result) == 8
        # All characters should be the full block.
        assert result == "█" * 8

    def test_half_value_is_half_filled(self) -> None:
        result = render_gradient_bar(5, 10, width=8)
        assert len(result) == 8
        # With width=8 and value=5/10=0.5, exactly 4 full blocks and
        # the remaining 4 positions are empty (remainder=0 at the midpoint).
        assert result == "████" + " " * 4

    def test_negative_value_clamps_to_empty_no_crash(self) -> None:
        result = render_gradient_bar(-1, 10)
        assert len(result) == 8
        assert result == " " * 8

    def test_above_maximum_clamps_to_full_no_crash(self) -> None:
        result = render_gradient_bar(15, 10)
        assert len(result) == 8
        assert result == "█" * 8

    def test_zero_maximum_returns_space_bar_no_crash(self) -> None:
        result = render_gradient_bar(5, 0)
        assert len(result) == 8
        assert result == " " * 8


class TestRenderGradientBarWidth:
    def test_width_parameter_is_respected(self) -> None:
        result = render_gradient_bar(5, 10, width=4)
        assert len(result) == 4

    def test_width_1_full_value_returns_single_full_block(self) -> None:
        result = render_gradient_bar(10, 10, width=1)
        assert result == "█"

    def test_width_1_zero_value_returns_single_space(self) -> None:
        result = render_gradient_bar(0, 10, width=1)
        assert result == " "

    def test_width_16_returns_16_chars(self) -> None:
        result = render_gradient_bar(3, 10, width=16)
        assert len(result) == 16

    def test_invalid_width_raises(self) -> None:
        with pytest.raises(ValueError, match="width"):
            render_gradient_bar(5, 10, width=0)


class TestRenderGradientBarGlyphSet:
    def test_partial_fill_uses_intermediate_glyph(self) -> None:
        """A non-zero, non-full value must produce at least one non-space, non-full glyph
        or a clean split between full and empty characters (no unexpected characters)."""
        _valid_glyphs = set(" ▏▎▍▌▋▊▉█")
        for v in range(11):
            result = render_gradient_bar(v, 10, width=8)
            assert all(ch in _valid_glyphs for ch in result), (
                f"unexpected glyph in bar for value={v}: {result!r}"
            )

    def test_result_is_always_monotone_filled(self) -> None:
        """Characters must be sorted non-ascending in fill level (full blocks before partial
        before empty) — no empty glyph followed by a filled glyph."""
        _glyph_order = " ▏▎▍▌▋▊▉█"
        for v in range(11):
            result = render_gradient_bar(v, 10, width=8)
            indices = [_glyph_order.index(ch) for ch in result]
            # Each subsequent character must have equal or lower fill.
            for i in range(len(indices) - 1):
                assert indices[i] >= indices[i + 1], f"non-monotone bar for value={v}: {result!r}"
