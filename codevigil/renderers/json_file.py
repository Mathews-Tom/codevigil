"""NDJSON file renderer backed by the rotating JSONL writer.

Emits one JSON record per ``render()`` call into a rotating file under an
output directory. The directory must resolve inside ``$HOME``; attempts to
write outside are rejected at construction with ``PrivacyViolationError``,
matching the scope check the watcher enforces on its walk root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codevigil.errors import (
    CodevigilError,
    ErrorLevel,
    ErrorSource,
    RotatingJsonlWriter,
    record,
)
from codevigil.privacy import PrivacyViolationError
from codevigil.types import MetricSnapshot, SessionMeta


class JsonFileRenderer:
    """Appends NDJSON snapshot records to a rotating file under ``output_dir``."""

    name: str = "json_file"

    def __init__(
        self,
        *,
        output_dir: Path,
        filename: str = "snapshots.jsonl",
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        resolved_dir = self._validate_dir(output_dir)
        self._output_dir: Path = resolved_dir
        self._filename: str = filename
        self._path: Path = resolved_dir / filename
        self._writer: RotatingJsonlWriter = RotatingJsonlWriter(
            self._path, max_bytes=max_bytes, backups=backups
        )

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _validate_dir(output_dir: Path) -> Path:
        resolved_dir = output_dir.expanduser().resolve()
        home = Path.home().resolve()
        if not resolved_dir.is_relative_to(home):
            err = CodevigilError(
                level=ErrorLevel.CRITICAL,
                source=ErrorSource.RENDERER,
                code="json_file.path_scope_violation",
                message=(
                    f"json_file output directory {str(resolved_dir)!r} is outside "
                    f"the user home directory {str(home)!r}; refusing to write"
                ),
                context={"output_dir": str(resolved_dir), "home": str(home)},
            )
            record(err)
            raise PrivacyViolationError(err.message)
        return resolved_dir

    def render(self, snapshots: list[MetricSnapshot], meta: SessionMeta) -> None:
        record_payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "kind": "snapshot",
            "session_id": meta.session_id,
            "project_hash": meta.project_hash,
            "project_name": meta.project_name,
            "state": meta.state.value,
            "parse_confidence": meta.parse_confidence,
            "snapshots": [_snapshot_to_dict(s) for s in snapshots],
        }
        self._writer.write(record_payload)

    def render_error(self, err: CodevigilError, meta: SessionMeta | None) -> None:
        payload: dict[str, Any] = err.to_json_record()
        payload["kind"] = "error"
        if meta is not None:
            payload["session_id"] = meta.session_id
            payload["project_hash"] = meta.project_hash
        self._writer.write(payload)

    def close(self) -> None:
        """No-op. ``RotatingJsonlWriter`` opens and closes per write."""


def _snapshot_to_dict(snap: MetricSnapshot) -> dict[str, Any]:
    return {
        "name": snap.name,
        "value": snap.value,
        "label": snap.label,
        "severity": snap.severity.value,
        "detail": snap.detail,
    }


__all__ = ["JsonFileRenderer"]
