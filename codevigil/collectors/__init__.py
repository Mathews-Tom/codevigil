"""Collector registry package.

Built-in collectors import themselves into ``COLLECTORS`` at module import
time via ``register_collector``. Phase 1 ships the registry with zero
collectors; ``parse_health`` is introduced in the parser phase and the v0.1
user-facing collectors land in the collectors phase.
"""

from __future__ import annotations

from codevigil.registry import register_collector
from codevigil.types import Collector

COLLECTORS: dict[str, type[Collector]] = {}

# Import built-in collectors for their registration side effects. The
# ``parse_health`` collector is always-on and registers itself at import
# time; later phases append more user-facing collectors here.
from codevigil.collectors import parse_health as _parse_health  # noqa: E402,F401
from codevigil.collectors import prompts as _prompts  # noqa: E402,F401
from codevigil.collectors import read_edit_ratio as _read_edit_ratio  # noqa: E402,F401
from codevigil.collectors import reasoning_loop as _reasoning_loop  # noqa: E402,F401
from codevigil.collectors import stop_phrase as _stop_phrase  # noqa: E402,F401
from codevigil.collectors import thinking as _thinking  # noqa: E402,F401

__all__ = ["COLLECTORS", "register_collector"]
