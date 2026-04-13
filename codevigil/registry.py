"""Shared validation for the collector and renderer registries.

Both ``codevigil.collectors`` and ``codevigil.renderers`` expose a registry
dict built by calling ``register()`` at module import time. This module holds
the validation rules that apply to both:

* Duplicate ``name`` collides loudly via ``RegistryCollisionError``.
* Third-party plugins (any module not under ``codevigil.collectors`` /
  ``codevigil.renderers``) must use a dotted ``name`` to avoid stomping on
  built-in names. Built-ins use bare names.
* Each registered class is checked for the presence of the attributes and
  methods its target protocol declares, at registration time — not at the
  first ingest/render call.
"""

from __future__ import annotations

from typing import TypeVar, cast

from codevigil.types import Collector, Renderer

_CODEVIGIL_COLLECTOR_PKG: str = "codevigil.collectors"
_CODEVIGIL_RENDERER_PKG: str = "codevigil.renderers"

_COLLECTOR_REQUIRED_METHODS: tuple[str, ...] = ("ingest", "snapshot", "reset")
_RENDERER_REQUIRED_METHODS: tuple[str, ...] = ("render", "render_error", "close")

_COLLECTOR_REQUIRED_STR_ATTRS: tuple[str, ...] = ("name", "complexity")
_RENDERER_REQUIRED_STR_ATTRS: tuple[str, ...] = ("name",)


class RegistryCollisionError(Exception):
    """Two classes tried to register under the same name."""


class RegistryValidationError(Exception):
    """A class failed protocol conformance or namespacing checks at registration."""


def _check_str_attrs(cls: type, attrs: tuple[str, ...], kind: str) -> None:
    for attr in attrs:
        if not hasattr(cls, attr):
            raise RegistryValidationError(
                f"{kind} {cls.__qualname__!r} is missing required class attribute {attr!r}"
            )
        value = getattr(cls, attr)
        if not isinstance(value, str) or not value:
            raise RegistryValidationError(
                f"{kind} {cls.__qualname__!r} class attribute {attr!r} must be a "
                f"non-empty string; got {type(value).__name__}"
            )


def _check_methods(cls: type, methods: tuple[str, ...], kind: str) -> None:
    for method in methods:
        if not callable(getattr(cls, method, None)):
            raise RegistryValidationError(
                f"{kind} {cls.__qualname__!r} is missing required method {method!r}"
            )


def _check_namespace(cls: type, name: str, builtin_pkg: str, kind: str) -> None:
    module = cls.__module__ or ""
    is_builtin = module == builtin_pkg or module.startswith(builtin_pkg + ".")
    if is_builtin:
        if "." in name:
            raise RegistryValidationError(
                f"built-in {kind} {cls.__qualname__!r} must use a bare name "
                f"without dots; got {name!r}"
            )
        return
    if "." not in name:
        raise RegistryValidationError(
            f"third-party {kind} {cls.__qualname__!r} (module {module!r}) must "
            f"register under a dotted name like 'vendor.metric'; got {name!r}"
        )


C = TypeVar("C", bound=type)


def register_collector(registry: dict[str, type[Collector]], cls: C) -> C:
    """Validate and register a collector class in the given registry dict."""

    _check_str_attrs(cls, _COLLECTOR_REQUIRED_STR_ATTRS, kind="collector")
    _check_methods(cls, _COLLECTOR_REQUIRED_METHODS, kind="collector")
    name: str = cast(str, cls.name)  # type: ignore[attr-defined]
    _check_namespace(cls, name, _CODEVIGIL_COLLECTOR_PKG, kind="collector")
    if name in registry:
        existing = registry[name]
        raise RegistryCollisionError(
            f"collector name {name!r} already registered by "
            f"{existing.__qualname__!r}; {cls.__qualname__!r} cannot reuse it"
        )
    registry[name] = cls
    return cls


def register_renderer(registry: dict[str, type[Renderer]], cls: C) -> C:
    """Validate and register a renderer class in the given registry dict."""

    _check_str_attrs(cls, _RENDERER_REQUIRED_STR_ATTRS, kind="renderer")
    _check_methods(cls, _RENDERER_REQUIRED_METHODS, kind="renderer")
    name: str = cast(str, cls.name)  # type: ignore[attr-defined]
    _check_namespace(cls, name, _CODEVIGIL_RENDERER_PKG, kind="renderer")
    if name in registry:
        existing = registry[name]
        raise RegistryCollisionError(
            f"renderer name {name!r} already registered by "
            f"{existing.__qualname__!r}; {cls.__qualname__!r} cannot reuse it"
        )
    registry[name] = cls
    return cls


__all__ = [
    "RegistryCollisionError",
    "RegistryValidationError",
    "register_collector",
    "register_renderer",
]
