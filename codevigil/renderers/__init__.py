"""Renderer registry package.

Built-in renderers import themselves into ``RENDERERS`` at module import
time via ``register_renderer``. Phase 1 ships the registry empty; concrete
terminal and json_file renderers land in the renderers phase.
"""

from __future__ import annotations

from codevigil.registry import register_renderer
from codevigil.types import Renderer

RENDERERS: dict[str, type[Renderer]] = {}

__all__ = ["RENDERERS", "register_renderer"]
