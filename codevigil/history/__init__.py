"""Historical session retrospective surface.

This package provides the ``codevigil history`` subcommand family:

- ``history list``  — enumerate stored sessions with filters
- ``history <ID>``  — render a single session in detail
- ``history diff A B`` — side-by-side LCS diff of two sessions
- ``history heatmap <ID>`` — tool x severity matrix

``rich`` is a core dependency — all subcommands use it unconditionally.
"""

from __future__ import annotations

__all__: list[str] = []
