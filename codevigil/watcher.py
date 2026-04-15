"""Filesystem watcher: ``Source`` protocol and ``PollingSource`` implementation.

Turns a directory tree of session JSONL files into an iterator of typed
``SourceEvent`` records. The aggregator is the only consumer and is unaware
of which concrete ``Source`` is feeding it — v0.1 ships ``PollingSource``;
v0.2+ may add inotify / fsevents backends behind the same protocol.

The ``Source`` protocol lives in this module rather than ``codevigil.types``
because it is watcher-internal vocabulary: the aggregator imports
``codevigil.watcher.Source`` directly. Keeping it here means the watcher
module is self-contained and ``codevigil.types`` stays focused on the
parser/collector/renderer contracts that genuinely cross subsystem lines.

Five-case transition table (per ``docs/design.md`` §Watcher Design):

    | Transition              | Action                                    |
    | ----------------------- | ----------------------------------------- |
    | unknown path            | NEW_SESSION + APPEND per complete line    |
    | same inode, size grew   | APPEND per complete line                  |
    | same inode, size shrank | TRUNCATE, reset cursor, re-read           |
    | inode changed           | ROTATE, reset cursor, re-read             |
    | path vanished           | DELETE, evict cursor                      |

Partial trailing bytes (no newline) are buffered in the cursor's ``pending``
field and carried to the next poll, so a writer that flushes half a JSON
record never produces a torn line.

Note on ``SourceEvent`` shape: ``docs/design.md`` sketches a batched form
with ``lines: list[str]``; this implementation emits one ``SourceEvent`` per
complete line (``line: str | None``). The aggregator phase wires the events
into the parser one at a time anyway, and the per-line shape removes a layer
of unpacking at the call site without losing information.

Cold-start timestamp semantics
------------------------------

When ``PollingSource`` discovers a JSONL file for the first time, the
``SourceEvent.timestamp`` on the emitted ``NEW_SESSION`` event is set to the
file's ``stat.st_mtime`` (converted to a UTC ``datetime``), not the current
wall-clock time.

For a file that genuinely just appeared (live new session), ``st_mtime`` and
``_now()`` are equivalent within milliseconds, so the aggregator's lifecycle
math is unaffected.  For a pre-existing file replayed at startup, ``st_mtime``
reflects the last real write — often minutes, hours, or days in the past.  The
aggregator consumes this truthful timestamp to back-date the monotonic cursor
(``_SessionContext.last_monotonic``) so the 5-min / 35-min stale/evict
thresholds classify cold-replayed sessions correctly on the very first tick,
without waiting for real wall-clock time to pass.

See ``docs/design.md`` §Watcher Design → Cold-Start Replay for the full
back-dating derivation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.privacy import PrivacyViolationError
from codevigil.watcher_cache import CachedCursor, CursorStore, prefix_fingerprint_for_path

_CHUNK_SIZE: int = 1 * 1024 * 1024  # 1 MiB delta read chunk

_LOG = logging.getLogger(__name__)


class SourceEventKind(Enum):
    """Five filesystem-state transitions a ``Source`` can report."""

    NEW_SESSION = "new_session"
    APPEND = "append"
    ROTATE = "rotate"
    TRUNCATE = "truncate"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """One typed record yielded by ``Source.poll()``.

    ``line`` is populated for ``APPEND`` events and is ``None`` for every
    other kind. ``inode`` is the device-local inode number captured at the
    moment the event was generated; for ``DELETE`` it carries the last
    observed inode so downstream consumers can correlate against an earlier
    cursor.
    """

    kind: SourceEventKind
    session_id: str
    path: Path
    inode: int
    line: str | None
    timestamp: datetime


@dataclass(slots=True)
class FileCursor:
    """Per-file watcher state.

    ``offset`` is the byte offset of the next unread byte; ``pending``
    carries bytes read past the last newline that have not yet completed a
    line. A line is only emitted when its terminating ``\\n`` arrives.

    ``large_file_warned`` stays True for the lifetime of the cursor.
    The cursor itself is replaced on every ROTATE and TRUNCATE (see
    ``_handle_path``), so a file that shrinks and spikes again
    naturally gets a fresh cursor with ``large_file_warned=False`` and
    the next oversize growth re-warns. High-water-mark semantics on
    steadily-growing files would be too noisy and was deliberately
    rejected.
    """

    path: Path
    inode: int
    size: int
    offset: int
    pending: bytes = b""
    large_file_warned: bool = False


@runtime_checkable
class Source(Protocol):
    """Interface every watcher backend must honor.

    The aggregator calls ``poll()`` on its tick loop and consumes the
    iterator to exhaustion before returning to its own bookkeeping. ``poll``
    must not block. ``close`` releases any backend state; for the polling
    implementation it simply drops the cursor table.
    """

    def poll(self) -> Iterator[SourceEvent]: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _WalkResult:
    files: list[Path]
    overflowed: bool
    overflow_count: int


def _now() -> datetime:
    return datetime.now(tz=UTC)


class PollingSource:
    """Stat-and-read polling implementation of the ``Source`` protocol.

    Holds a per-file ``FileCursor`` table in memory; on every ``poll()``
    call walks the configured ``root`` (capped at ``max_files``), stats each
    discovered ``*.jsonl`` file, and yields ``SourceEvent`` records for the
    transitions documented in the module docstring.

    Constructor requires ``root`` to resolve to a path inside the user's
    home directory; any path outside ``$HOME`` raises ``PrivacyViolationError``
    before the source is usable. This is the runtime half of the filesystem
    scope rule (``docs/design.md`` §Privacy Enforcement); a CRITICAL error
    is also recorded on the error channel so operators see the attempt in
    the JSONL log.
    """

    def __init__(
        self,
        root: Path,
        *,
        interval: float = 2.0,
        max_files: int = 2000,
        large_file_warn_bytes: int = 10 * 1024 * 1024,
        cache_path: Path | None = None,
        seed_cursors: dict[Path, CachedCursor] | None = None,
    ) -> None:
        self._interval: float = interval
        self._max_files: int = max_files
        self._large_file_warn_bytes: int = large_file_warn_bytes
        self._cursors: dict[Path, FileCursor] = {}
        self._overflow_warned: bool = False
        self._root: Path = self._validate_root(root)
        # First-tick instrumentation: log cold-start costs at INFO so users
        # can see where the startup wall-clock went without enabling DEBUG.
        self._first_poll_done: bool = False
        self._bytes_read_this_poll: int = 0
        # Persistent cursor cache (Phase B): populated from disk at
        # construction time, consulted the first time ``_handle_path``
        # encounters each file, and written back from :meth:`close`.
        # Phase C passes pre-loaded seeds from the processed-session
        # store via ``seed_cursors``, taking precedence over ``cache_path``.
        self._cursor_store: CursorStore | None = None
        self._pending_seeds: dict[Path, CachedCursor] = {}
        if seed_cursors is not None:
            self._pending_seeds = dict(seed_cursors)
        elif cache_path is not None:
            self._cursor_store = CursorStore(cache_path, self._root)
            self._pending_seeds = self._cursor_store.load()
            _LOG.info(
                "watcher.cursor_cache_loaded path=%s entries=%d",
                cache_path,
                len(self._pending_seeds),
            )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def max_files(self) -> int:
        return self._max_files

    # ------------------------------------------------------------------ scope

    @staticmethod
    def _validate_root(root: Path) -> Path:
        """Resolve the root once and refuse anything outside ``$HOME``."""

        resolved_root = root.expanduser().resolve()
        home = Path.home().resolve()
        if not resolved_root.is_relative_to(home):
            err = CodevigilError(
                level=ErrorLevel.CRITICAL,
                source=ErrorSource.WATCHER,
                code="watcher.path_scope_violation",
                message=(
                    f"watcher root {str(resolved_root)!r} is outside the user "
                    f"home directory {str(home)!r}; refusing to walk"
                ),
                context={
                    "root": str(resolved_root),
                    "home": str(home),
                },
            )
            record(err)
            raise PrivacyViolationError(err.message)
        return resolved_root

    # ------------------------------------------------------------------- walk

    def _walk(self) -> _WalkResult:
        """Return the deterministic, capped list of session files under root.

        Walks the tree with ``os.scandir`` and collects every regular file
        ending in ``.jsonl``. Results are sorted by absolute path so the
        "first ``max_files``" slice is stable across polls and platforms.
        """

        walk_started = time.monotonic()
        discovered: list[Path] = []
        if not self._root.exists():
            return _WalkResult(files=[], overflowed=False, overflow_count=0)

        stack: list[Path] = [self._root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                        discovered.append(Path(entry.path))
                except OSError:
                    continue

        discovered.sort(key=lambda p: str(p))
        walk_elapsed = time.monotonic() - walk_started
        _LOG.debug(
            "watcher.walk elapsed_ms=%.2f discovered=%d max_files=%d",
            walk_elapsed * 1000.0,
            len(discovered),
            self._max_files,
        )
        if len(discovered) > self._max_files:
            return _WalkResult(
                files=discovered[: self._max_files],
                overflowed=True,
                overflow_count=len(discovered) - self._max_files,
            )
        return _WalkResult(files=discovered, overflowed=False, overflow_count=0)

    # ------------------------------------------------------------------- poll

    def poll(self) -> Iterator[SourceEvent]:
        """Yield one ``SourceEvent`` per state transition since the last call.

        The iterator is materialised eagerly into a list and returned via
        ``iter()``: the aggregator wants stable ordering and the disk reads
        happen inside this call, not lazily inside the consumer's loop.
        """

        poll_started = time.monotonic()
        self._bytes_read_this_poll = 0
        events: list[SourceEvent] = []
        walk = self._walk()
        if walk.overflowed and not self._overflow_warned:
            self._overflow_warned = True
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.WATCHER,
                    code="watcher.bounded_walk_overflow",
                    message=(
                        f"watcher walk exceeded max_files={self._max_files}; "
                        f"{walk.overflow_count} file(s) skipped"
                    ),
                    context={
                        "max_files": self._max_files,
                        "overflow_count": walk.overflow_count,
                        "root": str(self._root),
                    },
                )
            )

        seen_paths: set[Path] = set()
        for path in walk.files:
            seen_paths.add(path)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                # File vanished between scandir and stat; treat as delete on
                # the next pass once the cursor sees it missing.
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            self._handle_path(path, st.st_ino, st.st_size, st.st_mtime, events)

        # Detect deletions: any cursored path that no longer appears in the
        # walk has been removed from the watched tree.
        deleted = [p for p in self._cursors if p not in seen_paths]
        for path in sorted(deleted, key=lambda p: str(p)):
            cursor = self._cursors.pop(path)
            events.append(
                SourceEvent(
                    kind=SourceEventKind.DELETE,
                    session_id=path.stem,
                    path=path,
                    inode=cursor.inode,
                    line=None,
                    timestamp=_now(),
                )
            )

        poll_elapsed_ms = (time.monotonic() - poll_started) * 1000.0
        fmt = "%s elapsed_ms=%.2f events=%d bytes_read=%d files_tracked=%d"
        if not self._first_poll_done:
            self._first_poll_done = True
            _LOG.info(
                fmt,
                "watcher.first_poll",
                poll_elapsed_ms,
                len(events),
                self._bytes_read_this_poll,
                len(self._cursors),
            )
        else:
            _LOG.debug(
                fmt,
                "watcher.poll",
                poll_elapsed_ms,
                len(events),
                self._bytes_read_this_poll,
                len(self._cursors),
            )

        return iter(events)

    def close(self) -> None:
        """Drop the cursor table. No OS handles are held between polls.

        When a persistent cursor cache is configured, the current cursor
        table is flushed to disk before being cleared so the next
        ``watch`` invocation can resume from the last known offsets.
        """

        if self._cursor_store is not None:
            self.flush_cursor_cache()
        self._cursors.clear()

    def flush_cursor_cache(self) -> None:
        """Write the current cursor table to the persistent cache file.

        No-op when no ``cache_path`` was supplied at construction time.
        Exposed as a public method so tests and long-lived processes can
        flush without tearing down the source.
        """

        if self._cursor_store is None:
            return
        snapshot: dict[Path, CachedCursor] = {}
        for path, cursor in self._cursors.items():
            try:
                mtime = path.stat().st_mtime
            except (FileNotFoundError, PermissionError):
                continue
            prefix_fingerprint, prefix_bytes = prefix_fingerprint_for_path(path)
            snapshot[path] = CachedCursor(
                inode=cursor.inode,
                size=cursor.size,
                offset=cursor.offset,
                pending=cursor.pending,
                mtime=mtime,
                prefix_fingerprint=prefix_fingerprint,
                prefix_bytes=prefix_bytes,
            )
        self._cursor_store.save(snapshot)
        _LOG.info(
            "watcher.cursor_cache_saved path=%s entries=%d",
            self._cursor_store.cache_path,
            len(snapshot),
        )

    # --------------------------------------------------------------- internals

    def _handle_path(
        self,
        path: Path,
        inode: int,
        size: int,
        mtime: float,
        events: list[SourceEvent],
    ) -> None:
        cursor = self._cursors.get(path)
        if cursor is None:
            seed = self._pending_seeds.pop(path, None)
            self._handle_new(path, inode, size, mtime, events, seed=seed)
            return
        if inode != cursor.inode:
            events.append(
                SourceEvent(
                    kind=SourceEventKind.ROTATE,
                    session_id=path.stem,
                    path=path,
                    inode=inode,
                    line=None,
                    timestamp=_now(),
                )
            )
            self._cursors.pop(path, None)
            self._handle_new(path, inode, size, mtime, events, emit_new_session=False)
            return
        if size < cursor.size:
            events.append(
                SourceEvent(
                    kind=SourceEventKind.TRUNCATE,
                    session_id=path.stem,
                    path=path,
                    inode=inode,
                    line=None,
                    timestamp=_now(),
                )
            )
            self._cursors.pop(path, None)
            self._handle_new(path, inode, size, mtime, events, emit_new_session=False)
            return
        if size > cursor.size:
            growth = size - cursor.offset
            self._maybe_warn_large_growth(path, cursor, growth)
            self._read_and_emit(path, cursor, size, events)

    def _handle_new(
        self,
        path: Path,
        inode: int,
        size: int,
        mtime: float,
        events: list[SourceEvent],
        *,
        emit_new_session: bool = True,
        seed: CachedCursor | None = None,
    ) -> None:
        # Seeded resume: accept the cached offset only when the file's
        # identity matches and its size has not shrunk. Any mismatch
        # falls through to a fresh full-replay cursor (offset = 0).
        start_offset = 0
        pending = b""
        accepted_seed = (
            seed if self._seed_matches_file(path, inode=inode, size=size, seed=seed) else None
        )
        if accepted_seed is not None:
            start_offset = accepted_seed.offset
            pending = accepted_seed.pending
        cursor = FileCursor(
            path=path,
            inode=inode,
            size=start_offset,
            offset=start_offset,
            pending=pending,
        )
        self._cursors[path] = cursor
        if emit_new_session:
            # Use the file's mtime as the NEW_SESSION timestamp so the
            # aggregator can back-date the monotonic cursor for pre-existing
            # files replayed at startup.  For files that just appeared, mtime
            # and _now() are equivalent within milliseconds.
            mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
            events.append(
                SourceEvent(
                    kind=SourceEventKind.NEW_SESSION,
                    session_id=path.stem,
                    path=path,
                    inode=inode,
                    line=None,
                    timestamp=mtime_dt,
                )
            )
        if size > start_offset:
            self._maybe_warn_large_growth(path, cursor, size - start_offset)
            self._read_and_emit(path, cursor, size, events)

    def _seed_matches_file(
        self,
        path: Path,
        *,
        inode: int,
        size: int,
        seed: CachedCursor | None,
    ) -> bool:
        if seed is None:
            return False
        if seed.inode != inode or seed.size > size:
            return False
        if not seed.prefix_fingerprint or seed.prefix_bytes <= 0:
            return True
        try:
            with path.open("rb") as handle:
                prefix = handle.read(seed.prefix_bytes)
        except (FileNotFoundError, PermissionError):
            return False
        return hashlib.sha256(prefix).hexdigest() == seed.prefix_fingerprint

    def _maybe_warn_large_growth(
        self,
        path: Path,
        cursor: FileCursor,
        growth: int,
    ) -> None:
        if growth <= self._large_file_warn_bytes or cursor.large_file_warned:
            return
        cursor.large_file_warned = True
        record(
            CodevigilError(
                level=ErrorLevel.WARN,
                source=ErrorSource.WATCHER,
                code="watcher.large_file_growth",
                message=(
                    f"file {str(path)!r} grew {growth} bytes in a single poll "
                    f"(threshold {self._large_file_warn_bytes}); processing anyway"
                ),
                context={
                    "path": str(path),
                    "growth": growth,
                    "threshold": self._large_file_warn_bytes,
                },
            )
        )

    def _read_and_emit(
        self,
        path: Path,
        cursor: FileCursor,
        new_size: int,
        events: list[SourceEvent],
    ) -> None:
        """Read from ``cursor.offset`` to ``new_size`` in 1 MiB chunks.

        Bytes read are appended to ``pending``; whenever ``pending`` contains
        a newline, every complete line is split off and emitted as an APPEND
        event. The trailing fragment (no newline yet) stays in ``pending``
        for the next poll.
        """

        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return
        try:
            handle.seek(cursor.offset)
            remaining = new_size - cursor.offset
            while remaining > 0:
                chunk = handle.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                cursor.pending += chunk
                remaining -= len(chunk)
                self._bytes_read_this_poll += len(chunk)
                while b"\n" in cursor.pending:
                    line_bytes, _, rest = cursor.pending.partition(b"\n")
                    cursor.pending = rest
                    line = line_bytes.decode("utf-8", errors="replace")
                    events.append(
                        SourceEvent(
                            kind=SourceEventKind.APPEND,
                            session_id=path.stem,
                            path=path,
                            inode=cursor.inode,
                            line=line,
                            timestamp=_now(),
                        )
                    )
            cursor.offset = handle.tell()
        finally:
            handle.close()
        cursor.size = new_size


# ``field`` is re-exported for symmetry with ``codevigil.types``; downstream
# phases that build on ``FileCursor`` may want default_factory without a
# second dataclasses import.
__all__ = [
    "FileCursor",
    "PollingSource",
    "Source",
    "SourceEvent",
    "SourceEventKind",
    "field",
]
