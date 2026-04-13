"""Smoke test: the package imports and exposes a version string."""

from __future__ import annotations

import codevigil


def test_version_is_non_empty_string() -> None:
    assert isinstance(codevigil.__version__, str)
    assert codevigil.__version__
