"""Shared watch-root identity helpers.

Phase 1 introduces a canonical multi-root configuration contract without yet
wiring every runtime path to consume multiple roots. This module provides the
stable identity primitives later phases will reuse in the watcher, ingest
pipeline, and processed-session store.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_ROOT_HASH_BYTES: int = 12
_SESSION_KEY_SEPARATOR: str = ":"


@dataclass(frozen=True, slots=True)
class RootDescriptor:
    """Stable identity for one configured watch root."""

    root_id: str
    root_path: Path
    display_name: str


def make_root_id(root_path: Path) -> str:
    """Return a deterministic root identifier for a resolved path."""

    resolved = root_path.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:_ROOT_HASH_BYTES]
    return f"root-{digest}"


def describe_root(root_path: Path) -> RootDescriptor:
    """Return the canonical descriptor for one watch root path."""

    resolved = root_path.expanduser().resolve()
    return RootDescriptor(
        root_id=make_root_id(resolved),
        root_path=resolved,
        display_name=str(resolved),
    )


def describe_roots(root_paths: list[Path]) -> list[RootDescriptor]:
    """Describe every configured watch root in order."""

    return [describe_root(path) for path in root_paths]


def make_session_key(root_id: str, session_id: str) -> str:
    """Compose the future store/runtime key for one session within one root."""

    if not root_id:
        raise ValueError("root_id must be non-empty")
    if not session_id:
        raise ValueError("session_id must be non-empty")
    return f"{root_id}{_SESSION_KEY_SEPARATOR}{session_id}"


def split_session_key(session_key: str) -> tuple[str, str]:
    """Split a composed session key back into ``(root_id, session_id)``."""

    root_id, separator, session_id = session_key.partition(_SESSION_KEY_SEPARATOR)
    if not separator or not root_id or not session_id:
        raise ValueError(f"invalid session_key: {session_key!r}")
    return root_id, session_id


__all__ = [
    "RootDescriptor",
    "describe_root",
    "describe_roots",
    "make_root_id",
    "make_session_key",
    "split_session_key",
]
