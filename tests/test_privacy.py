"""Import allowlist hook tests."""

from __future__ import annotations

import sys
from typing import Any

import pytest

import codevigil  # noqa: F401  — triggers hook installation
from codevigil.privacy import PrivacyImportHook, PrivacyViolationError

_BANNED_DIRECT = [
    "socket",
    "ssl",
    "urllib",
    "urllib.request",
    "httpx",
    "requests",
    "aiohttp",
    "ftplib",
    "smtplib",
    "subprocess",
    "pty",
]


def _exec_in_fake_codevigil_module(code: str) -> None:
    """Run ``code`` as if it lived inside a codevigil submodule.

    The import hook inspects the direct caller's ``__name__`` — setting it
    to ``codevigil.fake_caller`` makes the synthetic frame appear to be a
    codevigil submodule, which is exactly the case the hook must block.
    """

    globals_: dict[str, Any] = {
        "__name__": "codevigil.fake_caller",
        "__package__": "codevigil",
    }
    exec(code, globals_)


@pytest.mark.parametrize("module_name", _BANNED_DIRECT)
def test_direct_banned_import_from_codevigil_is_blocked(module_name: str) -> None:
    sys.modules.pop(module_name, None)
    with pytest.raises(PrivacyViolationError):
        _exec_in_fake_codevigil_module(f"import {module_name}")


def test_banned_import_from_non_codevigil_caller_is_allowed() -> None:
    """The hook must not block network imports from user code outside codevigil."""

    sys.modules.pop("socket", None)
    globals_: dict[str, Any] = {
        "__name__": "some_user_module",
        "__package__": "some_user_module",
    }
    exec("import socket", globals_)  # must not raise


def test_hook_is_installed_on_package_import() -> None:
    assert any(isinstance(f, PrivacyImportHook) for f in sys.meta_path)


def test_install_is_idempotent() -> None:
    from codevigil.privacy import install

    before = sum(isinstance(f, PrivacyImportHook) for f in sys.meta_path)
    install()
    install()
    after = sum(isinstance(f, PrivacyImportHook) for f in sys.meta_path)
    assert before == after == 1


def test_permitted_transitive_import_is_not_blocked() -> None:
    """Importing a permitted stdlib module whose body pulls in no banned
    module must still succeed when called from inside a codevigil frame."""

    _exec_in_fake_codevigil_module("import json")
    _exec_in_fake_codevigil_module("import pathlib")
    _exec_in_fake_codevigil_module("import dataclasses")
