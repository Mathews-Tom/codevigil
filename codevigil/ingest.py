"""Cold-ingest workflow: walks the watch root, parses every JSONL
session file, and persists each session's final metadata + metrics to
the :class:`~codevigil.analysis.processed_store.ProcessedSessionStore`.

Design notes
------------

- The ingest pipeline **bypasses** :class:`~codevigil.watcher.PollingSource`
  and :meth:`~codevigil.aggregator.SessionAggregator.tick`, synthesising
  :class:`~codevigil.watcher.SourceEvent` records directly from walked
  files. This is the right call for ingest because we want per-file
  progress reporting (rich :class:`~rich.progress.Progress`), and the
  live tick loop processes all discovered files in one atomic
  ``source.poll()`` call which is opaque to the progress bar.

- After each file's lines have been dispatched, we snapshot the
  session's current state out of the aggregator, upsert it to the
  processed-session store, and evict it from the aggregator so memory
  stays bounded even when ingesting thousands of session files.

- Lifecycle-timing config is pinned to huge stale/evict values during
  ingest so the lifecycle pass cannot remove a session mid-ingest
  before we have a chance to persist it. The real watch config is
  restored afterwards (we construct a separate config dict for the
  ingest run, leaving the caller's config untouched).

- Ingest is **idempotent**. A second run against an unchanged watch
  root produces identical DB rows. If the caller passes ``--force``,
  existing rows are overwritten; otherwise files whose ``(inode,
  size, mtime)`` matches the stored record are skipped.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from codevigil.aggregator import SessionAggregator
from codevigil.analysis.processed_store import (
    ProcessedMetric,
    ProcessedSession,
    ProcessedSessionStore,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import SessionState
from codevigil.watcher import SourceEvent, SourceEventKind

_HUGE_LIFECYCLE_SECONDS: int = 10_000_000_000


@dataclass(slots=True)
class IngestResult:
    """Summary of one ``codevigil ingest`` run."""

    sessions_processed: int
    sessions_skipped: int
    files_walked: int
    bytes_read: int
    db_path: Path


def _walk_jsonl_files(root: Path) -> list[Path]:
    """Deterministic recursive walk; returns every ``*.jsonl`` file."""

    if not root.exists():
        return []
    discovered: list[Path] = []
    stack: list[Path] = [root]
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


def _build_ingest_config(base: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied config with lifecycle thresholds pinned high."""

    watch = dict(base.get("watch", {}))
    watch["stale_after_seconds"] = _HUGE_LIFECYCLE_SECONDS
    watch["evict_after_seconds"] = _HUGE_LIFECYCLE_SECONDS
    out = dict(base)
    out["watch"] = watch
    return out


def _stat_path(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except (FileNotFoundError, PermissionError):
        return None


def _should_skip(
    store: ProcessedSessionStore,
    path: Path,
    inode: int,
    size: int,
    mtime: float,
    *,
    force: bool,
) -> bool:
    """Return True when the stored record for ``path`` matches the file on disk."""

    if force:
        return False
    record = store.get_by_path(path)
    if record is None:
        return False
    return record.inode == inode and record.size == size and abs(record.mtime - mtime) < 1e-3


def _feed_file(
    aggregator: SessionAggregator,
    path: Path,
    stat: os.stat_result,
) -> int:
    """Dispatch one file's lines through ``aggregator._dispatch_source_event``.

    Returns the byte count read for progress accounting.
    """

    mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    now = datetime.now(tz=UTC)
    # NEW_SESSION uses the file's mtime so back-dating works.
    new_session = SourceEvent(
        kind=SourceEventKind.NEW_SESSION,
        session_id=path.stem,
        path=path,
        inode=stat.st_ino,
        line=None,
        timestamp=mtime_dt,
    )
    # Private API: the aggregator does not expose a public "ingest one
    # SourceEvent" entry point, so we reach through the private method.
    # Intentional: ingest is a codevigil-internal workflow and shares
    # the aggregator's dispatch invariants.
    aggregator._dispatch_source_event(new_session)
    bytes_read = 0
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, PermissionError):
        return 0
    bytes_read = len(raw)
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line:
            continue
        append = SourceEvent(
            kind=SourceEventKind.APPEND,
            session_id=path.stem,
            path=path,
            inode=stat.st_ino,
            line=line,
            timestamp=now,
        )
        aggregator._dispatch_source_event(append)
    return bytes_read


def _snapshot_to_record(
    aggregator: SessionAggregator,
    path: Path,
    stat: os.stat_result,
    project_registry: ProjectRegistry,
) -> ProcessedSession | None:
    """Turn a freshly-ingested aggregator session into a
    :class:`ProcessedSession` ready for persistence."""

    ctx = aggregator.sessions.get(path.stem)
    if ctx is None:
        return None
    # Taking the snapshot also drives the collectors' ``snapshot()``
    # path so rolling metrics that report on close are captured.
    snapshots = aggregator._snapshot_session(ctx)
    project_name: str | None = project_registry.resolve(ctx.project_hash) or None

    metrics: list[ProcessedMetric] = []
    for snap in snapshots:
        metrics.append(
            ProcessedMetric(
                collector_name=snap.name,
                metric_name=snap.name,
                value=float(snap.value),
                severity=snap.severity.value,
                label=snap.label,
                detail=dict(snap.detail) if snap.detail else None,
            )
        )

    collector_state = aggregator.serialize_collector_state(ctx)

    return ProcessedSession(
        session_id=ctx.session_id,
        path=path,
        inode=stat.st_ino,
        size=stat.st_size,
        offset=stat.st_size,
        pending=b"",
        mtime=stat.st_mtime,
        project_hash=ctx.project_hash,
        project_name=project_name,
        first_event_time=ctx.first_event_time,
        last_event_time=ctx.last_event_time,
        event_count=ctx.event_count,
        session_task_type=None,
        collector_state=collector_state,
        metrics=metrics,
    )


def run_ingest(
    *,
    root: Path,
    store: ProcessedSessionStore,
    config: dict[str, Any],
    console: Console,
    force: bool = False,
) -> IngestResult:
    """Ingest every JSONL file under ``root`` into ``store``.

    ``console`` drives the live rich :class:`~rich.progress.Progress`
    display; pass a ``Console(quiet=True)`` in tests to suppress output.
    ``config`` is the caller's resolved config (from
    :func:`codevigil.config.load_config`); it is copied internally before
    lifecycle thresholds are pinned so the caller's config is never
    mutated.
    """

    files = _walk_jsonl_files(root)
    ingest_cfg = _build_ingest_config(config)
    project_registry = ProjectRegistry()
    aggregator = SessionAggregator(
        source=_NullSource(),
        config=ingest_cfg,
        project_registry=project_registry,
    )

    processed = 0
    skipped = 0
    bytes_read = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
    with progress:
        task = progress.add_task(
            f"Ingesting {len(files)} session file(s)",
            total=len(files),
        )
        for path in files:
            stat = _stat_path(path)
            if stat is None:
                progress.advance(task)
                continue
            if _should_skip(
                store,
                path,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime,
                force=force,
            ):
                skipped += 1
                progress.advance(task)
                continue

            file_bytes = _feed_file(aggregator, path, stat)
            bytes_read += file_bytes
            record = _snapshot_to_record(aggregator, path, stat, project_registry)
            if record is not None:
                store.upsert_session(record)
                processed += 1

            # Evict the session from the aggregator to bound memory.
            ctx = aggregator.sessions.get(path.stem)
            if ctx is not None:
                ctx.state = SessionState.EVICTED
                aggregator.sessions.pop(path.stem, None)
            progress.advance(task)

    with contextlib.suppress(BaseException):  # pragma: no cover - defensive
        aggregator.close()

    return IngestResult(
        sessions_processed=processed,
        sessions_skipped=skipped,
        files_walked=len(files),
        bytes_read=bytes_read,
        db_path=store.db_path,
    )


class _NullSource:
    """In-memory stub that satisfies the ``Source`` protocol for ingest.

    The aggregator treats the ``Source`` as authoritative for polling,
    but in ingest mode we feed events directly via
    ``_dispatch_source_event`` and never call :meth:`poll`. The null
    source exists so ``SessionAggregator.__init__`` has something to
    hold onto; its methods do nothing.
    """

    def poll(self) -> Any:
        return iter(())

    def close(self) -> None:
        return None


__all__ = ["IngestResult", "run_ingest"]
