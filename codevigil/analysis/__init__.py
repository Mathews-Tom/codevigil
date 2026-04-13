"""Historical analytics substrate for codevigil.

This package provides offline session-report storage, cohort reduction,
period-over-period comparison, and sample-size guards. All components are
stdlib-only; no database or network dependencies.

Submodules:

- :mod:`store` — append-only on-disk index of finalised session reports.
- :mod:`cohort` — reducer that takes a list of session reports and emits
  grouped aggregates keyed by ``day``, ``week``, ``project``, ``model``, or
  ``permission_mode``.
- :mod:`compare` — period-over-period comparator producing mean deltas and
  a signed significance flag via Welch's t-test (stdlib ``statistics``).
- :mod:`guards` — sample-size and span guards that suppress low-n cells from
  headline display and drop undersized observation windows.
"""

from __future__ import annotations

__all__ = [
    "cohort",
    "compare",
    "guards",
    "store",
]
