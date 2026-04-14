"""codevigil — local, privacy-preserving observability for Claude Code sessions.

Importing the top-level package installs the privacy import hook so that any
subsequent ``import socket`` / ``import subprocess`` / etc. from inside a
codevigil module raises ``PrivacyViolationError`` immediately. This must
happen before any other codevigil submodule loads, so the hook install is
the first statement after ``__future__`` in this file.
"""

from __future__ import annotations

from codevigil.privacy import PrivacyViolationError
from codevigil.privacy import install as _install_privacy_hook

_install_privacy_hook()

__version__: str = "0.2.1"

__all__ = ["PrivacyViolationError", "__version__"]
