"""Network-egress import gate.

Installs an ``importlib`` meta-path finder that refuses to load any banned
module when the *direct* importer lives inside the ``codevigil`` package. The
hook is active in every execution mode and is the runtime half of the privacy
guarantee documented in ``docs/design.md`` §Privacy Enforcement.

The hook is deliberately scoped to *direct* imports from codevigil: if codevigil
imports a permitted stdlib module (e.g. ``json``) which in turn imports a
banned module transitively, the transitive import is allowed — the importer
frame at the point the banned module is resolved is the permitted stdlib
module, not codevigil.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import FrameType

# Exact fully-qualified module names that are blocked.
_BANNED_EXACT: frozenset[str] = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "http.client",
        "http.server",
        "urllib",
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "urllib3",
        "httpx",
        "requests",
        "aiohttp",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "xmlrpc",
        "xmlrpc.client",
        "xmlrpc.server",
        "subprocess",
        "pty",
        "multiprocessing.popen_fork",
        "multiprocessing.popen_forkserver",
        "multiprocessing.popen_spawn_posix",
        "multiprocessing.popen_spawn_win32",
    }
)

# Top-level package names whose submodules are all blocked.
_BANNED_ROOTS: frozenset[str] = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "httpx",
        "requests",
        "aiohttp",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "xmlrpc",
        "subprocess",
        "pty",
    }
)

_CODEVIGIL_ROOT: str = "codevigil"

# Frame modules we skip when locating the direct caller of a banned import.
# These are all Python-internal import-machinery frames.
_SKIPPED_CALLER_PREFIXES: tuple[str, ...] = (
    "importlib",
    "_frozen_importlib",
    "_frozen_importlib_external",
)


class PrivacyViolationError(ImportError):
    """Raised when a codevigil module attempts to import a banned module.

    Subclasses ``ImportError`` so ``find_spec`` can raise it and have the
    traceback blame the offending import statement, and so test assertions
    that use ``pytest.raises(ImportError)`` continue to work.
    """


def _is_banned(fullname: str) -> bool:
    if fullname in _BANNED_EXACT:
        return True
    root = fullname.split(".", 1)[0]
    return root in _BANNED_ROOTS


def _direct_caller_module(start: FrameType | None) -> str | None:
    """Return the name of the first non-import-machinery frame's module.

    Walks ``frame.f_back`` until it finds a frame whose ``__name__`` does
    not belong to the import-machinery prefixes. Returns ``None`` if no such
    frame exists (e.g. if called at interpreter shutdown).
    """

    frame = start
    while frame is not None:
        raw = frame.f_globals.get("__name__", "")
        module_name = raw if isinstance(raw, str) else ""
        if not any(
            module_name == prefix or module_name.startswith(prefix + ".")
            for prefix in _SKIPPED_CALLER_PREFIXES
        ):
            return module_name
        frame = frame.f_back
    return None


def _caller_is_codevigil(module_name: str | None) -> bool:
    if module_name is None:
        return False
    return module_name == _CODEVIGIL_ROOT or module_name.startswith(_CODEVIGIL_ROOT + ".")


class PrivacyImportHook(MetaPathFinder):
    """Meta-path finder that blocks banned imports from inside codevigil."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: object | None = None,
    ) -> ModuleSpec | None:
        if not _is_banned(fullname):
            return None
        caller = _direct_caller_module(sys._getframe(1))
        if _caller_is_codevigil(caller):
            raise PrivacyViolationError(
                f"codevigil module {caller!r} attempted to import banned module "
                f"{fullname!r}; network and subprocess modules are disallowed "
                "by the privacy gate (see docs/design.md §Privacy Enforcement)."
            )
        return None


_HOOK_SINGLETON: PrivacyImportHook | None = None


def install() -> PrivacyImportHook:
    """Install the privacy import hook.

    Idempotent: repeated calls return the same singleton instance without
    registering the hook more than once.
    """

    global _HOOK_SINGLETON
    if _HOOK_SINGLETON is None:
        _HOOK_SINGLETON = PrivacyImportHook()
        sys.meta_path.insert(0, _HOOK_SINGLETON)
    elif _HOOK_SINGLETON not in sys.meta_path:
        sys.meta_path.insert(0, _HOOK_SINGLETON)
    return _HOOK_SINGLETON


def uninstall() -> None:
    """Remove the privacy import hook if installed.

    Exposed for tests that need to observe the uninstalled baseline. Never
    called by the runtime.
    """

    global _HOOK_SINGLETON
    if _HOOK_SINGLETON is not None and _HOOK_SINGLETON in sys.meta_path:
        sys.meta_path.remove(_HOOK_SINGLETON)
    _HOOK_SINGLETON = None


__all__ = [
    "PrivacyImportHook",
    "PrivacyViolationError",
    "install",
    "uninstall",
]
