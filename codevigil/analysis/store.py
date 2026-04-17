"""Append-only on-disk index of finalised session reports.

The store writes one JSON file per session under the XDG state directory
(``$XDG_STATE_HOME/codevigil/sessions/`` or
``~/.local/state/codevigil/sessions/`` when ``XDG_STATE_HOME`` is not set).

Design decisions:
- One file per session, named ``<session_id>.json``. Append-only at the
  directory level: once written, a file is never modified in place. If the
  aggregator evicts and recreates a session with the same id, the new report
  overwrites the old one atomically via a ``tmp→rename`` dance.
- No database dependency. The store is a thin wrapper over the filesystem.
  Reads enumerate and filter on the fly; there is no index to corrupt.
- ``schema_version`` is a first-class field on every record. Starting at 1.
  Future phases that add columns bump the version and ship a one-way migrator
  in :func:`_migrate_record`. See the migration policy in ``docs/design.md``.
- Persistence is opt-in. The aggregator checks
  ``config["storage"]["enable_persistence"]`` before calling
  :meth:`SessionStore.write`. Nothing is written unless the caller explicitly
  enables it. The first write logs a single-line activation notice naming the
  target directory.

Session report schema (``schema_version = 1``):

.. code-block:: json

    {
        "schema_version": 1,
        "session_id": "agent-abc123",
        "project_hash": "abc12345",
        "project_name": null,
        "model": null,
        "permission_mode": null,
        "started_at": "2026-04-14T10:00:00+00:00",
        "ended_at": "2026-04-14T10:30:00+00:00",
        "duration_seconds": 1800.0,
        "event_count": 120,
        "parse_confidence": 0.98,
        "metrics": {
            "read_edit_ratio": 5.2,
            "stop_phrase": 0.0,
            "reasoning_loop": 8.3
        },
        "eviction_churn": 0,
        "cohort_size": 3
    }

Field notes:

- ``model`` and ``permission_mode`` are captured from session metadata when
  available. They may be ``null`` if the session JSONL does not carry them.
  The Phase 3 group-by on these dimensions silently omits null-valued records
  from those cohort cells; it does not impute values.
- ``eviction_churn`` and ``cohort_size`` are snapshot-point counters from the
  aggregator at finalisation time. They are for fleet-level observability and
  are not used by the cohort reducer.
- ``duration_seconds`` is ``(ended_at - started_at).total_seconds()``. It
  may be 0.0 for single-event sessions.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from codevigil.turns import Turn
from codevigil.watch_roots import LEGACY_ROOT_ID

_LOG = logging.getLogger(__name__)

# Current schema version. Increment when adding or removing fields from the
# session report. Each version bump requires a migration entry in
# _migrate_record() below.
CURRENT_SCHEMA_VERSION: int = 2

# Minimum schema version this code can read. Records older than this require
# a migration that is not yet implemented and will raise MigrationError.
_MINIMUM_SUPPORTED_VERSION: int = 1


class StoreError(Exception):
    """Raised on unrecoverable store I/O failures."""


class MigrationError(StoreError):
    """Raised when a stored record cannot be migrated to the current schema."""


class AmbiguousSessionError(StoreError):
    """Raised when a plain session_id matches multiple persisted reports."""


# ---------------------------------------------------------------------------
# Public record type
# ---------------------------------------------------------------------------


class SessionReport:
    """Validated, schema-version-stamped session report.

    Construct via :meth:`SessionReport.from_dict` (deserialisation) or
    :func:`build_report` (aggregator path). Direct construction is intentional
    only in tests.

    All timestamps are stored as ISO 8601 strings internally and exposed as
    :class:`datetime` objects through properties. This keeps the JSON
    serialisation format stable while providing typed access in Python.
    """

    __slots__ = (
        "_data",
        "_ended_at",
        "_started_at",
    )

    def __init__(self, data: dict[str, Any]) -> None:
        self._data: dict[str, Any] = data
        self._started_at: datetime = _parse_dt(data["started_at"])
        self._ended_at: datetime = _parse_dt(data["ended_at"])

    # ------------------------------------------------------------------ class methods

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionReport:
        """Deserialise and validate a raw dict from JSON storage.

        Applies schema migrations when the stored version is older than
        :data:`CURRENT_SCHEMA_VERSION`. Raises :exc:`MigrationError` when the
        stored version is newer than this code supports (forward-incompatible).
        """
        version = raw.get("schema_version")
        if not isinstance(version, int):
            raise MigrationError(
                f"session report missing schema_version field: {raw.get('session_id', '<unknown>')}"
            )
        if version > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                f"session report schema_version {version} is newer than supported "
                f"{CURRENT_SCHEMA_VERSION}; upgrade codevigil to read this record"
            )
        if version < _MINIMUM_SUPPORTED_VERSION:
            raise MigrationError(
                f"session report schema_version {version} is below the minimum "
                f"supported version {_MINIMUM_SUPPORTED_VERSION}"
            )
        migrated = _migrate_record(raw, from_version=version)
        _validate_record(migrated)
        return cls(migrated)

    # ------------------------------------------------------------------ properties

    @property
    def schema_version(self) -> int:
        return int(self._data["schema_version"])

    @property
    def session_id(self) -> str:
        return str(self._data["session_id"])

    @property
    def session_key(self) -> str:
        return str(self._data["session_key"])

    @property
    def root_id(self) -> str:
        return str(self._data["root_id"])

    @property
    def root_label(self) -> str | None:
        v = self._data.get("root_label")
        return str(v) if v is not None else None

    @property
    def project_hash(self) -> str:
        return str(self._data["project_hash"])

    @property
    def project_name(self) -> str | None:
        v = self._data.get("project_name")
        return str(v) if v is not None else None

    @property
    def model(self) -> str | None:
        v = self._data.get("model")
        return str(v) if v is not None else None

    @property
    def permission_mode(self) -> str | None:
        v = self._data.get("permission_mode")
        return str(v) if v is not None else None

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def ended_at(self) -> datetime:
        return self._ended_at

    @property
    def duration_seconds(self) -> float:
        return float(self._data["duration_seconds"])

    @property
    def event_count(self) -> int:
        return int(self._data["event_count"])

    @property
    def parse_confidence(self) -> float:
        return float(self._data["parse_confidence"])

    @property
    def metrics(self) -> dict[str, float]:
        raw: Any = self._data.get("metrics", {})
        return {str(k): float(v) for k, v in raw.items()}

    @property
    def eviction_churn(self) -> int:
        return int(self._data.get("eviction_churn", 0))

    @property
    def cohort_size(self) -> int:
        return int(self._data.get("cohort_size", 0))

    @property
    def turns(self) -> tuple[Turn, ...] | None:
        """Completed turns for this session, or ``None`` when not recorded.

        ``None`` is returned for records written before Phase 4 (no ``turns``
        key present) or when the session had no turns to record. Callers must
        treat ``None`` and an empty tuple as equivalent "no turn data" states.
        """
        raw = self._data.get("turns")
        if raw is None:
            return None
        return tuple(_deserialise_turn(t) for t in raw)

    @property
    def session_task_type(self) -> str | None:
        """Session-level aggregate task type, or ``None`` when not classified.

        ``None`` for records written before Phase 5, or when
        ``classifier.enabled = false``. One of the category names in
        ``TASK_CATEGORIES`` when present.
        """
        v = self._data.get("session_task_type")
        return str(v) if v is not None else None

    @property
    def turn_task_types(self) -> tuple[str, ...] | None:
        """Per-turn task type labels in turn order, or ``None`` when absent.

        ``None`` for records written before Phase 5, or when
        ``classifier.enabled = false``. Each element is one of the category
        names in ``TASK_CATEGORIES``.
        """
        raw = self._data.get("turn_task_types")
        if raw is None:
            return None
        return tuple(str(t) for t in raw)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable copy of the underlying data."""
        return dict(self._data)


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_report(
    *,
    session_id: str,
    session_key: str | None = None,
    root_id: str | None = None,
    root_label: str | None = None,
    project_hash: str,
    project_name: str | None,
    model: str | None,
    permission_mode: str | None,
    started_at: datetime,
    ended_at: datetime,
    event_count: int,
    parse_confidence: float,
    metrics: dict[str, float],
    eviction_churn: int = 0,
    cohort_size: int = 0,
    turns: tuple[Turn, ...] | None = None,
    session_task_type: str | None = None,
    turn_task_types: tuple[str, ...] | None = None,
) -> SessionReport:
    """Construct a :class:`SessionReport` from aggregator-supplied values.

    This is the intended construction path for the aggregator's ingest path.
    Tests may also call it directly with synthetic data.

    The ``turns``, ``session_task_type``, and ``turn_task_types`` parameters
    are all optional (default ``None``). Pre-upgrade records that lack these
    keys read back with the corresponding ``SessionReport`` properties as
    ``None`` — no migration is required.
    """
    duration = (ended_at - started_at).total_seconds()
    resolved_session_key = session_key or session_id
    resolved_root_id = root_id or LEGACY_ROOT_ID
    data: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "session_id": session_id,
        "session_key": resolved_session_key,
        "root_id": resolved_root_id,
        "root_label": root_label,
        "project_hash": project_hash,
        "project_name": project_name,
        "model": model,
        "permission_mode": permission_mode,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": duration,
        "event_count": event_count,
        "parse_confidence": parse_confidence,
        "metrics": {k: float(v) for k, v in metrics.items()},
        "eviction_churn": eviction_churn,
        "cohort_size": cohort_size,
    }
    if turns is not None:
        data["turns"] = [_serialise_turn(t) for t in turns]
    if session_task_type is not None:
        data["session_task_type"] = session_task_type
    if turn_task_types is not None:
        data["turn_task_types"] = list(turn_task_types)
    _validate_record(data)
    return SessionReport(data)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SessionStore:
    """Append-only on-disk store for session reports.

    The store is a directory of JSON files, one per session. It does not hold
    any in-memory index; reads enumerate the directory on demand so there is
    no state to corrupt on process crash.

    Persistence is opt-in. The calling code (aggregator) checks the
    ``[storage] enable_persistence`` flag before constructing the store or
    calling :meth:`write`. The store itself is transport-agnostic — it writes
    when asked, logs a one-time activation notice on the first write, and
    never creates directories unless explicitly requested.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir: Path = base_dir if base_dir is not None else _default_sessions_dir()
        self._activation_logged: bool = False

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ------------------------------------------------------------------ write

    def write(self, report: SessionReport) -> Path:
        """Persist *report* to ``<base_dir>/<session_key>.json``.

        Creates the directory on first write. Uses an atomic ``tmp → rename``
        so a partial write never produces a corrupt file. Logs a one-time
        activation notice naming the target directory.

        Returns the path of the written file.
        """
        self._ensure_dir()
        dest = self._base_dir / f"{report.session_key}.json"
        payload = json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n"

        # Atomic write: write to a sibling temp file, then rename.
        fd, tmp_path_str = tempfile.mkstemp(
            dir=self._base_dir,
            prefix=f".{report.session_key}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            tmp_path.rename(dest)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise
        return dest

    # ------------------------------------------------------------------ read

    def list_reports(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[SessionReport]:
        """Load all session reports from the store directory.

        Applies optional ``since`` / ``until`` filters against
        ``SessionReport.started_at``. Missing, unreadable, or unmigrateable
        files are skipped with a logged WARNING — they never abort the load.

        Returns reports sorted by ``started_at`` ascending.
        """
        if not self._base_dir.exists():
            return []
        reports: list[SessionReport] = []
        for path in self._base_dir.iterdir():
            if path.suffix != ".json" or path.stem.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                report = SessionReport.from_dict(raw)
            except (OSError, json.JSONDecodeError, MigrationError, StoreError) as exc:
                _LOG.warning("skipping unreadable session report %s: %s", path, exc)
                continue
            if since is not None and report.started_at < since:
                continue
            if until is not None and report.started_at > until:
                continue
            reports.append(report)
        reports.sort(key=lambda r: r.started_at)
        return reports

    def get_report(self, identifier: str) -> SessionReport | None:
        """Load a single report by session_key or legacy session_id."""
        path = self._base_dir / f"{identifier}.json"
        if path.exists():
            try:
                return self._load_report_file(path)
            except (OSError, json.JSONDecodeError, MigrationError, StoreError) as exc:
                _LOG.warning("failed to load session report %s: %s", identifier, exc)
                return None
        if not self._base_dir.exists():
            return None
        matched: SessionReport | None = None
        for report in self._iter_report_files():
            if report.session_id == identifier or report.session_key == identifier:
                if report.session_key == identifier:
                    return report
                if matched is not None and matched.session_key != report.session_key:
                    raise AmbiguousSessionError(
                        f"session id {identifier!r} matched multiple reports; "
                        "use the full session_key instead"
                    )
                matched = report
        return matched

    def _load_report_file(self, path: Path) -> SessionReport:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SessionReport.from_dict(raw)

    def _iter_report_files(self) -> Iterator[SessionReport]:
        for candidate in self._base_dir.iterdir():
            if candidate.suffix != ".json" or candidate.stem.startswith("."):
                continue
            try:
                yield self._load_report_file(candidate)
            except (OSError, json.JSONDecodeError, MigrationError, StoreError):
                continue

    # ------------------------------------------------------------------ internals

    def _ensure_dir(self) -> None:
        if not self._base_dir.exists():
            self._base_dir.mkdir(parents=True, exist_ok=True)
        if not self._activation_logged:
            _LOG.info(
                "persistence enabled, writing to %s",
                self._base_dir,
            )
            self._activation_logged = True


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _migrate_record(record: dict[str, Any], *, from_version: int) -> dict[str, Any]:
    """Apply forward-only migrations from *from_version* to :data:`CURRENT_SCHEMA_VERSION`.

    Migration policy (from ``docs/design.md``):
    - Migrations are one-way and forward-compatible. A record from schema
      version N can always be read by code at version N or later.
    - Adding a nullable field: set the new field to ``None`` for old records.
    - Removing a field: silently drop it; do not error on unknown keys.
    - Renaming a field: add the new name from the old value, drop the old name.
    - Changing a field type: coerce the old value to the new type.

    When schema_version is already at CURRENT_SCHEMA_VERSION, this is a
    cheap identity pass.
    """
    result = dict(record)
    version = from_version
    # Future migrations slot in here as ``if version < N:`` blocks:
    if version < 2:
        result["session_key"] = str(result.get("session_id", ""))
        result["root_id"] = LEGACY_ROOT_ID
        result["root_label"] = None
        version = 2
    result["schema_version"] = CURRENT_SCHEMA_VERSION
    _ = version  # suppress "variable assigned but never used" when no migrations exist
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "session_id",
    "session_key",
    "root_id",
    "project_hash",
    "started_at",
    "ended_at",
    "duration_seconds",
    "event_count",
    "parse_confidence",
    "metrics",
)


def _validate_record(record: dict[str, Any]) -> None:
    for field in _REQUIRED_FIELDS:
        if field not in record:
            raise StoreError(f"session report is missing required field {field!r}")
    if not isinstance(record.get("metrics"), dict):
        raise StoreError("session report field 'metrics' must be a dict")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_turn(turn: Turn) -> dict[str, Any]:
    """Convert a :class:`~codevigil.turns.Turn` to a JSON-serialisable dict."""
    return {
        "session_id": turn.session_id,
        "started_at": turn.started_at.isoformat(),
        "ended_at": turn.ended_at.isoformat(),
        "user_message_text": turn.user_message_text,
        "tool_calls": list(turn.tool_calls),
        "event_count": turn.event_count,
        "task_type": turn.task_type,
    }


def _deserialise_turn(raw: Any) -> Turn:
    """Reconstruct a :class:`~codevigil.turns.Turn` from a stored dict."""
    if not isinstance(raw, dict):
        raise StoreError(f"turn entry must be a dict, got {type(raw).__name__}")
    return Turn(
        session_id=str(raw["session_id"]),
        started_at=_parse_dt(raw["started_at"]),
        ended_at=_parse_dt(raw["ended_at"]),
        user_message_text=str(raw.get("user_message_text", "")),
        tool_calls=tuple(str(t) for t in raw.get("tool_calls", [])),
        event_count=int(raw.get("event_count", 0)),
        task_type=str(raw["task_type"]) if raw.get("task_type") is not None else None,
    )


def _default_sessions_dir() -> Path:
    """Resolve ``$XDG_STATE_HOME/codevigil/sessions/`` with fallback."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg and xdg.strip() else Path.home() / ".local" / "state"
    return base / "codevigil" / "sessions"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise StoreError(f"cannot parse timestamp: {value!r}")


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MigrationError",
    "SessionReport",
    "SessionStore",
    "StoreError",
    "build_report",
]
