"""Renderer registry package.

Built-in renderers import themselves into ``RENDERERS`` at module import
time via ``register_renderer``. v0.1 ships the terminal and json_file
renderers; third-party renderers register under dotted names.
"""

from __future__ import annotations

from codevigil.registry import register_renderer
from codevigil.renderers import json_file as _json_file
from codevigil.renderers import terminal as _terminal
from codevigil.types import Renderer

RENDERERS: dict[str, type[Renderer]] = {}

register_renderer(RENDERERS, _terminal.TerminalRenderer)
register_renderer(RENDERERS, _json_file.JsonFileRenderer)

__all__ = ["RENDERERS", "register_renderer"]
