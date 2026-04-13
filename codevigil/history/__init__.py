"""Historical session retrospective surface.

This package provides the ``codevigil history`` subcommand family:

- ``history list``  — enumerate stored sessions with filters
- ``history <ID>``  — render a single session in detail
- ``history diff A B`` — side-by-side LCS diff of two sessions
- ``history heatmap <ID>`` — tool x severity matrix (requires rich extra)

The ``rich`` optional dependency is detected exactly once here. Every
module in this package imports ``RICH`` from here rather than repeating
the try/except. This is the single approved optional-dependency branch.

    from codevigil.history import RICH

``RICH`` is the ``rich`` module object when installed, or ``None`` when
the ``[rich]`` extra is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    import rich as _rich_module

    RICH: object = _rich_module
except ImportError:
    RICH = None

if TYPE_CHECKING:
    import rich  # noqa: F401  (type-checker only)

__all__ = ["RICH"]
