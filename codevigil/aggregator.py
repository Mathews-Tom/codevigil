"""Session orchestration: source → parser → collectors → snapshots.

The :class:`SessionAggregator` is the only subsystem that touches every
other subsystem. It owns one :class:`SessionParser` per session, instantiates
the active collectors from the registry per session, drives lifecycle
transitions (ACTIVE → STALE → EVICTED) on a monotonic clock, and is the
single owner of the error channel: every :class:`CodevigilError` raised by a
collector is caught here, recorded with ``source=COLLECTOR`` plus the
offending collector name, and the loop moves on. Peer collectors and other
sessions are unaffected — see ``docs/design.md`` §Error Non-Swallowing Rule.

Wiring the parse_health collector
---------------------------------

The :class:`~codevigil.types.Collector` protocol is frozen and exposes no
"give me the parser stats" hook. The aggregator works around this with a
duck-typed bind: at session creation time it constructs the parser, then for
each collector instance whose class declares a ``bind_stats`` method it calls
``collector.bind_stats(parser.stats)``. Today only
:class:`~codevigil.collectors.parse_health.ParseHealthCollector` declares
that method; future drift-aware collectors that need the same handle just
need to grow the same one-method protocol. This keeps the registry
collector-shape clean while still letting the always-on integrity collector
read live parser counters without going through the error channel.

The "always on, never disableable" rule for ``parse_health`` is enforced
here in addition to ``codevigil.config``: even if a future code path managed
to drop ``parse_health`` from ``collectors.enabled``, the aggregator still
instantiates it for every session.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codevigil.bootstrap import BootstrapManager
from codevigil.collectors import COLLECTORS
from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.parser import SessionParser
from codevigil.projects import ProjectRegistry
from codevigil.types import (
    Collector,
    Event,
    EventKind,
    MetricSnapshot,
    SessionMeta,
    SessionState,
    Severity,
)
from codevigil.watcher import Source, SourceEvent, SourceEventKind

_PARSE_HEALTH_NAME: str = "parse_health"


@dataclass(slots=True)
class _SessionContext:
    """Per-session bookkeeping owned by :class:`SessionAggregator`.

    ``last_monotonic`` is updated from the aggregator's clock callable on
    every observed APPEND so lifecycle transitions are deterministic in
    tests. ``last_event_time`` is the wall-clock timestamp from the last
    emitted :class:`Event` and is what the renderer surfaces in
    :class:`SessionMeta`.
    """

    session_id: str
    file_path: Path
    project_hash: str
    parser: SessionParser
    collectors: dict[str, Collector]
    first_event_time: datetime
    last_event_time: datetime
    last_monotonic: float
    event_count: int = 0
    state: SessionState = SessionState.ACTIVE
    last_snapshots: dict[str, MetricSnapshot] = field(default_factory=dict)


_ClockFn = Callable[[], float]


class SessionAggregator:
    """Drive a :class:`Source` through parser, collectors, and lifecycle.

    The aggregator does not block, does not schedule its own ticks, and does
    not own any threads. The CLI watch loop calls :meth:`tick` on whatever
    cadence ``watch.tick_interval`` dictates and consumes the yielded
    ``(meta, snapshots)`` pairs. Tests drive ``tick()`` directly with a
    scripted fake source and a controllable clock — see
    ``tests/_aggregator_helpers.py``.
    """

    def __init__(
        self,
        source: Source,
        *,
        config: dict[str, Any],
        project_registry: ProjectRegistry | None = None,
        clock: _ClockFn = time.monotonic,
        registry: dict[str, type[Collector]] | None = None,
        bootstrap: BootstrapManager | None = None,
    ) -> None:
        self._source: Source = source
        self._config: dict[str, Any] = config
        self._registry: dict[str, type[Collector]] = (
            registry if registry is not None else COLLECTORS
        )
        self._project_registry: ProjectRegistry = (
            project_registry if project_registry is not None else ProjectRegistry()
        )
        self._clock: _ClockFn = clock
        self._sessions: dict[str, _SessionContext] = {}
        self._bootstrap: BootstrapManager | None = bootstrap

        watch_cfg = config.get("watch", {})
        self._stale_after: float = float(watch_cfg.get("stale_after_seconds", 300))
        self._evict_after: float = float(watch_cfg.get("evict_after_seconds", 2100))
        collectors_cfg = config.get("collectors", {})
        enabled = collectors_cfg.get("enabled", [])
        self._enabled_collectors: tuple[str, ...] = tuple(enabled)

    # --------------------------------------------------------------- properties

    @property
    def sessions(self) -> dict[str, _SessionContext]:
        """Read-only-ish accessor used by tests; do not mutate externally."""

        return self._sessions

    # ----------------------------------------------------------------- tick API

    def tick(self) -> Iterator[tuple[SessionMeta, list[MetricSnapshot]]]:
        """Consume one batch of source events and yield current snapshots.

        Order of operations: drain ``source.poll()`` first, then run the
        lifecycle pass over every known session. Doing the source pass first
        means an APPEND received in the same tick that would have crossed
        the STALE threshold counts as activity and keeps the session ACTIVE.
        """

        for source_event in self._source.poll():
            self._dispatch_source_event(source_event)
        self._run_lifecycle_pass()

        results: list[tuple[SessionMeta, list[MetricSnapshot]]] = []
        for ctx in self._sessions.values():
            if ctx.state is SessionState.EVICTED:
                continue
            snapshots = self._snapshot_session(ctx)
            results.append((self._build_meta(ctx), snapshots))
        return iter(results)

    def close(self) -> None:
        """Tear down the source and every live collector."""

        try:
            self._source.close()
        except CodevigilError as err:
            self._record_collector_error(err, collector_name="source", session_id="*")
        for ctx in list(self._sessions.values()):
            # Take a final snapshot pass so sessions that never reached
            # EVICTED still contribute to the bootstrap distribution.
            if self._bootstrap is not None and self._bootstrap.is_active():
                self._snapshot_session(ctx)
            self._observe_for_bootstrap(ctx)
            self._reset_collectors(ctx)
        self._sessions.clear()

    # ---------------------------------------------------------- source dispatch

    def _dispatch_source_event(self, source_event: SourceEvent) -> None:
        kind = source_event.kind
        if kind is SourceEventKind.NEW_SESSION:
            self._ensure_session(source_event)
            return
        if kind is SourceEventKind.APPEND:
            ctx = self._ensure_session(source_event)
            if source_event.line is None:
                return
            self._ingest_line(ctx, source_event.line)
            return
        if kind is SourceEventKind.ROTATE or kind is SourceEventKind.TRUNCATE:
            # The watcher resets its file cursor and re-reads from byte 0,
            # so a fresh stream of APPEND events will follow. The *session*
            # is the same logical session, so we deliberately preserve the
            # collector state and the parser instance — clearing them would
            # erase the very degradation history the metrics exist to
            # surface. The watcher is the source of truth for line replay;
            # the aggregator just keeps consuming.
            return
        if kind is SourceEventKind.DELETE:
            self._evict_session(source_event.session_id)
            return

    def _ensure_session(self, source_event: SourceEvent) -> _SessionContext:
        sid = source_event.session_id
        existing = self._sessions.get(sid)
        if existing is not None:
            return existing
        parser = SessionParser(session_id=sid)
        collectors = self._instantiate_collectors(parser)
        now_clock = self._clock()
        now_wall = source_event.timestamp
        ctx = _SessionContext(
            session_id=sid,
            file_path=source_event.path,
            project_hash=self._extract_project_hash(source_event.path),
            parser=parser,
            collectors=collectors,
            first_event_time=now_wall,
            last_event_time=now_wall,
            last_monotonic=now_clock,
        )
        self._sessions[sid] = ctx
        return ctx

    @staticmethod
    def _extract_project_hash(path: Path) -> str:
        """Pull the project-hash directory from the canonical path layout.

        Layout is ``~/.claude/projects/<project-hash>/sessions/<id>.jsonl``;
        we walk parents until we find one named ``projects`` and return the
        directory immediately under it. Anything else falls back to the
        empty string, which the registry resolves to ``""[:8] == ""``.
        """

        parts = path.parts
        for index, part in enumerate(parts):
            if part == "projects" and index + 1 < len(parts):
                return parts[index + 1]
        return ""

    def _instantiate_collectors(self, parser: SessionParser) -> dict[str, Collector]:
        """Build the per-session collector dict from the registry.

        ``parse_health`` is always instantiated, regardless of whether it
        appears in the enabled list — it is the only un-disableable
        collector and the validator already refuses configs that try to
        turn it off, but enforcing it here too means a buggy code path that
        bypasses the validator still cannot drop the integrity collector.
        """

        instances: dict[str, Collector] = {}
        names: list[str] = []
        if _PARSE_HEALTH_NAME in self._registry:
            names.append(_PARSE_HEALTH_NAME)
        for name in self._enabled_collectors:
            if name == _PARSE_HEALTH_NAME:
                continue
            if name in self._registry:
                names.append(name)
        for name in names:
            cls = self._registry[name]
            instance = cls()
            bind = getattr(instance, "bind_stats", None)
            if callable(bind):
                bind(parser.stats)
            instances[name] = instance
        return instances

    # -------------------------------------------------------------- ingest path

    def _ingest_line(self, ctx: _SessionContext, line: str) -> None:
        for event in ctx.parser.parse([line]):
            ctx.event_count += 1
            ctx.last_event_time = event.timestamp
            ctx.last_monotonic = self._clock()
            if ctx.state is SessionState.STALE:
                # STALE → ACTIVE on a new APPEND (the "coffee break" rule).
                # Collector state is intentionally preserved.
                ctx.state = SessionState.ACTIVE
            if event.kind is EventKind.SYSTEM:
                self._project_registry.observe_system_event(ctx.project_hash, event)
            self._fan_out_event(ctx, event)

    def _fan_out_event(self, ctx: _SessionContext, event: Event) -> None:
        for collector_name, collector in ctx.collectors.items():
            try:
                collector.ingest(event)
            except CodevigilError as err:
                # One collector raising must not poison its peers. We log
                # the failure with source=COLLECTOR and continue — both the
                # remaining collectors for this event and the next event
                # for the same collector keep flowing.
                self._record_collector_error(
                    err,
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )
            except Exception as exc:
                # A collector raising a non-CodevigilError is a bug, but
                # the design's non-swallowing rule still requires we route
                # it through the error channel rather than crash the loop.
                self._record_collector_error(
                    CodevigilError(
                        level=ErrorLevel.ERROR,
                        source=ErrorSource.COLLECTOR,
                        code="aggregator.collector_unexpected_exception",
                        message=(
                            f"collector {collector_name!r} raised {type(exc).__name__}: {exc}"
                        ),
                        context={
                            "collector": collector_name,
                            "session_id": ctx.session_id,
                            "exception_type": type(exc).__name__,
                        },
                    ),
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )

    def _record_collector_error(
        self,
        err: CodevigilError,
        *,
        collector_name: str,
        session_id: str,
    ) -> None:
        ctx_payload = dict(err.context)
        ctx_payload.setdefault("collector", collector_name)
        ctx_payload.setdefault("session_id", session_id)
        record(
            CodevigilError(
                level=err.level if err.level is not ErrorLevel.INFO else ErrorLevel.ERROR,
                source=ErrorSource.COLLECTOR,
                code=err.code or "aggregator.collector_error",
                message=err.message,
                context=ctx_payload,
            )
        )

    # ------------------------------------------------------------------ snapshot

    def _snapshot_session(self, ctx: _SessionContext) -> list[MetricSnapshot]:
        snapshots: list[MetricSnapshot] = []
        for collector_name, collector in ctx.collectors.items():
            try:
                raw = collector.snapshot()
                ctx.last_snapshots[collector_name] = raw
                snapshots.append(self._apply_bootstrap_clamp(collector_name, raw))
            except CodevigilError as err:
                self._record_collector_error(
                    err,
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )
            except Exception as exc:
                self._record_collector_error(
                    CodevigilError(
                        level=ErrorLevel.ERROR,
                        source=ErrorSource.COLLECTOR,
                        code="aggregator.snapshot_unexpected_exception",
                        message=(
                            f"collector {collector_name!r} snapshot raised "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        context={
                            "collector": collector_name,
                            "session_id": ctx.session_id,
                        },
                    ),
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )
        return snapshots

    def _apply_bootstrap_clamp(
        self,
        collector_name: str,
        snap: MetricSnapshot,
    ) -> MetricSnapshot:
        """Pin severity to OK and tag the label while bootstrap runs.

        ``parse_health`` is the only integrity signal and must keep its
        real severity; every other collector is still experimental until
        the bootstrap window closes, so we refuse to let them drive
        alerts during calibration. The raw snapshot the collector
        produced is preserved in ``ctx.last_snapshots`` for observation;
        only the user-visible copy is rewritten.
        """

        bootstrap = self._bootstrap
        if bootstrap is None or not bootstrap.is_active():
            return snap
        if collector_name == _PARSE_HEALTH_NAME:
            return snap
        tag = f"[bootstrap {bootstrap.sessions_observed() + 1}/{bootstrap.target}]"
        label = f"{snap.label} {tag}" if snap.label else tag
        return MetricSnapshot(
            name=snap.name,
            value=snap.value,
            label=label,
            severity=Severity.OK,
            detail=snap.detail,
        )

    def _observe_for_bootstrap(self, ctx: _SessionContext) -> None:
        """Hand the final per-collector snapshots to the bootstrap manager."""

        bootstrap = self._bootstrap
        if bootstrap is None or not bootstrap.is_active():
            return
        payload: dict[str, MetricSnapshot] = {}
        for collector_name, snap in ctx.last_snapshots.items():
            if collector_name == _PARSE_HEALTH_NAME:
                continue
            payload[collector_name] = snap
        if not payload:
            return
        bootstrap.observe_session(ctx.session_id, payload)
        if bootstrap.finalize_if_ready():
            record(
                CodevigilError(
                    level=ErrorLevel.INFO,
                    source=ErrorSource.AGGREGATOR,
                    code="aggregator.bootstrap_complete",
                    message=(
                        f"bootstrap window closed after {bootstrap.sessions_observed()} "
                        f"sessions; derived thresholds persisted to "
                        f"{bootstrap.state_path!s}"
                    ),
                    context={
                        "state_path": str(bootstrap.state_path),
                        "sessions_observed": bootstrap.sessions_observed(),
                    },
                )
            )

    def _build_meta(self, ctx: _SessionContext) -> SessionMeta:
        confidence = ctx.parser.stats.parse_confidence
        return SessionMeta(
            session_id=ctx.session_id,
            project_hash=ctx.project_hash,
            project_name=self._project_registry.resolve(ctx.project_hash),
            file_path=ctx.file_path,
            start_time=ctx.first_event_time,
            last_event_time=ctx.last_event_time,
            event_count=ctx.event_count,
            parse_confidence=float(confidence),
            state=ctx.state,
        )

    # ----------------------------------------------------------------- lifecycle

    def _run_lifecycle_pass(self) -> None:
        now = self._clock()
        to_evict: list[str] = []
        for sid, ctx in self._sessions.items():
            silence = now - ctx.last_monotonic
            if silence >= self._evict_after:
                to_evict.append(sid)
                continue
            if silence >= self._stale_after and ctx.state is SessionState.ACTIVE:
                ctx.state = SessionState.STALE
        for sid in to_evict:
            self._evict_session(sid)

    def _evict_session(self, session_id: str) -> None:
        ctx = self._sessions.pop(session_id, None)
        if ctx is None:
            return
        ctx.state = SessionState.EVICTED
        self._observe_for_bootstrap(ctx)
        self._reset_collectors(ctx)

    def _reset_collectors(self, ctx: _SessionContext) -> None:
        for collector_name, collector in ctx.collectors.items():
            try:
                collector.reset()
            except CodevigilError as err:
                self._record_collector_error(
                    err,
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )
            except Exception as exc:
                self._record_collector_error(
                    CodevigilError(
                        level=ErrorLevel.ERROR,
                        source=ErrorSource.COLLECTOR,
                        code="aggregator.reset_unexpected_exception",
                        message=(
                            f"collector {collector_name!r} reset raised {type(exc).__name__}: {exc}"
                        ),
                        context={
                            "collector": collector_name,
                            "session_id": ctx.session_id,
                        },
                    ),
                    collector_name=collector_name,
                    session_id=ctx.session_id,
                )


def _utc_now() -> datetime:  # pragma: no cover - convenience for callers
    return datetime.now(tz=UTC)


# Re-export ``field`` so downstream phases that grow ``_SessionContext`` can
# reach the dataclasses helper without a second import.
__all__ = ["SessionAggregator", "field"]
