"""SIGINT / shutdown path for ``codevigil watch``.

The watch loop installs a SIGINT handler that flips a module-level
``_shutdown_requested`` flag; the loop polls the flag between ticks and
breaks cleanly, calling ``aggregator.close`` and the renderer's ``close``.
We simulate the signal by flipping the flag directly via monkeypatch so
the test is hermetic — no real signal delivery required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codevigil import cli as cli_module
from codevigil.cli import main


def test_watch_clean_shutdown_on_sigint_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Redirect watcher root and log into tmp_path so we never touch the
    # user's real filesystem.
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", str(home / ".claude" / "projects"))
    monkeypatch.setenv("CODEVIGIL_WATCH_TICK_INTERVAL", "0.05")

    # Make the signal handler installer flip the flag immediately so the
    # very first iteration of the loop sees shutdown and exits cleanly.
    def _fake_install() -> None:
        cli_module._shutdown_requested = True

    monkeypatch.setattr(cli_module, "_install_sigint_handler", _fake_install)

    # Also collapse sleep so a stray iteration cannot hang the test.
    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(cli_module.time, "sleep", _fake_sleep)

    exit_code = main(["watch"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "codevigil shutdown" in captured.out


def test_watch_handles_aggregator_error_without_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codevigil.aggregator import SessionAggregator

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", str(home / ".claude" / "projects"))

    def _fake_install() -> None:
        cli_module._shutdown_requested = True

    monkeypatch.setattr(cli_module, "_install_sigint_handler", _fake_install)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)

    # Swap in an aggregator whose tick() returns an empty iterator — the
    # loop should run once (flag was pre-flipped) and exit cleanly.
    original_tick = SessionAggregator.tick
    calls: list[int] = []

    def _fake_tick(self: SessionAggregator) -> Any:
        calls.append(1)
        return iter(())

    monkeypatch.setattr(SessionAggregator, "tick", _fake_tick)
    try:
        exit_code = main(["watch"])
    finally:
        monkeypatch.setattr(SessionAggregator, "tick", original_tick)

    assert exit_code == 0
    assert "codevigil shutdown" in capsys.readouterr().out
    # Loop runs one tick, then observes the pre-flipped flag and exits.
    assert calls == [1]
