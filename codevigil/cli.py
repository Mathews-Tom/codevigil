"""CLI entrypoint and subcommand dispatch.

Phase 2 wires only the ``config check`` subcommand. ``watch``, ``report``,
and ``export`` subcommands land in follow-up phases and are currently
registered as stubs that exit with a clear "not yet implemented" message
so the CLI surface is discoverable from ``--help`` but cannot accidentally
be invoked against half-built plumbing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from codevigil import __version__
from codevigil.config import ConfigError, load_config, render_config_check
from codevigil.errors import CodevigilError, ErrorLevel


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

    for name in ("watch", "report", "export"):
        sub.add_parser(
            name,
            help=f"{name} mode — wiring lands in a follow-up change.",
        )

    return parser


def _run_config_check(args: argparse.Namespace) -> int:
    resolved = load_config(config_path=args.config)
    sys.stdout.write(render_config_check(resolved))
    return 0


def _format_error(err: CodevigilError) -> str:
    tag = err.level.value.upper()
    return f"{tag}: {err.code}: {err.message}\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        sys.stdout.write(f"codevigil {__version__}\n")
        return 0

    if args.command == "config":
        if args.config_command == "check":
            try:
                return _run_config_check(args)
            except ConfigError as err:
                sys.stderr.write(_format_error(err))
                return 2 if err.level is ErrorLevel.CRITICAL else 1
        parser.error(f"unknown config subcommand {args.config_command!r}")

    if args.command in {"watch", "report", "export"}:
        sys.stderr.write(
            f"ERROR: the {args.command!r} command is not wired yet; it lands in a "
            "follow-up change. See the development plan for the current scope.\n"
        )
        return 2

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover — parser.error raises SystemExit


__all__ = ["main"]
