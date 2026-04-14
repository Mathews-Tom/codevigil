"""Gradient Unicode bar renderer for terminal heatmap cells.

Uses the 9-glyph block-element set ``" ▏▎▍▌▋▊▉█"`` (space through full
block), which gives 8 partial-fill steps per character position.  The result
is a smooth proportional bar that communicates magnitude without color alone.

Example (width=8, value=5, maximum=10):
    "████░░░░"  — when using a simpler glyph set

With the 9-glyph set the partial boundary character encodes the fractional
remainder, so ``render_gradient_bar(5, 10, width=8)`` returns the four full
blocks followed by a mid-fill glyph then three spaces, depending on the
fractional calculation.

Clamping rules (all enforce at the return boundary so callers need not guard):
  - ``value < 0``         → treated as 0
  - ``value > maximum``   → treated as ``maximum``
  - ``maximum == 0``      → returns a bar of ``width`` space characters
"""

from __future__ import annotations

_GLYPHS: str = " ▏▎▍▌▋▊▉█"
# 9 glyphs → 8 sub-character steps (indices 0-8 inclusive).
_STEPS_PER_CHAR: int = len(_GLYPHS) - 1  # 8


def render_gradient_bar(value: float, maximum: float, width: int = 8) -> str:
    """Return a proportional Unicode bar string of exactly ``width`` characters.

    Parameters:
        value:   The current value to represent.  Clamped to ``[0, maximum]``.
        maximum: The value that corresponds to a fully filled bar.
                 When 0, returns ``width`` space characters.
        width:   Number of output characters.  Must be >= 1.

    Returns:
        A string of exactly ``width`` Unicode characters whose filled portion
        is proportional to ``value / maximum``.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    if maximum == 0.0:
        return " " * width

    # Clamp.
    clamped = max(0.0, min(float(value), float(maximum)))

    # Total sub-character fill units across all ``width`` positions.
    total_units = width * _STEPS_PER_CHAR
    filled_units = round(clamped / maximum * total_units)

    full_chars = filled_units // _STEPS_PER_CHAR
    remainder = filled_units % _STEPS_PER_CHAR

    if full_chars >= width:
        # Value is at or rounds up to maximum.
        return _GLYPHS[-1] * width

    bar = _GLYPHS[-1] * full_chars
    bar += _GLYPHS[remainder]
    bar += _GLYPHS[0] * (width - full_chars - 1)
    return bar


__all__ = ["render_gradient_bar"]
