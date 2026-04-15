"""Local SQLite-backed memory of processed Claude Code sessions.

This module is the backbone of the ``codevigil ingest`` /
``codevigil watch`` split. It is deliberately a **local-only system
memory**: no network, no telemetry, no cross-machine sync. The database
file lives under ``~/.local/state/codevigil/processed_sessions.db`` (or
wherever ``ingest.db_path`` points) and stores one row per JSONL session
file that codevigil has already ingested.

Purpose
-------

Before Phase C, every ``codevigil watch`` invocation walked the watch
root and re-read every JSONL file from byte 0, replaying weeks of
historical events through the aggregator on every startup. The
processed-session store breaks that cycle by persisting, per session:

- The file path, inode, last observed byte size, and cursor offset so
  watcher resumes know whether they have to touch the file at all.
- Session identity (project hash, project name, task type) so the TUI
  can display meaningful labels without re-parsing historical events.
- The final per-metric :class:`~codevigil.types.MetricSnapshot` values
  from the last run so the header and per-row rendering have real
  state to show on restart instead of "warming up" zeros everywhere.
- A free-form ``collector_state_json`` blob per collector, opaque to
  the store, that individual collectors may use to round-trip their
  internal rolling-window state across restarts (Phase C5 work — the
  store itself does not care about the shape).

Consistency model
-----------------

- Single-writer, any-readers. ``codevigil ingest`` and
  ``codevigil watch`` both write, but they are never run concurrently
  against the same database — ``ingest`` runs to completion before
  ``watch`` starts. Concurrent reads from other tools (e.g. ``report``
  inspecting the store for dashboards) are safe because SQLite's
  default journal mode supports them.
- Writes are wrapped in a transaction per ``upsert_session`` call.
- Schema version is tracked in a ``schema_version`` table; on open the
  store asserts the current file is at the expected version and
  raises ``ProcessedStoreError`` on any mismatch. Migrations are
  additive when at all possible; breaking migrations bump the major
  version and clear the database.

This module intentionally does **not** depend on the aggregator or on
any collector. It speaks in plain dicts and scalar values; all
translation into / out of ``SessionMeta`` and ``MetricSnapshot`` is the
caller's responsibility. This keeps the store unit-testable in
isolation and makes the schema easy to reason about without chasing
type imports across three packages.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record

_SCHEMA_VERSION: int = 1

_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_sessions (
        session_id          TEXT PRIMARY KEY,
        path                TEXT NOT NULL,
        inode               INTEGER NOT NULL,
        size                INTEGER NOT NULL,
        offset              INTEGER NOT NULL,
        pending_b64         TEXT NOT NULL DEFAULT '',
        mtime               REAL NOT NULL,
        project_hash        TEXT NOT NULL,
        project_name        TEXT,
        first_event_time    TEXT NOT NULL,
        last_event_time     TEXT NOT NULL,
        event_count         INTEGER NOT NULL,
        session_task_type   TEXT,
        collector_state_json TEXT NOT NULL DEFAULT '{}',
        updated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_processed_sessions_path
        ON processed_sessions(path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_processed_sessions_project
        ON processed_sessions(project_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_processed_sessions_last_event
        ON processed_sessions(last_event_time)
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_metrics (
        session_id      TEXT NOT NULL,
        collector_name  TEXT NOT NULL,
        metric_name     TEXT NOT NULL,
        value           REAL NOT NULL,
        severity        TEXT NOT NULL,
        label           TEXT NOT NULL,
        detail_json     TEXT,
        PRIMARY KEY (session_id, collector_name, metric_name),
        FOREIGN KEY (session_id)
            REFERENCES processed_sessions(session_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_processed_metrics_session
        ON processed_metrics(session_id)
    """,
)


class ProcessedStoreError(Exception):
    """Raised for unrecoverable processed-store errors (schema mismatch, IO).

    Carries a structured ``code`` and ``message`` so callers can route
    the failure through the standard error channel. Declared as a plain
    ``Exception`` subclass rather than a ``CodevigilError`` subclass
    because ``CodevigilError`` is a frozen slotted dataclass and Python's
    ``raise`` machinery cannot attach a traceback to frozen instances.
    """

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_error_record(self) -> CodevigilError:
        return CodevigilError(
            level=ErrorLevel.CRITICAL,
            source=ErrorSource.AGGREGATOR,
            code=self.code,
            message=self.message,
            context={},
        )

    def record(self) -> None:
        record(self.to_error_record())


@dataclass(slots=True)
class ProcessedMetric:
    """One persisted metric reading for a processed session."""

    collector_name: str
    metric_name: str
    value: float
    severity: str
    label: str
    detail: dict[str, object] | None = None


@dataclass(slots=True)
class RecentProjectAggregate:
    """One project-level aggregation row for the project-view TUI.

    Returned by
    :meth:`ProcessedSessionStore.iter_recent_project_aggregates` and
    consumed by the terminal renderer to populate the project-row view
    from the persistent memory (the store), so watch users see their
    top-N most recent projects even when the in-memory aggregator
    cohort is empty after the cold-start lifecycle pass.
    """

    project_key: str
    project_hash: str
    project_name: str | None
    session_count: int
    last_event_time: datetime
    metrics: list[ProcessedMetric] = field(default_factory=list)


@dataclass(slots=True)
class ProcessedSession:
    """One persisted session record.

    Fields mirror ``processed_sessions`` column-for-column. The
    ``metrics`` list is joined in from ``processed_metrics`` on read;
    on write the caller passes a list alongside the main record.
    """

    session_id: str
    path: Path
    inode: int
    size: int
    offset: int
    pending: bytes
    mtime: float
    project_hash: str
    project_name: str | None
    first_event_time: datetime
    last_event_time: datetime
    event_count: int
    session_task_type: str | None
    collector_state: dict[str, dict[str, object]] = field(default_factory=dict)
    metrics: list[ProcessedMetric] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class ProcessedSessionStore:
    """Local-only SQLite memory of processed sessions.

    One instance per process. Safe to keep open for the lifetime of a
    ``codevigil ingest`` or ``codevigil watch`` run; closed via
    :meth:`close` or via the context manager returned by :meth:`connect`.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path: Path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ----------------------------------------------------------- lifecycle

    def open(self) -> None:
        """Create the database file and schema if needed.

        Idempotent. Safe to call on an existing database; validates
        the schema version and raises :class:`ProcessedStoreError` on
        mismatch.
        """

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._transaction():
            for statement in _SCHEMA_SQL:
                self._conn.execute(statement)
            current = self._read_schema_version()
            if current is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            elif current != _SCHEMA_VERSION:
                raise ProcessedStoreError(
                    code="processed_store.schema_mismatch",
                    message=(
                        f"database at {self._db_path!s} is schema v{current}, "
                        f"but codevigil expects v{_SCHEMA_VERSION}; refusing to "
                        f"open — delete the file and re-run codevigil ingest"
                    ),
                )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ProcessedSessionStore:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- schema

    def _read_schema_version(self) -> int | None:
        assert self._conn is not None
        row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            return None
        return int(row[0])

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        assert self._conn is not None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ----------------------------------------------------------- writes

    def upsert_session(self, record: ProcessedSession) -> None:
        """Insert or replace a processed-session record and its metrics."""

        assert self._conn is not None, "store must be opened before use"
        with self._transaction():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO processed_sessions (
                    session_id, path, inode, size, offset, pending_b64,
                    mtime, project_hash, project_name,
                    first_event_time, last_event_time, event_count,
                    session_task_type, collector_state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    str(record.path),
                    record.inode,
                    record.size,
                    record.offset,
                    base64.b64encode(record.pending).decode("ascii"),
                    record.mtime,
                    record.project_hash,
                    record.project_name,
                    record.first_event_time.isoformat(),
                    record.last_event_time.isoformat(),
                    record.event_count,
                    record.session_task_type,
                    json.dumps(record.collector_state, sort_keys=True),
                    record.updated_at.isoformat(),
                ),
            )
            self._conn.execute(
                "DELETE FROM processed_metrics WHERE session_id = ?",
                (record.session_id,),
            )
            for metric in record.metrics:
                self._conn.execute(
                    """
                    INSERT INTO processed_metrics (
                        session_id, collector_name, metric_name,
                        value, severity, label, detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.session_id,
                        metric.collector_name,
                        metric.metric_name,
                        float(metric.value),
                        metric.severity,
                        metric.label,
                        (
                            json.dumps(metric.detail, sort_keys=True)
                            if metric.detail is not None
                            else None
                        ),
                    ),
                )

    def delete_session(self, session_id: str) -> None:
        """Remove a session and its metrics. No-op if the row is absent."""

        assert self._conn is not None
        with self._transaction():
            self._conn.execute(
                "DELETE FROM processed_sessions WHERE session_id = ?",
                (session_id,),
            )

    # ----------------------------------------------------------- reads

    def get_session(self, session_id: str) -> ProcessedSession | None:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT session_id, path, inode, size, offset, pending_b64,
                   mtime, project_hash, project_name,
                   first_event_time, last_event_time, event_count,
                   session_task_type, collector_state_json, updated_at
            FROM processed_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        metrics = self._load_metrics(row[0])
        return _row_to_session(row, metrics)

    def get_by_path(self, path: Path) -> ProcessedSession | None:
        """Return the record keyed by file path, or ``None``."""

        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT session_id, path, inode, size, offset, pending_b64,
                   mtime, project_hash, project_name,
                   first_event_time, last_event_time, event_count,
                   session_task_type, collector_state_json, updated_at
            FROM processed_sessions WHERE path = ? LIMIT 1
            """,
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        metrics = self._load_metrics(row[0])
        return _row_to_session(row, metrics)

    def iter_all(self) -> Iterator[ProcessedSession]:
        """Yield every processed session in last-event-time order (newest first)."""

        assert self._conn is not None
        cursor = self._conn.execute(
            """
            SELECT session_id, path, inode, size, offset, pending_b64,
                   mtime, project_hash, project_name,
                   first_event_time, last_event_time, event_count,
                   session_task_type, collector_state_json, updated_at
            FROM processed_sessions ORDER BY last_event_time DESC
            """
        )
        for row in cursor:
            metrics = self._load_metrics(row[0])
            yield _row_to_session(row, metrics)

    def count(self) -> int:
        assert self._conn is not None
        row = self._conn.execute("SELECT COUNT(*) FROM processed_sessions").fetchone()
        return int(row[0]) if row else 0

    def iter_recent_project_aggregates(self, limit: int) -> list[RecentProjectAggregate]:
        """Return the top-``limit`` most recently-active projects.

        One row per project, aggregated across every persisted session
        that shares a ``(project_name, project_hash)`` key. For each
        project we record:

        - ``project_key`` — ``project_name`` when known, else
          ``project_hash[:8]``.
        - ``session_count`` — total persisted sessions for the project.
        - ``last_event_time`` — max across all those sessions.
        - ``metrics`` — the full metric list from the single most-recent
          session in that project (we do not roll metrics up across
          sessions here; the caller can re-derive if needed).

        Projects are returned sorted by ``last_event_time`` descending.
        The SQL uses a single ``GROUP BY`` + a bounded loop to fetch
        per-project metric slices so watch ticks stay O(limit) regardless
        of the total session count in the store.
        """

        assert self._conn is not None
        group_sql = """
            SELECT
                COALESCE(NULLIF(project_name, ''), substr(project_hash, 1, 8))
                    AS project_key,
                project_hash,
                project_name,
                COUNT(*) AS session_count,
                MAX(last_event_time) AS latest_event_time
            FROM processed_sessions
            GROUP BY project_key
            ORDER BY latest_event_time DESC
            LIMIT ?
        """
        rows = self._conn.execute(group_sql, (int(limit),)).fetchall()

        out: list[RecentProjectAggregate] = []
        for row in rows:
            project_key = str(row[0])
            project_hash = str(row[1])
            project_name_val = row[2]
            project_name: str | None = str(project_name_val) if project_name_val else None
            session_count = int(row[3])
            latest_event_time = datetime.fromisoformat(str(row[4]))

            latest_session_row = self._conn.execute(
                """
                SELECT session_id FROM processed_sessions
                WHERE COALESCE(NULLIF(project_name, ''), substr(project_hash, 1, 8)) = ?
                ORDER BY last_event_time DESC
                LIMIT 1
                """,
                (project_key,),
            ).fetchone()
            metrics: list[ProcessedMetric] = []
            if latest_session_row is not None:
                metrics = self._load_metrics(str(latest_session_row[0]))

            out.append(
                RecentProjectAggregate(
                    project_key=project_key,
                    project_hash=project_hash,
                    project_name=project_name,
                    session_count=session_count,
                    last_event_time=latest_event_time,
                    metrics=metrics,
                )
            )
        return out

    def _load_metrics(self, session_id: str) -> list[ProcessedMetric]:
        assert self._conn is not None
        out: list[ProcessedMetric] = []
        for row in self._conn.execute(
            """
            SELECT collector_name, metric_name, value, severity, label, detail_json
            FROM processed_metrics WHERE session_id = ?
            ORDER BY collector_name, metric_name
            """,
            (session_id,),
        ):
            detail: dict[str, object] | None = None if row[5] is None else json.loads(row[5])
            out.append(
                ProcessedMetric(
                    collector_name=row[0],
                    metric_name=row[1],
                    value=float(row[2]),
                    severity=row[3],
                    label=row[4],
                    detail=detail,
                )
            )
        return out


def _row_to_session(
    row: tuple[Any, ...],
    metrics: list[ProcessedMetric],
) -> ProcessedSession:
    return ProcessedSession(
        session_id=str(row[0]),
        path=Path(str(row[1])),
        inode=int(row[2]),
        size=int(row[3]),
        offset=int(row[4]),
        pending=base64.b64decode(str(row[5])),
        mtime=float(row[6]),
        project_hash=str(row[7]),
        project_name=str(row[8]) if row[8] is not None else None,
        first_event_time=datetime.fromisoformat(str(row[9])),
        last_event_time=datetime.fromisoformat(str(row[10])),
        event_count=int(row[11]),
        session_task_type=str(row[12]) if row[12] is not None else None,
        collector_state=json.loads(str(row[13])),
        metrics=metrics,
        updated_at=datetime.fromisoformat(str(row[14])),
    )


def default_db_path() -> Path:
    """Return the default on-disk location for the processed-session DB."""

    return Path("~/.local/state/codevigil/processed_sessions.db").expanduser()


__all__ = [
    "ProcessedMetric",
    "ProcessedSession",
    "ProcessedSessionStore",
    "ProcessedStoreError",
    "RecentProjectAggregate",
    "default_db_path",
]
