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

__all__ = ["COLLECTORS", "register_collector"]
