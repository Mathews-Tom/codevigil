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
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.privacy import PrivacyViolationError

_CHUNK_SIZE: int = 1 * 1024 * 1024  # 1 MiB delta read chunk


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
    """

    path: Path
    inode: int
    size: int
    offset: int
    pending: bytes = b""


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


def _now() -> datetime:
    return datetime.now(tz=UTC)


class PollingSource:
    """Stat-and-read polling implementation of the ``Source`` protocol.

    Holds a per-file ``FileCursor`` table in memory; on every ``poll()``
    call walks the configured ``root``, stats each discovered ``*.jsonl``
    file, and yields ``SourceEvent`` records for the transitions documented
    in the module docstring.
    """

    def __init__(
        self,
        root: Path,
        *,
        interval: float = 2.0,
    ) -> None:
        self._interval: float = interval
        self._cursors: dict[Path, FileCursor] = {}
        self._root: Path = self._validate_root(root)

    # ------------------------------------------------------------------ scope

    @staticmethod
    def _validate_root(root: Path) -> Path:
        """Resolve the root once and refuse anything outside ``$HOME``.

        Runtime half of the filesystem-scope rule from
        ``docs/design.md`` §Privacy Enforcement. A CRITICAL error is recorded
        on the error channel before ``PrivacyViolationError`` is raised so
        operators see the attempt in the JSONL log even though the constructor
        also propagates the exception to the caller.
        """

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

    @property
    def root(self) -> Path:
        return self._root

    @property
    def interval(self) -> float:
        return self._interval

    # ------------------------------------------------------------------- walk

    def _walk(self) -> list[Path]:
        """Return the deterministic list of session files under root.

        Walks the tree with ``os.scandir`` and collects every regular file
        ending in ``.jsonl``. Results are sorted by absolute path so the
        traversal order is stable across polls and platforms.
        """

        discovered: list[Path] = []
        if not self._root.exists():
            return discovered

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
        return discovered

    # ------------------------------------------------------------------- poll

    def poll(self) -> Iterator[SourceEvent]:
        """Yield one ``SourceEvent`` per state transition since the last call.

        The iterator is materialised eagerly into a list and returned via
        ``iter()``: the aggregator wants stable ordering and the disk reads
        happen inside this call, not lazily inside the consumer's loop.
        """

        events: list[SourceEvent] = []
        files = self._walk()

        seen_paths: set[Path] = set()
        for path in files:
            seen_paths.add(path)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            self._handle_path(path, st.st_ino, st.st_size, events)

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

        return iter(events)

    def close(self) -> None:
        """Drop the cursor table. No OS handles are held between polls."""

        self._cursors.clear()

    # --------------------------------------------------------------- internals

    def _handle_path(
        self,
        path: Path,
        inode: int,
        size: int,
        events: list[SourceEvent],
    ) -> None:
        cursor = self._cursors.get(path)
        if cursor is None:
            self._handle_new(path, inode, size, events)
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
            self._handle_new(path, inode, size, events, emit_new_session=False)
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
            self._handle_new(path, inode, size, events, emit_new_session=False)
            return
        if size > cursor.size:
            self._read_and_emit(path, cursor, size, events)

    def _handle_new(
        self,
        path: Path,
        inode: int,
        size: int,
        events: list[SourceEvent],
        *,
        emit_new_session: bool = True,
    ) -> None:
        cursor = FileCursor(path=path, inode=inode, size=0, offset=0, pending=b"")
        self._cursors[path] = cursor
        if emit_new_session:
            events.append(
                SourceEvent(
                    kind=SourceEventKind.NEW_SESSION,
                    session_id=path.stem,
                    path=path,
                    inode=inode,
                    line=None,
                    timestamp=_now(),
                )
            )
        if size > 0:
            self._read_and_emit(path, cursor, size, events)

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
