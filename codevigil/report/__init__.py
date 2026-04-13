"""Cohort report surface for codevigil.

Provides the ``codevigil report --group-by`` and
``codevigil report --compare-periods`` rendering paths on top of the
:mod:`codevigil.analysis` substrate.

Submodules:

- :mod:`loader` — builds :class:`~codevigil.analysis.store.SessionReport`
  objects from JSONL session files, including extraction of the
  ``write_precision`` metric from the ``read_edit_ratio`` collector detail.
- :mod:`renderer` — Markdown renderers for the weekly-trend table,
  period-comparison table, methodology section, and appendix.
"""

from __future__ import annotations

__all__ = [
    "loader",
    "renderer",
]
