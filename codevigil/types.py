"""Core vocabulary: Event, MetricSnapshot, SessionMeta, Collector/Renderer protocols.

Everything in this module is a *frozen contract*. Once Phase 1 lands on main
these names, shapes, and semantics may not change without an explicit
breaking-change PR, because every downstream subsystem (parser, watcher,
aggregator, collectors, renderers, CLI) imports from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record


class EventKind(Enum):
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASSISTANT_MESSAGE = "assistant"
    USER_MESSAGE = "user"
    THINKING = "thinking"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Event:
    """One typed record emitted by the parser.

    ``payload`` is deliberately unstructured at the type level — the
    per-``EventKind`` schemas live in ``docs/design.md`` §Payload Schemas by
    EventKind and are enforced by ``safe_get`` at read time, not by a
    dataclass tree. This keeps the kind space open for additive growth.

    ``message_id`` is the raw API message ID extracted from the JSONL line's
    ``message.id`` field. It is ``None`` for system events, synthesised lines,
    and older session files that predate the ID field. The parser uses it for
    deduplication; collectors and renderers may read it but must never filter
    on it unconditionally (``None`` events are always valid).
    """

    timestamp: datetime
    session_id: str
    kind: EventKind
    payload: dict[str, Any]
    message_id: str | None = None


class Severity(Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """Single metric reading produced by a collector on each ``snapshot()``.

    ``value`` is always a float so every metric has exactly one scalar the
    renderer can threshold, trend, and compare. Structured breakdowns go in
    ``detail`` so the scalar contract stays simple.
    """

    name: str
    value: float
    label: str
    severity: Severity = Severity.OK
    detail: dict[str, Any] | None = None


class SessionState(Enum):
    ACTIVE = "active"
    STALE = "stale"
    EVICTED = "evicted"


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """Aggregator-owned view of a session handed to renderers on every tick.

    ``snapshot_history`` carries the last-three per-metric scalar values for
    the mini-trend display in the watch-mode renderer. Keys are collector
    names; values are tuples of up to three floats in chronological order
    (oldest first). The field defaults to an empty dict so existing callers
    and tests that construct ``SessionMeta`` without it continue to work.

    ``session_task_type`` is the classifier-derived session-level task label
    (one of ``TASK_CATEGORIES`` from ``codevigil.classifier``), or ``None``
    when the classifier is disabled or no turns have been classified yet.
    This field defaults to ``None`` so existing callers that construct
    ``SessionMeta`` directly continue to work unchanged.
    """

    session_id: str
    project_hash: str
    project_name: str | None
    file_path: Path
    start_time: datetime
    last_event_time: datetime
    event_count: int
    parse_confidence: float
    state: SessionState
    snapshot_history: dict[str, tuple[float, ...]] = field(default_factory=dict)
    session_task_type: str | None = None


@runtime_checkable
class Collector(Protocol):
    """Contract every metric collector must honor.

    The ``complexity`` attribute is a human-readable big-O string documented
    per §Complexity Honesty in the design. Snapshots are pure functions of
    collector state and are idempotent.
    """

    name: str
    complexity: str

    def ingest(self, event: Event) -> None: ...

    def snapshot(self) -> MetricSnapshot: ...

    def reset(self) -> None: ...


@runtime_checkable
class Renderer(Protocol):
    """Contract every renderer must honor."""

    name: str

    def render(self, snapshots: list[MetricSnapshot], meta: SessionMeta) -> None: ...

    def render_error(self, err: CodevigilError, meta: SessionMeta | None) -> None: ...

    def close(self) -> None: ...


_MISSING = object()


def safe_get(
    payload: dict[str, Any],
    key: str,
    default: Any,
    expected: type | None = None,
    *,
    required: bool = False,
    source: ErrorSource = ErrorSource.PARSER,
    event_kind: str | None = None,
) -> Any:
    """Typed payload lookup that routes drift through the error channel.

    Returns ``payload[key]`` when present and (optionally) type-matching the
    ``expected`` type. Emits a WARN ``CodevigilError`` to the process-wide
    error channel on missing-but-required or type-mismatch cases, so every
    silent ``KeyError`` becomes a counted, observable drift signal the
    parser's ``parse_confidence`` meter can pick up.
    """

    value: Any = payload.get(key, _MISSING)
    if value is _MISSING:
        if required:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=source,
                    code="safe_get.missing_required",
                    message=f"required key {key!r} missing from payload",
                    context={"key": key, "event_kind": event_kind},
                )
            )
        return default
    if expected is not None and not isinstance(value, expected):
        record(
            CodevigilError(
                level=ErrorLevel.WARN,
                source=source,
                code="safe_get.type_mismatch",
                message=(
                    f"key {key!r} has type {type(value).__name__}, expected {expected.__name__}"
                ),
                context={
                    "key": key,
                    "expected": expected.__name__,
                    "actual": type(value).__name__,
                    "event_kind": event_kind,
                },
            )
        )
        return default
    return value


# Re-export field so downstream phases that need default_factory can pull it
# from types without introducing a second dataclasses import.
__all__ = [
    "Collector",
    "Event",
    "EventKind",
    "MetricSnapshot",
    "Renderer",
    "SessionMeta",
    "SessionState",
    "Severity",
    "field",
    "safe_get",
]
