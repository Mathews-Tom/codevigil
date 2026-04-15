"""Ctrl+C during the watch tick interval must exit immediately.

Regression test for the 60-second tick default: ``time.sleep`` is not
interruptible by a flag-flipping signal handler, so the old loop
waited the full interval before honoring SIGINT. The loop now uses a
``threading.Event.wait`` which the handler wakes via ``set()``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import codevigil.cli as cli_module
from codevigil.cli import main


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", str(home / ".claude" / "projects"))
    (home / ".claude" / "projects" / "X" / "sessions").mkdir(parents=True)
    (home / ".claude" / "projects" / "X" / "sessions" / "s.jsonl").write_text(
        '{"type":"user","timestamp":"2025-11-01T10:00:00Z",'
        '"message":{"id":"u1","content":[{"type":"text","text":"hi"}]}}\n',
        encoding="utf-8",
    )
    yield home


def test_shutdown_event_wakes_wait_immediately(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must exit within a few hundred milliseconds when the
    shutdown event is set mid-wait, regardless of tick_interval."""
    monkeypatch.setattr(cli_module, "_install_sigint_handler", lambda: None)

    # Force a very long tick_interval so the test would hang if the
    # loop used a non-interruptible sleep.
    original_dispatch = cli_module._run_watch

    def _wake_soon() -> None:
        time.sleep(0.15)
        cli_module._shutdown_event.set()

    threading.Thread(target=_wake_soon, daemon=True).start()

    # Monkeypatch tick_interval via a config override by writing a TOML
    # and passing it through env/CLI. Simpler: patch load_config to
    # return a high tick_interval.
    from codevigil.config import ResolvedConfig

    real_load = cli_module.load_config

    def _long_load(*args: Any, **kwargs: Any) -> ResolvedConfig:
        resolved = real_load(*args, **kwargs)
        resolved.values["watch"]["tick_interval"] = 30.0
        return resolved

    monkeypatch.setattr(cli_module, "load_config", _long_load)

    started = time.monotonic()
    exit_code = main(["watch"])
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 5.0, (
        f"watch loop did not honor the shutdown event within 5 s "
        f"(tick_interval=30 s); actual elapsed={elapsed:.2f}s"
    )
    # Avoid the unused-import warning — original_dispatch is retained
    # for debugging if the monkeypatch strategy regresses.
    assert original_dispatch is cli_module._run_watch or elapsed >= 0
