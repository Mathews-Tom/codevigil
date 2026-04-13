"""CLI entrypoint and subcommand dispatch.

Wires the ``codevigil`` CLI surface on top of the core pipeline. This
commit lands ``watch`` mode — the live tick loop that drives
``PollingSource`` through ``SessionAggregator`` and renders each tick
via ``TerminalRenderer``. ``config check`` is unchanged; ``report`` and
``export`` land in follow-up changes and are currently stubbed out so the
CLI surface is discoverable from ``--help`` but cannot accidentally be
invoked against half-built plumbing.

SIGINT handling
---------------

``watch`` installs a signal handler that flips a module-level
``_shutdown_requested`` flag; the main loop checks the flag between tick
body and the next ``time.sleep`` so we get a clean shutdown path instead
of the default ``KeyboardInterrupt`` raise, which would skip
``aggregator.close`` and the terminal renderer's final flush. Tests
substitute their own flag flip via ``monkeypatch`` rather than delivering
a real signal, so the shutdown path is hermetic.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from codevigil import __version__
from codevigil.aggregator import SessionAggregator
from codevigil.config import ConfigError, load_config, render_config_check
from codevigil.errors import CodevigilError, ErrorLevel
from codevigil.privacy import PrivacyViolationError
from codevigil.projects import ProjectRegistry
from codevigil.renderers.terminal import TerminalRenderer
from codevigil.watcher import PollingSource


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

    sub = parser.add_subparsers(dest="command", required=False)

    config_parser = sub.add_parser("config", help="Inspect or validate configuration.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser(
        "check",
        help="Resolve the effective config and print each value with its source.",
    )

    sub.add_parser("watch", help="Live tick loop over ~/.claude/projects session files.")

    for name in ("report", "export"):
        sub.add_parser(
            name,
            help=f"{name} mode — wiring lands in a follow-up change.",
        )

    return parser


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

    if args.command in {"report", "export"}:
        sys.stderr.write(
            f"ERROR: the {args.command!r} command is not wired yet; it lands in a "
            "follow-up change. See the development plan for the current scope.\n"
        )
        return 2

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

    aggregator = SessionAggregator(
        source,
        config=cfg,
        project_registry=ProjectRegistry(),
        clock=time.monotonic,
    )

    show_badge = _any_experimental_enabled(cfg)
    renderer = TerminalRenderer(show_experimental_badge=show_badge)

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
                renderer.render(snapshots, meta)
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


def _any_experimental_enabled(cfg: dict[str, Any]) -> bool:
    """Return True if any enabled collector has ``experimental = True``."""

    collectors = cfg.get("collectors", {})
    enabled = collectors.get("enabled", [])
    for name in enabled:
        section = collectors.get(name)
        if isinstance(section, dict) and section.get("experimental") is True:
            return True
    return False


__all__ = ["main"]
