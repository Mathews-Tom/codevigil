"""Persistent per-file cursor cache for ``PollingSource``.

``codevigil watch`` re-walks every JSONL file under its configured root
on every invocation and, without this cache, re-reads each file from
byte zero. For a home directory with weeks of accumulated Claude Code
session logs this means the first tick takes longer than the interval
between ticks, which is the "stuck startup" the user sees.

The cursor cache persists each file's byte offset (and its inode, size,
pending partial-line buffer, and file mtime) between watch invocations.
On startup the cache is loaded; on shutdown it is written back. During a
running session the cache is read-only — the authoritative cursor state
lives in ``PollingSource._cursors``.

Cache correctness
-----------------

The cache is consulted only from ``PollingSource._handle_path`` when the
in-memory cursor table has no entry for a file (i.e. the file is being
discovered fresh this run). The cache entry is accepted only when:

1. The file's inode matches the cached inode, and
2. The file's current size is >= the cached size.

A rotation (inode change), truncate (size shrank), or missing file
invalidates the cache entry. The usual NEW_SESSION + full-replay code
path takes over in those cases. When the cache entry is accepted the
seeded cursor skips ``cached.size`` bytes of already-processed history;
only the tail (``current_size - cached.size`` bytes) is read and emitted
as APPEND events.

Metric-history caveat
---------------------

Collectors are in-memory and do not persist between watch invocations.
A file that resumes from a cached offset feeds only the tail events to
the collectors, so collector metrics on a resumed session reflect only
events that arrived after the last cache flush, not the full session
history. This is intentional: the alternative (persisting collector
state) is larger in scope than Phase B and requires a schema versioning
story per collector. The lifecycle math (``last_monotonic``
back-dating) is unaffected because the NEW_SESSION event still carries
the file's real mtime.

Storage location
----------------

The default cache path is
``~/.local/state/codevigil/cursor_cache_<hash>.json`` where ``<hash>``
is the first 16 hex chars of ``sha256(resolved_root_path)``. This keeps
multiple watch roots from clobbering each other's cache. The path is
configurable via ``watch.cursor_cache_path``; setting
``watch.cursor_cache_enabled = false`` disables the cache entirely.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

_CACHE_VERSION: int = 1


@dataclass(slots=True)
class CachedCursor:
    """One persisted cursor entry.

    ``pending`` is the raw byte buffer of a partial line that had no
    terminating ``\\n`` at the last save. It must round-trip exactly so
    the next poll can concatenate new bytes onto it and re-attempt line
    splitting.
    """

    inode: int
    size: int
    offset: int
    pending: bytes
    mtime: float


def default_cache_path(state_dir: Path, root: Path) -> Path:
    """Compute the default cache file path for a given watch root.

    The file name is keyed by the SHA-256 of the resolved root path so
    two different watch roots get independent caches under a single
    state directory.
    """

    root_hash = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return state_dir / f"cursor_cache_{root_hash}.json"


class CursorStore:
    """Load and save a JSON-backed cursor cache bound to one watch root.

    The store is owned by :class:`~codevigil.watcher.PollingSource` and
    is consulted only at construction time (via :meth:`load`) and at
    shutdown time (via :meth:`save`). The in-memory cache between those
    two calls lives in the ``PollingSource``'s own cursor table.
    """

    def __init__(self, cache_path: Path, root: Path) -> None:
        self._cache_path: Path = cache_path
        self._root: Path = root.resolve()

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def load(self) -> dict[Path, CachedCursor]:
        """Read the cache file and return a path → :class:`CachedCursor` map.

        Returns an empty dict when the file is missing, unreadable, on a
        version mismatch, or when the stored ``root`` does not match the
        current watch root. Corrupt entries are dropped individually and
        do not prevent other entries from loading.
        """

        if not self._cache_path.exists():
            return {}
        try:
            with self._cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.info(
                "cursor_cache.unreadable path=%s error=%s",
                self._cache_path,
                exc,
            )
            return {}

        if not isinstance(payload, dict):
            return {}
        if payload.get("version") != _CACHE_VERSION:
            _LOG.info(
                "cursor_cache.version_mismatch path=%s got=%s want=%d",
                self._cache_path,
                payload.get("version"),
                _CACHE_VERSION,
            )
            return {}
        stored_root = payload.get("root")
        if stored_root != str(self._root):
            _LOG.info(
                "cursor_cache.root_mismatch path=%s stored=%s current=%s",
                self._cache_path,
                stored_root,
                self._root,
            )
            return {}

        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            return {}

        out: dict[Path, CachedCursor] = {}
        for entry in raw_files:
            parsed = self._parse_entry(entry)
            if parsed is None:
                continue
            path, cursor = parsed
            out[path] = cursor
        return out

    def save(self, cursors: dict[Path, CachedCursor]) -> None:
        """Persist the given cursor map to disk.

        Creates the parent directory as needed. Writes atomically via a
        ``.tmp`` sibling and ``Path.replace`` so a crash mid-write does
        not leave a half-truncated cache file.
        """

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        files_payload: list[dict[str, object]] = []
        for path, cursor in sorted(cursors.items(), key=lambda kv: str(kv[0])):
            files_payload.append(
                {
                    "path": str(path),
                    "inode": cursor.inode,
                    "size": cursor.size,
                    "offset": cursor.offset,
                    "pending_b64": base64.b64encode(cursor.pending).decode("ascii"),
                    "mtime": cursor.mtime,
                }
            )
        payload = {
            "version": _CACHE_VERSION,
            "root": str(self._root),
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "files": files_payload,
        }
        tmp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        tmp_path.replace(self._cache_path)

    @staticmethod
    def _parse_entry(entry: object) -> tuple[Path, CachedCursor] | None:
        if not isinstance(entry, dict):
            return None
        path_raw = entry.get("path")
        inode_raw = entry.get("inode")
        size_raw = entry.get("size")
        offset_raw = entry.get("offset")
        pending_raw = entry.get("pending_b64", "")
        mtime_raw = entry.get("mtime")
        if not isinstance(path_raw, str):
            return None
        if not isinstance(inode_raw, int) or not isinstance(size_raw, int):
            return None
        if not isinstance(offset_raw, int):
            return None
        if not isinstance(pending_raw, str):
            return None
        if not isinstance(mtime_raw, (int, float)):
            return None
        try:
            pending = base64.b64decode(pending_raw, validate=True)
        except (ValueError, TypeError):
            return None
        return (
            Path(path_raw),
            CachedCursor(
                inode=inode_raw,
                size=size_raw,
                offset=offset_raw,
                pending=pending,
                mtime=float(mtime_raw),
            ),
        )


__all__ = ["CachedCursor", "CursorStore", "default_cache_path"]
