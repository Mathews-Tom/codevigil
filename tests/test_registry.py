"""Registry validation tests.

These tests build synthetic collector / renderer classes in-memory and
drive ``register_collector`` / ``register_renderer`` directly against a
throwaway registry dict. The built-in package registries are not mutated.
"""

from __future__ import annotations

import pytest

from codevigil.registry import (
    RegistryCollisionError,
    RegistryValidationError,
    register_collector,
    register_renderer,
)
from codevigil.types import Collector, MetricSnapshot, Renderer


def _make_builtin_collector(name: str) -> type[Collector]:
    cls = type(
        "BuiltinC_" + name.replace(".", "_"),
        (),
        {
            "name": name,
            "complexity": "O(1)",
            "ingest": lambda self, event: None,
            "snapshot": lambda self: MetricSnapshot(name=name, value=0.0, label="ok"),
            "reset": lambda self: None,
        },
    )
    # Make the class appear to live in codevigil.collectors.<name>.
    cls.__module__ = "codevigil.collectors.fake_" + name.replace(".", "_")
    return cls  # type: ignore[return-value]


def _make_third_party_collector(name: str) -> type[Collector]:
    cls = type(
        "ThirdPartyC_" + name.replace(".", "_"),
        (),
        {
            "name": name,
            "complexity": "O(1)",
            "ingest": lambda self, event: None,
            "snapshot": lambda self: MetricSnapshot(name=name, value=0.0, label="ok"),
            "reset": lambda self: None,
        },
    )
    cls.__module__ = "acme.codevigil_plugin"
    return cls  # type: ignore[return-value]


def _make_builtin_renderer(name: str) -> type[Renderer]:
    cls = type(
        "BuiltinR_" + name.replace(".", "_"),
        (),
        {
            "name": name,
            "render": lambda self, snapshots, meta: None,
            "render_error": lambda self, err, meta: None,
            "close": lambda self: None,
        },
    )
    cls.__module__ = "codevigil.renderers.fake_" + name.replace(".", "_")
    return cls  # type: ignore[return-value]


def test_register_builtin_collector_succeeds() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = _make_builtin_collector("foo")
    register_collector(registry, cls)
    assert registry == {"foo": cls}


def test_collision_raises() -> None:
    registry: dict[str, type[Collector]] = {}
    first = _make_builtin_collector("dup")
    second = _make_builtin_collector("dup")
    register_collector(registry, first)
    with pytest.raises(RegistryCollisionError):
        register_collector(registry, second)


def test_third_party_must_use_dotted_name() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = _make_third_party_collector("bare_name")
    with pytest.raises(RegistryValidationError):
        register_collector(registry, cls)


def test_third_party_dotted_name_is_accepted() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = _make_third_party_collector("acme.quality")
    register_collector(registry, cls)
    assert registry == {"acme.quality": cls}


def test_builtin_may_not_use_dotted_name() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = _make_builtin_collector("not.allowed")
    with pytest.raises(RegistryValidationError):
        register_collector(registry, cls)


def test_missing_method_is_rejected() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = type(
        "Broken",
        (),
        {
            "name": "broken",
            "complexity": "O(1)",
            "ingest": lambda self, event: None,
            "snapshot": lambda self: MetricSnapshot(name="broken", value=0.0, label="ok"),
            # reset deliberately missing
        },
    )
    cls.__module__ = "codevigil.collectors.fake_broken"
    with pytest.raises(RegistryValidationError):
        register_collector(registry, cls)  # type: ignore[arg-type]


def test_non_string_name_is_rejected() -> None:
    registry: dict[str, type[Collector]] = {}
    cls = type(
        "BadName",
        (),
        {
            "name": 42,
            "complexity": "O(1)",
            "ingest": lambda self, event: None,
            "snapshot": lambda self: MetricSnapshot(name="badname", value=0.0, label="ok"),
            "reset": lambda self: None,
        },
    )
    cls.__module__ = "codevigil.collectors.fake_badname"
    with pytest.raises(RegistryValidationError):
        register_collector(registry, cls)  # type: ignore[arg-type]


def test_renderer_registration_validates_methods() -> None:
    registry: dict[str, type[Renderer]] = {}
    good = _make_builtin_renderer("terminal")
    register_renderer(registry, good)
    assert registry == {"terminal": good}

    bad = type(
        "BadRenderer",
        (),
        {
            "name": "bad",
            "render": lambda self, snapshots, meta: None,
            # render_error missing
            "close": lambda self: None,
        },
    )
    bad.__module__ = "codevigil.renderers.fake_bad"
    with pytest.raises(RegistryValidationError):
        register_renderer(registry, bad)  # type: ignore[arg-type]
