"""Error taxonomy and JSONL log writer.

Implements the single ``CodevigilError`` hierarchy, the four-level
``ErrorLevel`` enum, the five-source ``ErrorSource`` enum, and a hand-rolled
rotating JSONL writer (10 MiB per file, three archived generations).

The writer is hand-rolled rather than using ``logging.handlers.RotatingFileHandler``
because ``logging.handlers`` statically imports ``socket``, which the privacy
gate rejects the moment a codevigil module pulls it in. Keeping the writer
in-house means we have zero transitive imports of banned modules and the log
subsystem works under the privacy hook with no exemptions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_MAX_LOG_BYTES: int = 10 * 1024 * 1024  # 10 MiB
_MAX_LOG_BACKUPS: int = 3
_DEFAULT_LOG_PATH_ENV: str = "CODEVIGIL_LOG_PATH"
_DEFAULT_LOG_PATH: Path = Path.home() / ".local" / "state" / "codevigil" / "codevigil.log"


class ErrorLevel(Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorSource(Enum):
    PARSER = "parser"
    WATCHER = "watcher"
    COLLECTOR = "collector"
    RENDERER = "renderer"
    CONFIG = "config"
    AGGREGATOR = "aggregator"


@dataclass(frozen=True, slots=True)
class CodevigilError(Exception):
    """Base class for every subsystem-originated error.

    Inherits from ``Exception`` so collectors and the parser can ``raise`` a
    ``CodevigilError`` and have the aggregator catch and route it through the
    single error channel (see ``docs/design.md`` §Error Non-Swallowing Rule).

    Instances are frozen dataclasses so they can be safely re-raised, logged,
    and passed between subsystems without mutation.
    """

    level: ErrorLevel
    source: ErrorSource
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_json_record(self, *, timestamp: datetime | None = None) -> dict[str, Any]:
        """Serialise to a dict suitable for JSONL emission."""

        ts = timestamp if timestamp is not None else datetime.now(tz=UTC)
        return {
            "timestamp": ts.isoformat(),
            "level": self.level.value,
            "source": self.source.value,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - diagnostic only
        return f"[{self.level.value}/{self.source.value}] {self.code}: {self.message}"


def _resolve_log_path() -> Path:
    override = os.environ.get(_DEFAULT_LOG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return _DEFAULT_LOG_PATH


class RotatingJsonlWriter:
    """Single-writer rotating JSONL log.

    Rotation strategy: when a write would push the active file past
    ``_MAX_LOG_BYTES``, archive ``path`` → ``path.1``, ``path.1`` → ``path.2``,
    ..., up to ``_MAX_LOG_BACKUPS`` archive files. Older archives are dropped.

    The writer is single-threaded by construction — the aggregator owns the
    error channel and calls ``record()`` from its tick loop. Concurrent use
    is not a v0.1 concern.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = _MAX_LOG_BYTES,
        backups: int = _MAX_LOG_BACKUPS,
    ) -> None:
        self._path: Path = path
        self._max_bytes: int = max_bytes
        self._backups: int = backups
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _current_size(self) -> int:
        try:
            return self._path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rotate(self) -> None:
        # Drop the oldest archive if it exists.
        oldest = self._path.with_suffix(self._path.suffix + f".{self._backups}")
        if oldest.exists():
            oldest.unlink()
        # Shift archives N-1 → N, N-2 → N-1, ..., 1 → 2.
        for index in range(self._backups - 1, 0, -1):
            src = self._path.with_suffix(self._path.suffix + f".{index}")
            if src.exists():
                dst = self._path.with_suffix(self._path.suffix + f".{index + 1}")
                src.rename(dst)
        # Move active file to .1.
        if self._path.exists():
            first_archive = self._path.with_suffix(self._path.suffix + ".1")
            self._path.rename(first_archive)

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        size = self._current_size()
        if size > 0 and size + len(encoded) > self._max_bytes:
            self._rotate()
        with self._path.open("ab") as handle:
            handle.write(encoded)


class ErrorChannel:
    """Process-wide error sink.

    The aggregator owns the instance at runtime; during tests and inside
    ``types.safe_get`` we use the module-level singleton returned by
    ``get_error_channel()``. Writes are fire-and-forget — if the log path
    cannot be written, the error surfaces via the raised ``OSError``: there
    is no outer swallowing, which matches §Error Non-Swallowing Rule.
    """

    def __init__(self, writer: RotatingJsonlWriter) -> None:
        self._writer: RotatingJsonlWriter = writer

    @property
    def writer(self) -> RotatingJsonlWriter:
        return self._writer

    def record(self, error: CodevigilError) -> None:
        self._writer.write(error.to_json_record())


_CHANNEL_SINGLETON: ErrorChannel | None = None


def get_error_channel() -> ErrorChannel:
    """Return (and lazily create) the process-wide error channel."""

    global _CHANNEL_SINGLETON
    if _CHANNEL_SINGLETON is None:
        _CHANNEL_SINGLETON = ErrorChannel(RotatingJsonlWriter(_resolve_log_path()))
    return _CHANNEL_SINGLETON


def set_error_channel(channel: ErrorChannel) -> None:
    """Install an explicit error channel (used by tests and the aggregator)."""

    global _CHANNEL_SINGLETON
    _CHANNEL_SINGLETON = channel


def reset_error_channel() -> None:
    """Drop the singleton so the next ``get_error_channel()`` call re-creates it."""

    global _CHANNEL_SINGLETON
    _CHANNEL_SINGLETON = None


def record(error: CodevigilError) -> None:
    """Convenience shim for modules that only need to emit one error."""

    get_error_channel().record(error)


__all__ = [
    "CodevigilError",
    "ErrorChannel",
    "ErrorLevel",
    "ErrorSource",
    "RotatingJsonlWriter",
    "get_error_channel",
    "record",
    "reset_error_channel",
    "set_error_channel",
]
