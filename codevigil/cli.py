"""CLI entrypoint and subcommand dispatch.

Wires the ``codevigil`` CLI surface on top of the core pipeline:
``config check``, ``watch``, ``report``, and ``export``.

``report`` enforces the same home-directory path scope the watcher and
``JsonFileRenderer`` apply: the resolved output directory must be a
descendant of ``Path.home()``, otherwise a ``PrivacyViolationError`` is
raised and the command exits ``2``. Markdown output is deterministic
under identical input — sessions sorted by id, metric rows by name, no
wall-clock timestamps — so golden-file tests are possible.

The global ``--explain`` flag surfaces ``stop_phrase`` collector
``intent`` annotations in the relevant output channels. It is plumbed as
a bool that ``watch``, ``report``, and ``export`` each read directly:
``watch`` annotates the terminal metric label, ``report`` appends intent
text to the markdown label column and keeps ``recent_hits`` intent in
the JSON payload, and ``export`` passes through the full event payload.
Without ``--explain`` the JSON report strips intent fields from
``recent_hits`` for symmetry with the watcher's non-explain output.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from codevigil import __version__
from codevigil.aggregator import SessionAggregator
from codevigil.analysis.cohort import VALID_DIMENSIONS
from codevigil.analysis.store import SessionStore
from codevigil.bootstrap import BootstrapManager
from codevigil.config import CONFIG_DEFAULTS, ConfigError, load_config, render_config_check
from codevigil.errors import CodevigilError, ErrorLevel
from codevigil.parser import SessionParser, parse_session
from codevigil.privacy import PrivacyViolationError
from codevigil.projects import ProjectRegistry
from codevigil.renderers.terminal import TerminalRenderer
from codevigil.types import Event, MetricSnapshot, Severity
from codevigil.watcher import PollingSource

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codevigil",
        description="Local, privacy-preserving observability for Claude Code sessions.",
    )
    parser.add_argument("--version", action="version", version=f"codevigil {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a TOML config file. Overrides ~/.config/codevigil/config.toml.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        default=False,
        help="Surface stop_phrase intent annotations in watch/report/export output.",
    )

    sub = parser.add_subparsers(dest="command", required=False)

    config_parser = sub.add_parser("config", help="Inspect or validate configuration.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser(
        "check",
        help="Resolve the effective config and print each value with its source.",
    )

    sub.add_parser("watch", help="Live tick loop over ~/.claude/projects session files.")

    report_parser = sub.add_parser(
        "report",
        help="Batch analysis over one or more session files.",
    )
    report_parser.add_argument("path", type=str, help="File, directory, or glob pattern.")
    report_parser.add_argument(
        "--from",
        dest="from_date",
        type=str,
        default=None,
        help="Filter sessions whose first event is on/after YYYY-MM-DD.",
    )
    report_parser.add_argument(
        "--to",
        dest="to_date",
        type=str,
        default=None,
        help="Filter sessions whose first event is on/before YYYY-MM-DD.",
    )
    report_parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format (default: json).",
    )
    report_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override the report output directory (must live under $HOME).",
    )
    report_parser.add_argument(
        "--group-by",
        dest="group_by",
        choices=("day", "week", "project", "model", "permission_mode"),
        default=None,
        help=(
            "Produce a cohort trend table grouped by this dimension. "
            "Rows are dimension values, columns are metrics, "
            "cells show mean ± stdev (n). Cells with n<5 are redacted. "
            "Incompatible with --compare-periods."
        ),
    )
    report_parser.add_argument(
        "--compare-periods",
        dest="compare_periods",
        type=str,
        default=None,
        metavar="A_START:A_END,B_START:B_END",
        help=(
            "Compare two date ranges in YYYY-MM-DD:YYYY-MM-DD format, "
            "separated by a comma. Example: "
            "2026-03-01:2026-03-15,2026-04-01:2026-04-15. "
            "Produces a signed delta table and a prose summary per metric. "
            "Incompatible with --group-by."
        ),
    )

    export_parser = sub.add_parser(
        "export",
        help="Stream parsed events as NDJSON on stdout.",
    )
    export_parser.add_argument("path", type=str, help="File, directory, or glob pattern.")

    # history — all arguments are parsed manually in _run_history because
    # argparse subparsers conflict with the ``codevigil history <SESSION_ID>``
    # positional-as-detail-view pattern. We accept REMAINDER and dispatch
    # manually based on the first positional word.
    history_parser = sub.add_parser(
        "history",
        help="Retrospective analysis of stored session reports.",
        # Disable abbreviated prefix matching so 'list' does not match 'l'.
    )
    history_parser.add_argument(
        "history_args",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )

    return parser


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        sys.stdout.write(f"codevigil {__version__}\n")
        return 0

    if args.command == "config":
        if args.config_command == "check":
            try:
                resolved = load_config(config_path=args.config)
            except ConfigError as err:
                sys.stderr.write(_format_error(err))
                return 2 if err.level is ErrorLevel.CRITICAL else 1
            sys.stdout.write(render_config_check(resolved))
            return 0
        parser.error(f"unknown config subcommand {args.config_command!r}")

    if args.command == "watch":
        return _run_watch(args)
    if args.command == "report":
        return _run_report(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "history":
        return _run_history(args)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - parser.error raises SystemExit


def _format_error(err: CodevigilError) -> str:
    tag = err.level.value.upper()
    return f"{tag}: {err.code}: {err.message}\n"


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


_shutdown_requested: bool = False


def _install_sigint_handler() -> None:
    """Install a SIGINT handler that flips ``_shutdown_requested``.

    The handler only sets a module-level flag; the watch loop polls the
    flag after each tick and exits cleanly. Tests substitute their own
    flag flip via ``monkeypatch``.
    """

    def _handler(_signum: int, _frame: FrameType | None) -> None:
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGINT, _handler)


def _run_watch(args: argparse.Namespace) -> int:
    global _shutdown_requested
    _shutdown_requested = False
    try:
        resolved = load_config(config_path=args.config)
    except ConfigError as err:
        sys.stderr.write(_format_error(err))
        return 2 if err.level is ErrorLevel.CRITICAL else 1

    cfg = resolved.values
    watch_cfg = cfg["watch"]
    try:
        source = PollingSource(
            Path(watch_cfg["root"]),
            interval=float(watch_cfg["poll_interval"]),
            max_files=int(watch_cfg["max_files"]),
            large_file_warn_bytes=int(watch_cfg["large_file_warn_bytes"]),
        )
    except PrivacyViolationError as exc:
        sys.stderr.write(f"CRITICAL: watcher.path_scope_violation: {exc}\n")
        return 2

    bootstrap = _build_bootstrap_manager(cfg)
    aggregator = SessionAggregator(
        source,
        config=cfg,
        project_registry=ProjectRegistry(),
        clock=time.monotonic,
        bootstrap=bootstrap,
    )

    show_badge = _any_experimental_enabled(cfg)
    storage_cfg = cfg.get("storage", {})
    baseline_store: SessionStore | None = (
        SessionStore() if bool(storage_cfg.get("enable_persistence", False)) else None
    )
    renderer = TerminalRenderer(
        show_experimental_badge=show_badge,
        baseline_store=baseline_store,
    )
    explain = bool(args.explain)

    _install_sigint_handler()
    tick_interval = float(watch_cfg["tick_interval"])

    try:
        while True:
            try:
                pairs = list(aggregator.tick())
            except CodevigilError as err:
                renderer.render_error(err, None)
                pairs = []
            renderer.begin_tick()
            for meta, snapshots in pairs:
                adjusted = _apply_explain_to_snapshots(snapshots, explain=explain)
                renderer.render(adjusted, meta)
            renderer.end_tick()
            if _shutdown_requested:
                break
            time.sleep(tick_interval)
            if _shutdown_requested:
                break
    finally:
        aggregator.close()
        renderer.close()
        sys.stdout.write("\ncodevigil shutdown\n")
        sys.stdout.flush()
    return 0


def _build_bootstrap_manager(cfg: dict[str, Any]) -> BootstrapManager | None:
    """Construct the watch-mode bootstrap manager from the resolved config.

    Hard caps are derived from each enabled collector's per-collector
    ``warn_threshold`` / ``critical_threshold`` keys. Those keys default to
    the literal values from ``docs/design.md`` §v0.1 Collectors, so the
    fallback ceiling and the built-in default meet at the same place. If
    the user overrides a threshold in TOML, that override becomes the new
    ceiling — calibration should never loosen their intent.
    """

    bootstrap_cfg = cfg.get("bootstrap")
    if not isinstance(bootstrap_cfg, dict):
        return None
    raw_path = bootstrap_cfg.get("state_path")
    raw_target = bootstrap_cfg.get("sessions")
    if not isinstance(raw_path, str) or not isinstance(raw_target, int):
        return None
    state_path = Path(raw_path).expanduser()
    hard_caps: dict[str, tuple[float, float]] = {}
    collectors_cfg = cfg.get("collectors", {})
    enabled = collectors_cfg.get("enabled", [])
    for name in enabled:
        section = collectors_cfg.get(name)
        if not isinstance(section, dict):
            continue
        warn = section.get("warn_threshold")
        critical = section.get("critical_threshold")
        if isinstance(warn, (int, float)) and isinstance(critical, (int, float)):
            hard_caps[f"{name}.{name}"] = (float(warn), float(critical))
    mgr = BootstrapManager(
        state_path=state_path,
        target_sessions=int(raw_target),
        hard_caps=hard_caps,
    )
    mgr.load()
    return mgr


def _any_experimental_enabled(cfg: dict[str, Any]) -> bool:
    """Return True if any enabled collector has ``experimental = True``."""

    collectors = cfg.get("collectors", {})
    enabled = collectors.get("enabled", [])
    for name in enabled:
        section = collectors.get(name)
        if isinstance(section, dict) and section.get("experimental") is True:
            return True
    return False


def _apply_explain_to_snapshots(
    snapshots: list[MetricSnapshot],
    *,
    explain: bool,
) -> list[MetricSnapshot]:
    """Append stop_phrase intent annotations to the label when ``--explain``.

    Other collectors pass through unchanged. The renderer's label slot is
    the only surface that reaches the terminal body, and the design
    calls for intent to appear alongside the matched phrase, so we
    thread it in here rather than teaching the renderer a new hook.
    """

    if not explain:
        return snapshots
    out: list[MetricSnapshot] = []
    for snap in snapshots:
        annotation = _intent_annotation(snap)
        if annotation is None:
            out.append(snap)
            continue
        label = f"{snap.label} | intent: {annotation}" if snap.label else f"intent: {annotation}"
        out.append(
            MetricSnapshot(
                name=snap.name,
                value=snap.value,
                label=label,
                severity=snap.severity,
                detail=snap.detail,
            )
        )
    return out


def _intent_annotation(snap: MetricSnapshot) -> str | None:
    """Pull the most recent ``intent`` from a stop_phrase snapshot, if any."""

    if snap.name != "stop_phrase":
        return None
    detail = snap.detail
    if not isinstance(detail, dict):
        return None
    recent = detail.get("recent_hits")
    if not isinstance(recent, list) or not recent:
        return None
    last = recent[-1]
    if not isinstance(last, dict):
        return None
    intent = last.get("intent")
    if isinstance(intent, str) and intent:
        return intent
    return None


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SessionReport:
    """One session's rolled-up report derived from a fully-parsed file."""

    session_id: str
    file_path: Path
    first_event_time: datetime | None
    last_event_time: datetime | None
    event_count: int
    parse_confidence: float
    metrics: list[MetricSnapshot]


def _run_report(args: argparse.Namespace) -> int:
    try:
        resolved = load_config(config_path=args.config)
    except ConfigError as err:
        sys.stderr.write(_format_error(err))
        return 2 if err.level is ErrorLevel.CRITICAL else 1

    cfg = resolved.values

    # Mutual exclusivity check for the two new flags.
    group_by: str | None = getattr(args, "group_by", None)
    compare_periods_arg: str | None = getattr(args, "compare_periods", None)
    if group_by is not None and compare_periods_arg is not None:
        sys.stderr.write(
            "CRITICAL: cli.report.flag_conflict: "
            "--group-by and --compare-periods are mutually exclusive\n"
        )
        return 2

    if group_by is not None:
        return _run_report_group_by(args, cfg=cfg, dimension=group_by)
    if compare_periods_arg is not None:
        return _run_report_compare_periods(args, cfg=cfg, raw_periods=compare_periods_arg)

    # Original per-session report path (unchanged).
    from_dt = _parse_date_filter(args.from_date, end_of_day=False)
    to_dt = _parse_date_filter(args.to_date, end_of_day=True)
    if from_dt is None and args.from_date is not None:
        sys.stderr.write(f"CRITICAL: cli.report.bad_date: --from {args.from_date!r}\n")
        return 2
    if to_dt is None and args.to_date is not None:
        sys.stderr.write(f"CRITICAL: cli.report.bad_date: --to {args.to_date!r}\n")
        return 2

    try:
        output_dir = _resolve_report_output_dir(cfg, override=args.output)
    except PrivacyViolationError as exc:
        sys.stderr.write(f"CRITICAL: report.path_scope_violation: {exc}\n")
        return 2

    files = list(_expand_path_argument(args.path))
    files = _filter_by_date(files, from_dt=from_dt, to_dt=to_dt)
    files.sort(key=lambda p: str(p))

    session_reports: list[_SessionReport] = []
    exit_code = 0
    for path in files:
        report = _build_session_report(path, cfg)
        session_reports.append(report)
        if report.parse_confidence < 0.9:
            exit_code = 2

    output_dir.mkdir(parents=True, exist_ok=True)
    explain = bool(args.explain)
    if args.format == "json":
        payload = _render_report_json(session_reports, explain=explain)
        _write_report(output_dir / "report.json", payload)
        sys.stdout.write(payload)
    else:
        payload = _render_report_markdown(session_reports, explain=explain)
        _write_report(output_dir / "report.md", payload)
        sys.stdout.write(payload)
    sys.stdout.flush()
    return exit_code


def _run_report_group_by(
    args: argparse.Namespace,
    *,
    cfg: dict[str, Any],
    dimension: str,
) -> int:
    """Run the cohort trend table report for ``--group-by DIMENSION``."""
    from typing import cast

    from codevigil.analysis.cohort import GroupByDimension
    from codevigil.report.loader import expand_to_jsonl_paths, load_reports_from_jsonl
    from codevigil.report.renderer import render_group_by_report

    if dimension not in VALID_DIMENSIONS:
        sys.stderr.write(
            f"CRITICAL: cli.report.bad_group_by: "
            f"unsupported dimension {dimension!r}; "
            f"valid: {sorted(VALID_DIMENSIONS)!r}\n"
        )
        return 2

    try:
        output_dir = _resolve_report_output_dir(cfg, override=getattr(args, "output", None))
    except PrivacyViolationError as exc:
        sys.stderr.write(f"CRITICAL: report.path_scope_violation: {exc}\n")
        return 2

    since_date = _parse_date_only(getattr(args, "from_date", None))
    until_date = _parse_date_only(getattr(args, "to_date", None))
    from_dt = _parse_date_filter(getattr(args, "from_date", None), end_of_day=False)
    to_dt = _parse_date_filter(getattr(args, "to_date", None), end_of_day=True)

    paths = expand_to_jsonl_paths(args.path)
    store_reports = load_reports_from_jsonl(
        paths, cfg=cfg, from_timestamp=from_dt, to_timestamp=to_dt
    )

    payload = render_group_by_report(
        store_reports,
        dimension=cast(GroupByDimension, dimension),
        since=since_date,
        until=until_date,
        cfg=cfg,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_report(output_dir / f"cohort_{dimension}.md", payload)
    sys.stdout.write(payload)
    sys.stdout.flush()
    return 0


def _run_report_compare_periods(
    args: argparse.Namespace,
    *,
    cfg: dict[str, Any],
    raw_periods: str,
) -> int:
    """Run the period-comparison report for ``--compare-periods A:B,C:D``."""
    from codevigil.report.loader import expand_to_jsonl_paths, load_reports_from_jsonl
    from codevigil.report.renderer import render_compare_periods_report

    parsed = _parse_compare_periods_arg(raw_periods)
    if parsed is None:
        sys.stderr.write(
            f"CRITICAL: cli.report.bad_compare_periods: "
            f"expected format YYYY-MM-DD:YYYY-MM-DD,YYYY-MM-DD:YYYY-MM-DD, "
            f"got {raw_periods!r}\n"
        )
        return 2
    period_a_since, period_a_until, period_b_since, period_b_until = parsed

    try:
        output_dir = _resolve_report_output_dir(cfg, override=getattr(args, "output", None))
    except PrivacyViolationError as exc:
        sys.stderr.write(f"CRITICAL: report.path_scope_violation: {exc}\n")
        return 2

    paths = expand_to_jsonl_paths(args.path)
    # No --from/--to date filtering at the event level for compare-periods:
    # that path already splits sessions by period-date ranges at the renderer
    # level. Event-level filtering is transparent here because we load all
    # sessions and let the renderer bucket them by session-level started_at.
    store_reports = load_reports_from_jsonl(paths, cfg=cfg)

    payload = render_compare_periods_report(
        store_reports,
        period_a_since=period_a_since,
        period_a_until=period_a_until,
        period_b_since=period_b_since,
        period_b_until=period_b_until,
        cfg=cfg,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_report(output_dir / "compare_periods.md", payload)
    sys.stdout.write(payload)
    sys.stdout.flush()
    return 0


def _parse_date_only(value: str | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` string to a :class:`date`, or return ``None``."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_compare_periods_arg(
    raw: str,
) -> tuple[date, date, date, date] | None:
    """Parse ``YYYY-MM-DD:YYYY-MM-DD,YYYY-MM-DD:YYYY-MM-DD`` into four dates.

    Returns ``None`` on any parse failure.
    """
    parts = raw.strip().split(",")
    if len(parts) != 2:
        return None
    a_part, b_part = parts[0].strip(), parts[1].strip()

    a_dates = a_part.split(":")
    b_dates = b_part.split(":")
    if len(a_dates) != 2 or len(b_dates) != 2:
        return None

    try:
        a_since = date.fromisoformat(a_dates[0].strip())
        a_until = date.fromisoformat(a_dates[1].strip())
        b_since = date.fromisoformat(b_dates[0].strip())
        b_until = date.fromisoformat(b_dates[1].strip())
    except ValueError:
        return None

    return a_since, a_until, b_since, b_until


def _parse_date_filter(value: str | None, *, end_of_day: bool) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if end_of_day and parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed


def _resolve_report_output_dir(cfg: dict[str, Any], *, override: Path | None) -> Path:
    """Resolve the report output directory and enforce the home-scope rule.

    The config default is ``~/.local/share/codevigil/reports``. Users may
    override via ``report.output_dir`` in the TOML file or via the
    ``--output`` CLI flag; the resolved absolute path must be a descendant
    of ``Path.home().resolve()`` or the command aborts with
    ``PrivacyViolationError``. This mirrors the watcher's filesystem scope
    gate so there is exactly one privacy rule across read and write paths.
    """

    if override is not None:
        candidate = override
    else:
        raw = cfg.get("report", {}).get("output_dir", CONFIG_DEFAULTS["report"]["output_dir"])
        candidate = Path(str(raw))
    resolved = candidate.expanduser().resolve()
    home = Path.home().resolve()
    if not resolved.is_relative_to(home):
        raise PrivacyViolationError(
            f"report output directory {str(resolved)!r} is outside the user "
            f"home directory {str(home)!r}; refusing to write"
        )
    return resolved


def _expand_path_argument(raw: str) -> Iterator[Path]:
    """Resolve ``raw`` to one or more ``*.jsonl`` files.

    Accepts a file, a directory (walked recursively), or a shell-style
    glob (``*``/``?`` character present). File existence is verified here
    so a typo gets a loud message instead of an empty report.
    """

    if any(ch in raw for ch in "*?["):
        base = Path(raw).expanduser()
        parent = base.parent if str(base.parent) else Path(".")
        pattern = base.name
        yield from sorted(p for p in parent.glob(pattern) if p.is_file())
        return
    path = Path(raw).expanduser()
    if path.is_file():
        yield path
        return
    if path.is_dir():
        yield from sorted(p for p in path.rglob("*.jsonl") if p.is_file())
        return
    # Non-existent path: yield nothing. Callers render an empty report.


def _filter_by_date(
    paths: Iterable[Path],
    *,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> list[Path]:
    if from_dt is None and to_dt is None:
        return list(paths)
    kept: list[Path] = []
    for path in paths:
        first = _peek_first_event_timestamp(path)
        if first is None:
            kept.append(path)
            continue
        if from_dt is not None and first.replace(tzinfo=None) < from_dt.replace(tzinfo=None):
            continue
        if to_dt is not None and first.replace(tzinfo=None) > to_dt.replace(tzinfo=None):
            continue
        kept.append(path)
    return kept


def _peek_first_event_timestamp(path: Path) -> datetime | None:
    """Read the first non-blank line of ``path`` and extract ``timestamp``."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    return None
                raw = parsed.get("timestamp") if isinstance(parsed, dict) else None
                if not isinstance(raw, str) or not raw:
                    return None
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return None
    except OSError:
        return None
    return None


def _build_session_report(path: Path, cfg: dict[str, Any]) -> _SessionReport:
    """Parse ``path`` end-to-end and run every enabled collector offline.

    One-shot replay of the per-session ingest path: every event feeds
    through the same collector instances the aggregator would have built
    at tick time, then snapshot once at the end. No source, no tick loop,
    no lifecycle — just parser plus collectors.
    """

    from codevigil.collectors import COLLECTORS  # local to avoid CLI/boot cycle

    session_id = path.stem
    parser = SessionParser(session_id=session_id)
    collector_instances: dict[str, Any] = {}

    names: list[str] = ["parse_health"] if "parse_health" in COLLECTORS else []
    for name in cfg.get("collectors", {}).get("enabled", []):
        if name == "parse_health":
            continue
        if name in COLLECTORS:
            names.append(name)
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
                event_count += 1
                if first_ts is None:
                    first_ts = event.timestamp
                last_ts = event.timestamp
                for collector in collector_instances.values():
                    try:
                        collector.ingest(event)
                    except CodevigilError:
                        # Non-swallowing rule: peers continue, per-file
                        # degradation surfaces via parse_health.
                        continue
    except OSError:
        pass

    metrics: list[MetricSnapshot] = []
    for collector in collector_instances.values():
        try:
            metrics.append(collector.snapshot())
        except CodevigilError:
            continue

    return _SessionReport(
        session_id=session_id,
        file_path=path,
        first_event_time=first_ts,
        last_event_time=last_ts,
        event_count=event_count,
        parse_confidence=float(parser.stats.parse_confidence),
        metrics=metrics,
    )


def _render_report_json(reports: list[_SessionReport], *, explain: bool) -> str:
    out_lines: list[str] = []
    for report in sorted(reports, key=lambda r: r.session_id):
        record: dict[str, Any] = {
            "kind": "session_report",
            "session_id": report.session_id,
            "file_path": str(report.file_path),
            "first_event_time": (
                report.first_event_time.isoformat() if report.first_event_time else None
            ),
            "last_event_time": (
                report.last_event_time.isoformat() if report.last_event_time else None
            ),
            "event_count": report.event_count,
            "parse_confidence": report.parse_confidence,
            "metrics": [
                _metric_to_dict(m, explain=explain)
                for m in sorted(report.metrics, key=lambda m: m.name)
            ],
        }
        out_lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _render_report_markdown(reports: list[_SessionReport], *, explain: bool) -> str:
    """Render a deterministic markdown summary.

    Output is stable under identical input: sessions are sorted by id,
    metric rows by name, and no wall-clock timestamps are embedded. This
    is what makes the golden-output test in ``tests/cli`` possible.
    """

    lines: list[str] = ["# codevigil report", ""]
    for report in sorted(reports, key=lambda r: r.session_id):
        lines.append(f"## session `{report.session_id}`")
        lines.append("")
        lines.append(f"- file: `{report.file_path}`")
        lines.append(f"- events: {report.event_count}")
        lines.append(f"- parse_confidence: {report.parse_confidence:.2f}")
        lines.append("")
        lines.append("| metric | value | severity | label |")
        lines.append("| --- | --- | --- | --- |")
        for metric in sorted(report.metrics, key=lambda m: m.name):
            label = metric.label
            if explain:
                annotation = _intent_annotation(metric)
                if annotation is not None:
                    label = f"{label} | intent: {annotation}" if label else f"intent: {annotation}"
            lines.append(
                f"| {metric.name} | {metric.value:.2f} | "
                f"{_severity_word(metric.severity)} | {label} |"
            )
        lines.append("")
    return "\n".join(lines)


def _severity_word(severity: Severity) -> str:
    return {
        Severity.OK: "OK",
        Severity.WARN: "WARN",
        Severity.CRITICAL: "CRIT",
    }[severity]


def _metric_to_dict(metric: MetricSnapshot, *, explain: bool) -> dict[str, Any]:
    detail = metric.detail
    if not explain and isinstance(detail, dict) and "recent_hits" in detail:
        stripped_recent = [
            {k: v for k, v in hit.items() if k != "intent"}
            for hit in detail["recent_hits"]
            if isinstance(hit, dict)
        ]
        detail = {**detail, "recent_hits": stripped_recent}
    return {
        "name": metric.name,
        "value": metric.value,
        "label": metric.label,
        "severity": metric.severity.value,
        "detail": detail,
    }


def _write_report(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(payload)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _run_history(args: argparse.Namespace) -> int:
    """Dispatch ``codevigil history`` subcommands.

    History arguments are parsed manually because argparse subparsers
    conflict with the ``codevigil history <SESSION_ID>`` form (a bare
    session id would be rejected as an unknown subcommand choice).
    ``history_args`` is a REMAINDER list; we dispatch on the first element.

    Valid forms::

        codevigil history list [--project P] [--since D] [--until D]
                               [--severity S] [--model M] [--permission-mode M]
        codevigil history <SESSION_ID>
        codevigil history diff A B
        codevigil history heatmap <SESSION_ID>
    """
    from codevigil.history.detail_cmd import run_detail
    from codevigil.history.diff_cmd import run_diff
    from codevigil.history.filters import parse_date_arg
    from codevigil.history.heatmap_cmd import run_heatmap
    from codevigil.history.list_cmd import run_list

    remainder: list[str] = list(getattr(args, "history_args", []) or [])

    if not remainder:
        sys.stderr.write(
            "usage: codevigil history list [OPTIONS]\n"
            "       codevigil history <SESSION_ID>\n"
            "       codevigil history diff A B\n"
            "       codevigil history heatmap <SESSION_ID>\n"
        )
        return 2

    subcmd = remainder[0]
    rest = remainder[1:]

    if subcmd == "list":
        return _run_history_list(rest, run_list=run_list, parse_date_arg=parse_date_arg)

    if subcmd == "diff":
        if len(rest) < 2:
            sys.stderr.write("usage: codevigil history diff <SESSION_A> <SESSION_B>\n")
            return 2
        return run_diff(rest[0], rest[1])

    if subcmd == "heatmap":
        if not rest:
            sys.stderr.write("usage: codevigil history heatmap <SESSION_ID>\n")
            return 2
        return run_heatmap(rest[0])

    # Any other first token is treated as a session id for the detail view.
    return run_detail(subcmd)


def _run_history_list(
    argv: list[str],
    *,
    run_list: Any,
    parse_date_arg: Any,
) -> int:
    """Parse ``history list`` flags and invoke ``run_list``."""
    p = argparse.ArgumentParser(prog="codevigil history list", add_help=True)
    p.add_argument("--project", default=None)
    p.add_argument("--since", dest="since", type=str, default=None, metavar="YYYY-MM-DD")
    p.add_argument("--until", dest="until", type=str, default=None, metavar="YYYY-MM-DD")
    p.add_argument("--severity", choices=("ok", "warn", "crit"), default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--permission-mode", dest="permission_mode", default=None)
    parsed = p.parse_args(argv)

    since_date = None
    until_date = None
    if parsed.since is not None:
        try:
            since_date = parse_date_arg(parsed.since)
        except ValueError as exc:
            sys.stderr.write(f"CRITICAL: history.bad_date: {exc}\n")
            return 2
    if parsed.until is not None:
        try:
            until_date = parse_date_arg(parsed.until)
        except ValueError as exc:
            sys.stderr.write(f"CRITICAL: history.bad_date: {exc}\n")
            return 2

    return run_list(  # type: ignore[no-any-return]
        project=parsed.project,
        since=since_date,
        until=until_date,
        severity=parsed.severity,
        model=parsed.model,
        permission_mode=parsed.permission_mode,
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _run_export(args: argparse.Namespace) -> int:
    """Dump events as NDJSON on stdout.

    Event serialization shape (one JSON object per line)::

        {"timestamp": "<iso>", "session_id": "<id>", "kind": "<kind>",
         "payload": {...}}

    Deliberately *different* from ``JsonFileRenderer``: that renderer
    writes one snapshot row per tick per session with metric values,
    whereas ``export`` reproduces the parsed event stream so callers can
    pipe it into ``jq`` and compute their own aggregates.
    """

    explain = bool(args.explain)
    for path in _expand_path_argument(args.path):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for event in parse_session(handle, session_id=path.stem):
                    sys.stdout.write(_event_to_ndjson_line(event, explain=explain))
                    sys.stdout.write("\n")
        except OSError:
            continue
    sys.stdout.flush()
    return 0


def _event_to_ndjson_line(event: Event, *, explain: bool) -> str:
    payload = dict(event.payload)
    if not explain and "intent" in payload:
        payload.pop("intent", None)
    record = {
        "timestamp": event.timestamp.isoformat(),
        "session_id": event.session_id,
        "kind": event.kind.value,
        "payload": payload,
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


__all__ = ["main"]
