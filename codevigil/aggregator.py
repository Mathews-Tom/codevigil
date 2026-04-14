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

import dataclasses
import hashlib
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codevigil.analysis.store import SessionStore, build_report
from codevigil.bootstrap import BootstrapManager
from codevigil.classifier import aggregate_session_task_type, classify_turn
from codevigil.collectors import COLLECTORS
from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.parser import SessionParser
from codevigil.projects import ProjectRegistry
from codevigil.turns import Turn, TurnGrouper
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

    ``first_event_time`` starts as the watcher's wall-clock timestamp for
    the NEW_SESSION event (a reasonable fallback) and is overwritten by the
    first successfully-parsed event's own ``timestamp`` field. This is the
    authoritative session start time: the parsed timestamp reflects when
    Claude Code actually started the session, whereas the watcher timestamp
    reflects when codevigil first noticed the file. The distinction matters
    for uptime: a session file that was already minutes or hours old when
    codevigil first saw it would otherwise show 0s uptime on the first tick.
    ``first_event_time_set`` tracks whether the overwrite has happened so
    we only take the first parsed event, not the last.
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
    first_event_time_set: bool = False
    # Bounded per-metric value history for mini-trend display. Keyed by
    # collector name; each deque holds the last 3 scalar values in
    # chronological order (oldest first). The deque is capped at 3 so
    # memory growth is O(collectors * 3) per session.
    snapshot_history: dict[str, deque[float]] = field(default_factory=dict)
    # Turn sidecar — populated by TurnGrouper as a sidecar to collector
    # ingestion. Collectors do NOT consume Turn; this is exposed to the
    # classifier (Phase 5) and history detail (later phases). The grouper
    # is initialised in __post_init__ so it receives the session_id that
    # is already set as a required field.
    turn_grouper: TurnGrouper = field(init=False)
    completed_turns: list[Turn] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Called automatically by the generated __init__ because slots=True
        # dataclasses still support __post_init__. Sets turn_grouper, which
        # cannot be provided via a default_factory because it depends on the
        # session_id required field.
        self.turn_grouper = TurnGrouper(self.session_id)


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

        storage_cfg = config.get("storage", {})
        self._persistence_enabled: bool = bool(storage_cfg.get("enable_persistence", False))
        self._store: SessionStore | None = SessionStore() if self._persistence_enabled else None

        watch_cfg = config.get("watch", {})
        self._stale_after: float = float(watch_cfg.get("stale_after_seconds", 300))
        self._evict_after: float = float(watch_cfg.get("evict_after_seconds", 2100))
        collectors_cfg = config.get("collectors", {})
        enabled = collectors_cfg.get("enabled", [])
        self._enabled_collectors: tuple[str, ...] = tuple(enabled)
        classifier_cfg = config.get("classifier", {})
        self._classifier_enabled: bool = bool(classifier_cfg.get("enabled", True))
        # Tracks paths for which the "invalid layout" WARN has already been
        # emitted so the message fires once per distinct path per process,
        # not once per tick or per session that shares the same bad path.
        self._warned_invalid_paths: set[str] = set()
        # Eviction churn: number of sessions evicted in the most recent tick.
        # Reset to 0 at the start of each tick. Phase 3's reducer reads this
        # to detect flapping watcher roots or short-lived session bursts.
        self._eviction_churn: int = 0
        # Cohort size: number of live (non-evicted) sessions at the end of
        # the most recent tick. Callers may read either property between ticks.
        self._cohort_size: int = 0

    # --------------------------------------------------------------- properties

    @property
    def sessions(self) -> dict[str, _SessionContext]:
        """Read-only-ish accessor used by tests; do not mutate externally."""

        return self._sessions

    @property
    def eviction_churn(self) -> int:
        """Sessions evicted during the most recent :meth:`tick` call.

        Reset to 0 at the start of each tick. Phase 3's reducer uses this
        to detect flapping watcher roots (sustained high churn) and
        short-lived session bursts. A value of ``0`` is normal for stable
        watch loops.
        """

        return self._eviction_churn

    @property
    def cohort_size(self) -> int:
        """Live (non-evicted) session count at the end of the most recent tick.

        Includes both ``ACTIVE`` and ``STALE`` sessions — anything that has
        not yet been evicted. Phase 3's reducer uses this as the denominator
        for fleet-level metric aggregation.
        """

        return self._cohort_size

    # ----------------------------------------------------------------- tick API

    def tick(self) -> Iterator[tuple[SessionMeta, list[MetricSnapshot]]]:
        """Consume one batch of source events and yield current snapshots.

        Order of operations: drain ``source.poll()`` first, then run the
        lifecycle pass over every known session. Doing the source pass first
        means an APPEND received in the same tick that would have crossed
        the STALE threshold counts as activity and keeps the session ACTIVE.

        After each tick the :attr:`eviction_churn` and :attr:`cohort_size`
        properties are updated so callers can observe fleet composition
        without introspecting the private ``_sessions`` dict.
        """

        self._eviction_churn = 0
        for source_event in self._source.poll():
            self._dispatch_source_event(source_event)
        self._run_lifecycle_pass()

        results: list[tuple[SessionMeta, list[MetricSnapshot]]] = []
        for ctx in self._sessions.values():
            if ctx.state is SessionState.EVICTED:
                continue
            snapshots = self._snapshot_session(ctx)
            results.append((self._build_meta(ctx), snapshots))
        self._cohort_size = len(results)
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
        project_hash = self._extract_project_hash(source_event.path, session_id=sid)
        ctx = _SessionContext(
            session_id=sid,
            file_path=source_event.path,
            project_hash=project_hash,
            parser=parser,
            collectors=collectors,
            first_event_time=now_wall,
            last_event_time=now_wall,
            last_monotonic=now_clock,
        )
        self._sessions[sid] = ctx
        return ctx

    def _extract_project_hash(self, path: Path, *, session_id: str) -> str:
        """Pull the project-hash directory from the canonical path layout.

        Layout is ``~/.claude/projects/<project-hash>/sessions/<id>.jsonl``;
        we walk parents until we find one named ``projects`` and return
        the directory immediately under it.

        When the path does not follow the documented layout, emits a WARN
        once per distinct invalid path (tracked in
        ``self._warned_invalid_paths``) so a misconfigured watcher root is
        visible without flooding the log. Falls back to a deterministic
        16-hex-char SHA-256 prefix of the raw path string so callers always
        get a non-empty, stable hash even for unrecognised layouts — the
        empty-string fallback caused silent downstream failures when code
        keyed on project_hash assumed it was non-empty.
        """

        parts = path.parts
        for index, part in enumerate(parts):
            if part == "projects" and index + 1 < len(parts):
                return parts[index + 1]
        raw = str(path)
        if raw not in self._warned_invalid_paths:
            self._warned_invalid_paths.add(raw)
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.AGGREGATOR,
                    code="aggregator.project_layout_unknown",
                    message=(
                        f"session path {raw!r} does not contain a "
                        f"'projects/<hash>' segment; falling back to "
                        f"path-derived hash, project name will be None"
                    ),
                    context={"session_id": session_id, "path": raw},
                )
            )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _instantiate_collectors(self, parser: SessionParser) -> dict[str, Collector]:
        """Build the per-session collector dict from the registry.

        ``parse_health`` is always instantiated, regardless of whether it
        appears in the enabled list — it is the only un-disableable
        collector and the validator already refuses configs that try to
        turn it off, but enforcing it here too means a buggy code path that
        bypasses the validator still cannot drop the integrity collector.

        Each collector receives its per-collector config subtree
        (``config["collectors"][name]``) as the first positional
        argument when the constructor accepts one. Collectors that
        ignore their config still work — they fall back to built-in
        defaults via ``_default_config()``.
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
        collectors_cfg: dict[str, Any] = self._config.get("collectors", {})
        for name in names:
            # Built-in collectors accept an optional ``config`` dict as
            # their first positional argument. Test fixtures register
            # stub classes with zero-arg constructors, so we only pass
            # config through when the user's resolved config actually
            # has an entry for this collector name; otherwise we fall
            # back to the argless call and let the class use its own
            # defaults. The Collector protocol doesn't describe the
            # constructor shape, so the call is cast to Any for the
            # type checker only.
            factory: Any = self._registry[name]
            per_collector_cfg = collectors_cfg.get(name)
            instance = factory(per_collector_cfg) if per_collector_cfg is not None else factory()
            bind = getattr(instance, "bind_stats", None)
            if callable(bind):
                bind(parser.stats)
            instances[name] = instance
        return instances

    # -------------------------------------------------------------- ingest path

    def _ingest_line(self, ctx: _SessionContext, line: str) -> None:
        for event in ctx.parser.parse([line]):
            if not ctx.first_event_time_set:
                ctx.first_event_time = event.timestamp
                ctx.first_event_time_set = True
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
            completed_turn = ctx.turn_grouper.ingest(event)
            if completed_turn is not None:
                if self._classifier_enabled:
                    completed_turn = dataclasses.replace(
                        completed_turn, task_type=classify_turn(completed_turn)
                    )
                ctx.completed_turns.append(completed_turn)

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
                # Update the bounded per-metric value history (max 3 entries).
                history = ctx.snapshot_history.get(collector_name)
                if history is None:
                    history = deque(maxlen=3)
                    ctx.snapshot_history[collector_name] = history
                history.append(raw.value)
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
        # Convert bounded deques to immutable tuples for SessionMeta.
        history: dict[str, tuple[float, ...]] = {
            name: tuple(dq) for name, dq in ctx.snapshot_history.items()
        }
        # Derive current session task type from completed turns when the
        # classifier is enabled. None when disabled or no turns yet.
        current_task_type: str | None = None
        if self._classifier_enabled and ctx.completed_turns:
            current_task_type = aggregate_session_task_type(ctx.completed_turns)
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
            snapshot_history=history,
            session_task_type=current_task_type,
        )

    # ----------------------------------------------------------------- lifecycle

    def _run_lifecycle_pass(self) -> None:
        now = self._clock()
        to_evict: list[tuple[str, float]] = []
        for sid, ctx in self._sessions.items():
            silence = now - ctx.last_monotonic
            if silence >= self._evict_after:
                to_evict.append((sid, silence))
                continue
            if silence >= self._stale_after and ctx.state is SessionState.ACTIVE:
                ctx.state = SessionState.STALE
        for sid, silence in to_evict:
            self._evict_session(sid, reason="silence_timeout", silence_seconds=silence)

    def _evict_session(
        self,
        session_id: str,
        *,
        reason: str = "source_delete",
        silence_seconds: float | None = None,
    ) -> None:
        ctx = self._sessions.pop(session_id, None)
        if ctx is None:
            return
        ctx.state = SessionState.EVICTED
        self._eviction_churn += 1
        # Session churn is operationally interesting: a flapping
        # watcher or a chatty editor that rolls files every few minutes
        # shows up as an elevated eviction rate in the INFO log.
        context: dict[str, Any] = {
            "session_id": session_id,
            "reason": reason,
            "event_count": ctx.event_count,
            "remaining_sessions": len(self._sessions),
        }
        if silence_seconds is not None:
            context["silence_seconds"] = silence_seconds
        record(
            CodevigilError(
                level=ErrorLevel.INFO,
                source=ErrorSource.AGGREGATOR,
                code="aggregator.session_evicted",
                message=(
                    f"session {session_id!r} evicted ({reason}); {ctx.event_count} events processed"
                ),
                context=context,
            )
        )
        self._observe_for_bootstrap(ctx)
        final_turn = ctx.turn_grouper.finalize()
        if final_turn is not None:
            if self._classifier_enabled:
                final_turn = dataclasses.replace(final_turn, task_type=classify_turn(final_turn))
            ctx.completed_turns.append(final_turn)
        self._write_session_report(ctx)
        self._reset_collectors(ctx)

    def _write_session_report(self, ctx: _SessionContext) -> None:
        """Persist a finalised session report when persistence is enabled.

        Called at session eviction time (natural end-of-session boundary).
        Skipped entirely when ``storage.enable_persistence = false`` (the
        default). Any I/O error is routed through the error channel rather
        than crashing the loop.
        """
        if self._store is None:
            return
        snapshots = ctx.last_snapshots
        if not snapshots and ctx.event_count == 0:
            # Never received any events; skip — an empty report is noise.
            return
        metrics: dict[str, float] = {name: snap.value for name, snap in snapshots.items()}
        completed = tuple(ctx.completed_turns) if ctx.completed_turns else None
        session_task_type: str | None = None
        turn_task_types: tuple[str, ...] | None = None
        if self._classifier_enabled and completed:
            session_task_type = aggregate_session_task_type(completed)
            turn_task_types = (
                tuple(t.task_type for t in completed if t.task_type is not None) or None
            )
        try:
            report = build_report(
                session_id=ctx.session_id,
                project_hash=ctx.project_hash,
                project_name=self._project_registry.resolve(ctx.project_hash),
                model=None,  # Phase 5 wires model from session metadata
                permission_mode=None,  # Phase 5 wires permission_mode
                started_at=ctx.first_event_time,
                ended_at=ctx.last_event_time,
                event_count=ctx.event_count,
                parse_confidence=float(ctx.parser.stats.parse_confidence),
                metrics=metrics,
                eviction_churn=self._eviction_churn,
                cohort_size=self._cohort_size,
                turns=completed,
                session_task_type=session_task_type,
                turn_task_types=turn_task_types,
            )
            self._store.write(report)
        except Exception as exc:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.AGGREGATOR,
                    code="aggregator.store_write_failed",
                    message=f"failed to write session report for {ctx.session_id!r}: {exc}",
                    context={
                        "session_id": ctx.session_id,
                        "exception_type": type(exc).__name__,
                    },
                )
            )

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
