"""JSONL-to-SessionReport loader for the cohort report path.

Reads ``*.jsonl`` session files, runs the enabled collectors offline, and
produces :class:`~codevigil.analysis.store.SessionReport` objects suitable
for the cohort reducer and period comparator.

The loader extends the aggregator's ingest path with one addition: it
extracts ``write_precision`` from the ``read_edit_ratio`` collector's
``detail`` dict and stores it as an additional metric key in the
:class:`~codevigil.analysis.store.SessionReport`. This lets the cohort
reducer group and compare ``write_precision`` alongside the primary metrics
without requiring a separate collector or a schema change to the collector
protocol.

Per-entry date filtering: when ``from_timestamp`` or ``to_timestamp`` is
supplied, individual :class:`~codevigil.types.Event` objects are filtered
by ``event.timestamp`` before any collector sees them. Sessions whose
in-window event count reaches zero after filtering are skipped entirely
(no report emitted). ``SessionReport.started_at`` and ``ended_at`` are
clamped to the first and last in-window event timestamps, not to the raw
session boundaries.

No network calls. No disk writes. Offline, deterministic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codevigil.analysis.store import SessionReport, build_report
from codevigil.config import CONFIG_DEFAULTS
from codevigil.errors import CodevigilError
from codevigil.parser import SessionParser
from codevigil.types import Event, MetricSnapshot

_LOG = logging.getLogger(__name__)

# Project-hash sentinel used when the session file does not carry project info.
_UNKNOWN_PROJECT: str = "unknown"


def load_reports_from_jsonl(
    paths: list[Path],
    *,
    cfg: dict[str, Any] | None = None,
    from_timestamp: datetime | None = None,
    to_timestamp: datetime | None = None,
) -> list[SessionReport]:
    """Load a :class:`~codevigil.analysis.store.SessionReport` from each path.

    Files that cannot be parsed are skipped with a logged WARNING — they
    never abort the load. Returns reports sorted by ``started_at`` ascending.

    Parameters:
        paths: Absolute paths to ``*.jsonl`` session files.
        cfg: Effective config dict (``ResolvedConfig.values``). When
            ``None``, built-in defaults are used so the function is safe
            to call from tests without a full config stack.
        from_timestamp: Lower bound (inclusive) for ``event.timestamp``.
            Events before this timestamp are dropped. When ``None``, no
            lower bound is applied.
        to_timestamp: Upper bound (inclusive) for ``event.timestamp``.
            Events after this timestamp are dropped. When ``None``, no
            upper bound is applied. The filter short-circuits once an event
            exceeds this bound because events arrive in chronological order.

    Returns:
        List of :class:`~codevigil.analysis.store.SessionReport` sorted by
        ``started_at`` ascending. Sessions whose in-window event count is
        zero after filtering are omitted from the result.
    """
    effective_cfg: dict[str, Any] = cfg if cfg is not None else CONFIG_DEFAULTS
    reports: list[SessionReport] = []
    for path in paths:
        try:
            report = _load_one(
                path,
                cfg=effective_cfg,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
            )
        except Exception as exc:
            _LOG.warning("skipping %s: %s", path, exc)
            continue
        if report is not None:
            reports.append(report)
    reports.sort(key=lambda r: r.started_at)
    return reports


# ---------------------------------------------------------------------------
# Internal: parse one file
# ---------------------------------------------------------------------------


def _load_one(
    path: Path,
    *,
    cfg: dict[str, Any],
    from_timestamp: datetime | None = None,
    to_timestamp: datetime | None = None,
) -> SessionReport | None:
    """Parse one JSONL session file and return a SessionReport.

    Returns ``None`` when the file is empty (no events at all) or when all
    events fall outside the requested ``from_timestamp``/``to_timestamp``
    window. Raises on I/O or unrecoverable parse errors so the caller can
    skip and log.

    Events are filtered at the event pipeline level — collectors only see
    in-window events. ``started_at``/``ended_at`` on the produced
    ``SessionReport`` are clamped to the first/last in-window event
    timestamps rather than the raw session boundaries.
    """
    from codevigil.collectors import COLLECTORS  # local import to avoid boot cycle

    session_id = path.stem
    parser = SessionParser(session_id=session_id)

    # Mirror the collector instantiation order from cli._build_session_report:
    # parse_health first (it must bind parser.stats), then the rest in
    # config order.
    names: list[str] = ["parse_health"] if "parse_health" in COLLECTORS else []
    for name in cfg.get("collectors", {}).get("enabled", []):
        if name == "parse_health":
            continue
        if name in COLLECTORS:
            names.append(name)

    collector_instances: dict[str, Any] = {}
    for name in names:
        instance = COLLECTORS[name]()
        bind = getattr(instance, "bind_stats", None)
        if callable(bind):
            bind(parser.stats)
        collector_instances[name] = instance

    first_ts: datetime | None = None
    last_ts: datetime | None = None
    event_count = 0

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for event in parser.parse(handle):
                if not _event_in_window(event, from_timestamp, to_timestamp):
                    # Events arrive in chronological order; once we pass
                    # to_timestamp no subsequent event can be in-window.
                    if to_timestamp is not None and _event_after(event, to_timestamp):
                        break
                    continue
                event_count += 1
                if first_ts is None:
                    first_ts = event.timestamp
                last_ts = event.timestamp
                for collector in collector_instances.values():
                    try:
                        collector.ingest(event)
                    except CodevigilError:
                        continue
    except OSError as exc:
        raise OSError(f"cannot read {path}: {exc}") from exc

    if event_count == 0:
        return None

    snapshots: dict[str, MetricSnapshot] = {}
    for name, collector in collector_instances.items():
        try:
            snapshots[name] = collector.snapshot()
        except CodevigilError:
            continue

    # Build the primary metrics dict: collector_name -> snapshot.value.
    metrics: dict[str, float] = {}
    for name, snap in snapshots.items():
        metrics[name] = snap.value

    # Augment with write_precision extracted from read_edit_ratio detail.
    _inject_write_precision(metrics, snapshots)

    started: datetime = first_ts if first_ts is not None else datetime.now(UTC)
    ended: datetime = last_ts if last_ts is not None else started

    return build_report(
        session_id=session_id,
        project_hash=_UNKNOWN_PROJECT,
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=started,
        ended_at=ended,
        event_count=event_count,
        parse_confidence=float(parser.stats.parse_confidence),
        metrics=metrics,
    )


def _event_in_window(
    event: Event,
    from_timestamp: datetime | None,
    to_timestamp: datetime | None,
) -> bool:
    """Return True when *event* falls within the requested time window.

    Comparison is done in UTC-normalised wall-clock time by stripping
    tzinfo so that naive and aware datetimes from the CLI can be compared
    consistently. The parser always emits timezone-aware timestamps; the
    CLI may supply naive datetimes (from date-only strings) or aware ones.
    """
    ts = event.timestamp.replace(tzinfo=None)
    if from_timestamp is not None and ts < from_timestamp.replace(tzinfo=None):
        return False
    return not (to_timestamp is not None and ts > to_timestamp.replace(tzinfo=None))


def _event_after(event: Event, to_timestamp: datetime) -> bool:
    """Return True when *event* is strictly after *to_timestamp*.

    Used for the chronological short-circuit inside the ingest loop: once an
    event's timestamp exceeds the upper bound, all remaining events will also
    exceed it, so the loop can break early.
    """
    return event.timestamp.replace(tzinfo=None) > to_timestamp.replace(tzinfo=None)


def _inject_write_precision(
    metrics: dict[str, float],
    snapshots: dict[str, MetricSnapshot],
) -> None:
    """Extract write_precision from read_edit_ratio detail and add to metrics.

    Mutates ``metrics`` in place. No-ops when the snapshot is absent or
    when write_precision is None (no mutation sub-category calls seen).
    """
    snap = snapshots.get("read_edit_ratio")
    if snap is None or not isinstance(snap.detail, dict):
        return
    wp = snap.detail.get("write_precision")
    if isinstance(wp, float):
        metrics["write_precision"] = wp


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def expand_to_jsonl_paths(raw: str) -> list[Path]:
    """Resolve a path argument (file, directory, or glob) to JSONL paths.

    Returns a deterministically sorted list of absolute paths. Directories
    are walked recursively for ``*.jsonl`` files. Globs are evaluated
    relative to their parent directory.
    """
    paths: list[Path] = sorted(_expand(raw))
    return paths


def _expand(raw: str) -> Iterator[Path]:
    if any(ch in raw for ch in "*?["):
        base = Path(raw).expanduser()
        parent = base.parent if str(base.parent) else Path(".")
        pattern = base.name
        for p in parent.glob(pattern):
            if p.is_file():
                yield p
        return
    path = Path(raw).expanduser()
    if path.is_file():
        yield path
        return
    if path.is_dir():
        for p in sorted(path.rglob("*.jsonl")):
            if p.is_file():
                yield p


def load_reports_for_windows(
    paths: list[Path],
    windows: list[tuple[str, datetime, datetime]],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, list[SessionReport]]:
    """Load session reports for a list of named time windows.

    Calls :func:`load_reports_from_jsonl` once per window and returns results
    keyed by the window label. The same ``paths`` list is reused for every
    window; no duplicate parse logic is introduced — the existing per-entry
    filtering machinery does the work.

    Parameters:
        paths: Absolute paths to ``*.jsonl`` session files. Shared across all
            windows.
        windows: Ordered list of ``(label, from_timestamp, to_timestamp)``
            tuples. Each label becomes a key in the returned dict. Labels must
            be unique; later entries silently overwrite earlier ones with
            identical labels.
        cfg: Effective config dict. When ``None``, built-in defaults are used.

    Returns:
        ``dict`` mapping each window label to a (possibly empty) list of
        :class:`~codevigil.analysis.store.SessionReport` objects sorted by
        ``started_at``. Empty lists indicate no sessions fell within the window;
        the key is still present so callers can detect and render the empty case.
    """
    result: dict[str, list[SessionReport]] = {}
    for label, from_ts, to_ts in windows:
        result[label] = load_reports_from_jsonl(
            paths,
            cfg=cfg,
            from_timestamp=from_ts,
            to_timestamp=to_ts,
        )
    return result


__all__ = [
    "expand_to_jsonl_paths",
    "load_reports_for_windows",
    "load_reports_from_jsonl",
]
